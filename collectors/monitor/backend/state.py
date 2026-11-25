"""Read collector files without importing or changing the running collector."""
from __future__ import annotations

import csv
import ctypes
import json
import os
import re
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from android_health import AndroidHealth
from classification import matches_korean
from src.reels.korean_classification import apply_korean_overrides
from src.reels.android_reel_metrics import android_result_matches_reel
from src.reels.reel_store import open_csv_snapshot

BASE = Path(__file__).resolve().parents[1]
COLLECTORS = BASE.parent
UTC = timezone.utc


def project_korean(detail, selected="all"):
    """Filter recorded metrics while retaining actual process/queue health."""
    result = {key: value for key, value in detail.items() if key not in {"rows", "logs"}}
    if selected == "all":
        return result
    rows = [row for row in detail["rows"] if matches_korean(row, selected)]
    rounds = Counter()
    for row in rows:
        value = str(row.get("collection_number") or "").strip()
        rounds[int(value) if value.isdecimal() and int(value) > 0 else 0] += 1
    current = time.time()
    result["summary"] = {**detail["summary"], "unique": len({row.get("url") for row in rows}),
                         "snapshots": len(rows), "last_hour": sum(timestamp(row.get("collected_at")) >= current - 3600 for row in rows),
                         "rounds": [{"round": number or None, "count": rounds[number]}
                                    for number in sorted(rounds, key=lambda number: (number == 0, number))],
                         "last_collected_at": rows[0].get("collected_at") if rows else ""}
    def bins(times):
        counts = Counter(int(value // 300) * 300 for value in times
                         if detail["chart_times"][0] <= value <= current)
        return [counts[start] for start in detail["chart_times"]]
    def shortcode(value):
        match = re.search(r"https?://(?:www\.)?instagram\.com/(?:reels?|p)/([A-Za-z0-9_-]+)", str(value or ""), re.I)
        return match[1] if match else ""
    latest = {}
    for row in detail["rows"]:  # Snapshot rows are ordered newest first.
        code = shortcode(row.get("url"))
        if code:
            latest.setdefault(code, row)
    android_times = [timestamp(event["at"]) for event in detail["logs"]
                     if event["event"] == "reel_metric_collected"
                     and (row := latest.get(shortcode(event.get("message")))) is not None
                     and matches_korean(row, selected)]
    result["web"] = {**detail["web"], "bins": bins(timestamp(row.get("collected_at")) for row in rows)}
    result["android"] = {**detail["android"], "bins": bins(android_times)}
    return result


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp(value) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def refresh_summary(path: Path) -> dict:
    inbox = path / '.collector' / 'refresh_inbox'
    try:
        jobs = json.loads((inbox / 'queue.json').read_text(encoding='utf-8-sig'))
        if not isinstance(jobs, list) or not all(isinstance(job, dict) and isinstance(job.get('urls', []), list)
                                              and all(isinstance(url, str) for url in job.get('urls', [])) for job in jobs):
            raise ValueError('Invalid inbox queue')
        blocked = {url for job in jobs if job.get('kind') == 'cancel' for url in job.get('urls', [])}
        queued = {url for job in jobs if job.get('state') == 'queued' for url in job.get('urls', [])} - blocked
        registered = {url for job in jobs if job.get('state') == 'registered' for url in job.get('urls', [])} - blocked
        counts = {'queued': len(queued), 'registered': len(registered), 'cancelled': len(blocked)}
        warning = ''
    except FileNotFoundError:
        counts = dict.fromkeys(('queued', 'registered', 'cancelled'))
        warning = '입력함 기록이 없습니다.'
    except (OSError, ValueError, TypeError):
        counts = dict.fromkeys(('queued', 'registered', 'cancelled'))
        warning = '입력 큐 파일을 읽지 못했습니다. 잠시 후 다시 확인합니다.'
    capacity_path = inbox / 'capacity.json'
    capacity = read_json(capacity_path)
    status = capacity.get('status') if isinstance(capacity.get('status'), dict) else {}
    reservations = capacity.get('reservations') if isinstance(capacity.get('reservations'), dict) else {}
    runtime = read_json(path / '.collector' / 'refresh_status.json')
    if runtime:
        runtime = {**runtime, 'scope': 'run'}
    else:
        local = read_json(path / 'collector_status.json')
        runtime = {key: local.get(key) for key in ('started_at', 'finished_at', 'state', 'pid')}
        runtime.update(scope='batch', age_intervals=None)
    if runtime.get('started_at') and not runtime.get('finished_at'):
        runtime['alive'] = process_info(runtime.get('pid'), runtime['started_at'])['alive']
    return {**counts, 'has_inbox': inbox.is_dir(), 'warning': warning,
            'runtime': runtime,
            'hourly_capacity': status.get('hourly_capacity') if status.get('measured') else None,
            'reserved': sum(value for value in reservations.values() if isinstance(value, int) and value >= 0) if capacity else None,
            'capacity_at': datetime.fromtimestamp(signature(capacity_path)[0] / 1e9, UTC).isoformat() if capacity else '',
            'output_name': 'reels_updated.xlsx' if not inbox.is_dir() and (path / 'reels_updated.xlsx').exists() else 'reels.xlsx'}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def signature(path: Path) -> tuple:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


def tail(path: Path, size: int = 262144) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - size)
            stream.seek(offset)
            if offset:
                stream.readline()
            return stream.read().decode("utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []


def process_info(pid, started_at: str = "") -> dict:
    """Check process creation time too, so a reused PID is not reported as alive."""
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        pid = 0
    result = {"pid": pid, "alive": None, "created_at": "", "image": ""}
    if pid <= 0:
        return result
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            result["alive"] = True
        except ProcessLookupError:
            result["alive"] = False
        except PermissionError:
            pass
        return result
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        result["alive"] = None if ctypes.get_last_error() == 5 else False
        return result
    try:
        code = wintypes.DWORD()
        if kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            result["alive"] = code.value == 259
        created, ended, system, user = (wintypes.FILETIME() for _ in range(4))
        if kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(system), ctypes.byref(user)):
            seconds = ((created.dwHighDateTime << 32) + created.dwLowDateTime) / 10_000_000 - 11644473600
            result["created_at"] = datetime.fromtimestamp(seconds, UTC).isoformat()
            if started_at and timestamp(started_at) and seconds > timestamp(started_at) + 10:
                result["alive"] = False
        length = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(length.value)
        if kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
            result["image"] = image.value
    finally:
        kernel.CloseHandle(handle)
    return result


def owner_info(owner: dict) -> dict:
    result = process_info(owner.get("pid"), owner.get("started_at", ""))
    if result["image"] and Path(result["image"]).name.lower() not in {"python.exe", "pythonw.exe", "python"}:
        result["alive"] = False
    return result


ANDROID_STATES = {
    "running": ("처리 중", "good"),
    "starting": ("시작 중", "info"),
    "idle": ("작업 대기", "info"),
    "waiting_for_device": ("기기 연결 대기", "warning"),
    "recovering_device": ("기기 복구 중", "warning"),
    "recovery_failed": ("연결 복구 대기", "danger"),
    "recovering_ui": ("화면 복구 중", "warning"),
    "requeued_after_ui_read_failure": ("화면 재시도", "warning"),
    "requeued_after_app_restart": ("앱 재시작 대기", "warning"),
    "restart_requested": ("워커 재시작 대기", "info"),
    "restarting_emulator": ("에뮬레이터 재부팅 중", "info"),
    "backing_off": ("재시도 대기", "warning"),
    "stopped": ("워커 종료", "danger"),
    "paused": ("일시정지", "warning"),
}


class Snapshot:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.cache: dict[str, tuple] = {}
        self.last_good: dict[str, list] = {}
        self.log_history = {}
        self.android_health = AndroidHealth()
        self.web_error_records = {}

    def active_web_error(self, dataset_id, local, scheduler, saved_at):
        active = []
        for source, status in (("local", local), ("scheduler", scheduler)):
            key = (dataset_id, source)
            message = str(status.get("last_error") or "")
            if not message:
                self.web_error_records.pop(key, None)
                continue
            # Legacy schedulers republish old errors with a fresh updated_at.
            # Failure count identifies a new failure; a heartbeat does not.
            identity = (message, status.get("started_at"), status.get("collector_failures"), status.get("last_error_at"))
            record = self.web_error_records.get(key)
            if record is None or record["identity"] != identity:
                record = {"identity": identity, "at": timestamp(status.get("last_error_at") or status.get("updated_at")), "resolved": False}
                self.web_error_records[key] = record
            if record["at"] and timestamp(saved_at) > record["at"]:
                record["resolved"] = True
            if not record["resolved"]:
                active.append((record["at"], message))
        return max(active, default=(0, ""))[1]

    def datasets(self, jobs: list[dict]) -> list[dict]:
        roots = {self.root}
        roots.update(Path(job["data_root"]) for job in jobs if job.get("data_root"))
        roots.update(path.resolve() for base in (COLLECTORS, COLLECTORS / 'data') for path in base.glob('data*' if base == COLLECTORS else '*') if path.is_dir()
                     and (path.name.startswith('refresh') or (path / '.collector/refresh_inbox').is_dir()))
        found = []
        for root in sorted(roots):
            if not root.is_relative_to(COLLECTORS):
                continue
            nested = root / ".datasets"
            for path in sorted(nested.iterdir()) if nested.is_dir() else []:
                if path.is_dir() and (path / ".collector").is_dir():
                    found.append(self.describe(path, root, path.name))
            if (root / ".collector").is_dir() or (not nested.is_dir() and (root / "collector_status.json").exists()):
                found.append(self.describe(root, root, "reels"))
        return found

    @staticmethod
    def describe(path: Path, root: Path, name: str) -> dict:
        source = 'refresh' if root.name.startswith('refresh') or (root / '.collector/refresh_inbox').is_dir() else 'collector'
        return {"id": path.relative_to(COLLECTORS).as_posix(), "name": name, "root": root, "path": path, "source": source,
                "label": {"fashion": "패션", "beauty": "뷰티", "reels": "재수집" if source == 'refresh' else "일반 수집"}.get(name, name)}

    def rows(self, dataset: dict) -> tuple[list, str]:
        path = dataset["path"]
        history = path / ".collector" / "reels_history_active.csv"
        if not history.exists():
            history = path / "reels.csv"
        journal = history.with_name(history.name + ".pending.jsonl")
        queue = path / ".collector" / "android_metric_queue"
        # Recollection results remain here after the collector consumes completed/.
        files = sorted({file for name in ("completed", "recollection_completed")
                        for file in (queue / name).glob("*.json")}, key=lambda file: (file.name, str(file)))
        stamp = (signature(history), signature(journal), signature(path / ".collector" / "korean_overrides.json"), signature(path / ".collector" / "account_countries.json"),
                 tuple((str(p), signature(p)) for p in files))
        key = dataset["id"]
        if self.cache.get(key, (None,))[0] == stamp:
            return self.cache[key][1:]
        records = {}
        try:
            if history.exists():
                with open_csv_snapshot(history, wait_seconds=3) as stream:
                    for row in csv.DictReader(stream):
                        if row.get("url") and timestamp(row.get("collected_at")) and None not in row and all(v is not None for v in row.values()):
                            records[(row["url"], row["collected_at"])] = row
            if journal.exists():
                with journal.open(encoding="utf-8-sig") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and row.get("url") and timestamp(row.get("collected_at")):
                            records.setdefault((row["url"], row["collected_at"]), row)
            for file in files:
                result = read_json(file)
                target = result.get("target") or {}
                row = records.get((target.get("url"), target.get("collected_at")))
                if row is not None and isinstance(result.get("metrics"), dict) and android_result_matches_reel(result, row):
                    for field in ("like_count", "share_count", "repost_count", "saved_count"):
                        if field in result.get("compact_fields", ()):
                            continue
                        if field in result["metrics"]:
                            row[field] = result["metrics"][field]
                    if result.get("like_count_private"):
                        row["like_count"] = "X"
                    if result.get("recollection") and "comment_count" in result["metrics"] and "comment_count" not in result.get("compact_fields", ()):
                        row["comment_count"] = result["metrics"]["comment_count"]
                    if result.get("comment_count_disabled"):
                        row["comment_count"] = "X"
                    row["android_status"] = result.get("status", "")
            originals = sorted(records.values(), key=lambda row: timestamp(row.get("collected_at")), reverse=True)
            rows = apply_korean_overrides(originals, path)
            for original, row in zip(originals, rows):
                if "korean" in original and row.get("korean_source") != "manual":
                    row["korean"] = original["korean"]
            for row in rows:
                row["android_available"] = all(row.get(field) not in (None, "") for field in ("share_count", "saved_count"))
            self.last_good[key] = rows
            warning = ""
        except (OSError, csv.Error, UnicodeError) as error:
            rows = self.last_good.get(key, [])
            warning = f"데이터 파일을 읽는 중입니다. 직전 표시를 유지합니다. ({type(error).__name__})"
            return rows, warning
        self.cache[key] = (stamp, rows, warning)
        return rows, warning

    def logs(self, dataset: dict) -> list[dict]:
        events = []
        def parse(source, index, line):
            parts = line.split(" ", 2)
            if len(parts) < 3 or not timestamp(parts[0]):
                return None
            message = parts[2].strip()
            event = message.split(" |", 1)[0]
            tone = "danger" if any(word in event for word in ("failed", "error", "stopped")) else "warning" if any(word in event for word in ("retry", "recovery", "requeued", "expired", "unreadable")) else "info"
            return {"id": f"{source}:{parts[0]}:{index}", "at": parts[0], "source": source,
                    "event": event, "message": message[:2000], "tone": tone}
        for source in ("python", "android"):
            path = dataset["path"] / ".collector" / f"{source}.log"
            checked, stamp, archived = self.log_history.get(path, (0, None, []))
            if time.monotonic() - checked > 60 and signature(path) != stamp:
                history = []
                try:
                    with path.open(encoding="utf-8-sig", errors="replace") as stream:
                        for index, line in enumerate(stream):
                            if any(word in line for word in ("worker_stopped", "worker_started", "collection_stopped", "collection_stop_requested", "collector_started")):
                                record = parse(source, index, line)
                                if record:
                                    history.append(record)
                    archived = history[-1000:]
                    self.log_history[path] = (time.monotonic(), signature(path), archived)
                except OSError:
                    pass
            events.extend(archived)
            events.extend(record for index, line in enumerate(tail(path)) if (record := parse(source, index, line)))
        unique = {(event["source"], event["at"], event["message"]): event for event in events}
        return sorted(unique.values(), key=lambda event: timestamp(event["at"]), reverse=True)

    def detail(self, dataset: dict) -> dict:
        path, root = dataset["path"], dataset["root"]
        rows, warning = self.rows(dataset)
        scheduler = read_json(root / f"{'' if dataset['name'] == 'fashion' else dataset['name'] + '_'}collector_status.json")
        local = read_json(path / "collector_status.json")
        lock = read_json(root / ".datasets" / "fashion_beauty_scheduler.lock.json")
        if not lock:
            lock = read_json(path / "collector.lock.json") or local
        web_process = owner_info(lock)
        worker = path / ".collector" / "android_metric_queue"
        android = read_json(worker / "status.json")
        android_process = owner_info(read_json(worker / "worker.lock.json") or android)
        logs = self.logs(dataset)
        web_saved = next((e["at"] for e in logs if e["source"] == "python" and e["event"] == "reel_saved"), "")
        web_saved = web_saved or (rows[0].get("collected_at", "") if rows else "")
        web_error = self.active_web_error(dataset["id"], local, scheduler, web_saved)
        android_saved = next((e["at"] for e in logs if e["source"] == "android" and e["event"] == "reel_metric_collected"), "")
        last_activity = {source: next((e["at"] for e in logs if e["source"] == source), "") for source in ("python", "android")}
        paused = (COLLECTORS / "runtime" / ".collection-paused").exists()
        web_state, web_tone = "실행 확인 필요", "warning"
        if web_process["alive"] is False:
            web_state, web_tone = "수집기 종료", "danger"
        elif paused:
            web_state, web_tone = "일시정지", "warning"
        elif web_process["alive"] is True:
            recent = max(timestamp(last_activity["python"]), timestamp(local.get("updated_at")))
            web_state, web_tone = ("수집 진행 중", "good") if time.time() - recent < 180 else ("실행 중 · 진행 확인", "warning")
            if time.time() - recent < 180 and time.time() - timestamp(web_saved) >= 180:
                web_state, web_tone = "실행 중 · 새 저장 대기", "info"
            if local.get("state") in {"cooldown", "rate_limited", "retry_wait"}:
                web_state, web_tone = "재시도 대기", "warning"
            elif timestamp(scheduler.get("discovery_ends_at")) and time.time() > timestamp(scheduler["discovery_ends_at"]):
                web_state, web_tone = "예약 재수집 대기/처리", "info"
            elif local.get("state") in {"completed", "stopped", "finished"}:
                web_state, web_tone = "실행 중 · 다음 작업 대기", "info"
        android_state, android_tone = ANDROID_STATES.get(android.get("state"), ("상태 확인 필요", "warning"))
        android_error = android.get("device_error", "")
        success = next((e for e in logs if e["source"] == "android" and e["event"] == "reel_metric_collected"), {})
        issue = next((e for e in logs if e["source"] == "android" and any(word in e["event"] for word in ("failed", "unreadable", "dump_timeout", "stopped", "recovery_requested"))), {})
        if android_process["alive"] is True and timestamp(success.get("at")) > max(timestamp(android.get("updated_at")), timestamp(issue.get("at"))):
            android_state, android_tone, android_error = "처리 중", "good", ""
        if scheduler.get("collector_mode") == "web":
            android_state, android_tone = "웹 전용 모드", "info"
        elif android_process["alive"] is False:
            android_state, android_tone = "워커 종료 · 재시작 대기", "warning"
        elif android_process["alive"] is True and time.time() - max(timestamp(android.get("updated_at")), timestamp(last_activity["android"])) > 180:
            android_state, android_tone = "실행 중 · 응답 확인", "warning"
        counts = {}
        for name in ("pending", "working", "completed"):
            try:
                counts[name] = sum(1 for file in (worker / name).glob("*.json"))
            except OSError:
                counts[name] = None
        android_alert = ""
        device_health = {"state": "not_monitored", "alert": "", "checked_at": ""}
        intentional_stop = read_json(path / ".collector" / "collection_stop.json").get("stop_requested") is True
        run_active = (web_process["alive"] is True or android_process["alive"] is True
                      or counts.get("pending") or counts.get("working"))
        if scheduler.get("collector_mode") != "web" and not paused and not intentional_stop and run_active:
            serial = str(android.get("device_id") or "").strip()
            if not serial:
                launch = next((event for event in logs if event["source"] == "android" and event["event"] == "worker_started"
                               and re.search(rf"\bpid={android_process['pid']}\b", event["message"])), {})
                match = re.search(r"\bdevice_id=([^\s|]+)", launch.get("message", ""))
                serial = match[1] if match and match[1] != "default" else ""
            device_health = self.android_health.check(serial)
            android_alert = device_health.get("alert", "")
            if android_alert:
                android_state, android_tone = android_alert, "danger"
                android_error = device_health.get("detail", "")
            elif device_health["state"] in {"unknown", "booting", "unresponsive"}:
                android_state, android_tone = device_health["label"], "warning"
                android_error = device_health.get("detail", "")
        current = time.time()
        bucket_start = int(current // 300) * 300
        chart_times = [bucket_start - index * 300 for index in range(11, -1, -1)]
        session_start = timestamp(scheduler.get("started_at"))
        session_end = timestamp(scheduler.get("ends_at")) or current
        session_new_count = len({row["url"] for row in rows
                                 if str(row.get("collection_number")).strip() == "1"
                                 and session_start <= timestamp(row.get("collected_at")) <= session_end}) if session_start else None
        round_counts = Counter()
        for row in rows:
            value = str(row.get("collection_number") or "").strip()
            round_counts[int(value) if value.isdecimal() and int(value) > 0 else 0] += 1
        def bins(source):
            times = [timestamp(r.get("collected_at")) for r in rows] if source == "python" else [timestamp(e["at"]) for e in logs if e["event"] == "reel_metric_collected"]
            values = Counter(int(value // 300) * 300 for value in times if chart_times[0] <= value <= current)
            return [values[start] for start in chart_times]
        return {"id": dataset["id"], "name": dataset["name"], "label": dataset["label"], "source": dataset.get('source', 'collector'),
                "refresh": refresh_summary(path) if dataset.get('source') == 'refresh' else None,
                "rows": rows, "warning": warning, "logs": logs, "chart_times": chart_times,
                "summary": {"unique": len({r.get('url') for r in rows}), "snapshots": len(rows),
                            "session_new_count": session_new_count,
                            "rounds": [{"round": number or None, "count": round_counts[number]}
                                       for number in sorted(round_counts, key=lambda number: (number == 0, number))],
                            "last_hour": sum(timestamp(r.get("collected_at")) >= current - 3600 for r in rows),
                            "last_collected_at": rows[0].get("collected_at") if rows else "", "queue": counts},
                "web": {"label": web_state, "tone": web_tone, "process": web_process, "last_success": web_saved,
                        "last_activity": last_activity["python"], "updated_at": local.get("updated_at") or scheduler.get("updated_at"),
                        "error": web_error, "bins": bins("python")},
                "android": {"label": android_state, "tone": android_tone, "process": android_process, "last_success": android_saved,
                            "last_activity": last_activity["android"], "updated_at": android.get("updated_at"),
                            "error": android_error, "raw_state": android.get("state", ""), "alert": android_alert, "device_health": device_health,
                            "processed": android.get("processed", 0), "bins": bins("android")},
                "schedule": {key: scheduler.get(key) for key in ("started_at", "ends_at", "discovery_ends_at", "collector_mode", "active_keywords", "due_recollections", "updated_at")}}
