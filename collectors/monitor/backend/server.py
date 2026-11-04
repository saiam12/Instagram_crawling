"""Personal, loopback-only collector monitor. Uses Python's standard library."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from state import BASE, COLLECTORS, Snapshot, now, owner_info, process_info, project_korean, read_json, tail, timestamp, write_json
from classification import korean_selection, matches_korean
from src.reels.korean_classification import save_korean_override

RUNTIME = BASE.parent / "runtime" / "monitor"
TOKEN = secrets.token_urlsafe(32)
CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
sys.path.insert(0, str(COLLECTORS))
from visualization_data import MAX_UPLOAD_BYTES, read_visualization_file
from recommendations import DATASET_FILE, Recommendations
from analysis_data import read_analysis

VISUALIZATION_SLOTS = threading.BoundedSemaphore(1)
RECOMMENDATION_SEARCH_SLOTS = threading.BoundedSemaphore(1)


def data_root(value: str) -> Path:
    path = (COLLECTORS / value).resolve()
    if not path.is_relative_to(COLLECTORS) or path == COLLECTORS:
        raise ValueError("저장 폴더는 collectors 안에 지정하세요.")
    if not path.relative_to(COLLECTORS).parts[0].startswith("data"):
        raise ValueError("저장 폴더는 data/collector 또는 data/refresh처럼 data 폴더 안에 지정하세요.")
    return path


def number(value, label, minimum, maximum) -> float:
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{label}에 숫자를 입력하세요.") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{label}은 {minimum}~{maximum} 범위로 입력하세요.")
    return value


def settings(payload: dict) -> dict:
    command = payload.get("command", "fashion")
    mode = payload.get("mode", "hybrid")
    if command not in {"fashion", "beauty", "fashion-beauty"} or mode not in {"web", "hybrid"}:
        raise ValueError("지원하는 수집 종류와 모드를 선택하세요.")
    duration = number(payload.get("duration", 24), "전체 실행 시간", 0.05, 720)
    discovery = number(payload.get("discovery", 4), "신규 탐색 시간", 0.01, duration)
    interval = number(payload.get("interval", 240), "재수집 간격", 1, 10080)
    snapshots = number(payload.get("snapshots", 4), "목표 수집 횟수", 1, 100)
    if not snapshots.is_integer():
        raise ValueError("목표 횟수는 정수로 입력하세요.")
    avd = str(payload.get("avd", "")).strip()
    device = str(payload.get("device", "")).strip()
    if avd and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", avd):
        raise ValueError("AVD 이름에는 영문·숫자·밑줄·점·하이픈만 사용할 수 있습니다.")
    if device and not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,150}", device):
        raise ValueError("기기 serial을 확인하세요.")
    root = data_root(str(payload.get("data_dir", "data/collector")))
    args = [command, "--collector-mode", mode, "--duration-hours", str(duration),
            "--discovery-hours", str(discovery),
            "--recollection-interval-minutes", str(interval), "--target-snapshots", str(int(snapshots)),
            "--data-dir", str(root), "--background"]
    if device:
        args += ["--android-device-id", device]
    return {"args": args, "data_root": str(root), "command": command, "mode": mode, "avd": avd, "device": device}


class Monitor:
    def __init__(self, root: Path):
        self.snapshot = Snapshot(root)
        self.recommendations = Recommendations()
        self.lock = threading.RLock()
        self.details = {}
        self.last_error = ""
        self.updated_at = ""
        self.previous = {}
        self.events = deque(maxlen=1000)
        self.environment_cache = (0.0, {})
        self.stop = threading.Event()
        RUNTIME.mkdir(parents=True, exist_ok=True)
        for line in tail(RUNTIME / "events.jsonl", 524288):
            try:
                self.events.append(json.loads(line))
            except ValueError:
                pass
        self.event("모니터 시작", "이 시각부터 상태 변화를 감지합니다. 이전 종료 이력은 수집 로그에서 확인하세요.")

    def event(self, title: str, message: str, source="monitor", tone="info", dataset=""):
        event = {"id": uuid.uuid4().hex, "at": now(), "event": title, "message": message,
                 "source": source, "tone": tone, "dataset": dataset}
        self.events.append(event)
        with (RUNTIME / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def jobs(self) -> list[dict]:
        result = []
        for path in (RUNTIME / "runs").glob("*.json"):
            job = read_json(path)
            if not job.get("id"):
                continue
            if not job.get("pid"):
                job["pid"] = read_json(path.with_suffix(".launch")).get("pid", 0)
            info = process_info(job.get("pid"), job.get("started_at", ""))
            job["alive"] = info["alive"]
            if job.get("state") in {"queued", "starting", "running"} and path.with_suffix(".stop").exists():
                job["state"] = "stopping"
            if job.get("state") in {"queued", "starting", "running", "stopping"} and info["alive"] is False:
                job["state"] = "exit_unknown"
            job["output"] = "\n".join(tail(path.with_suffix(".log"), 16000)[-30:])
            result.append(job)
        return sorted(result, key=lambda job: job.get("created_at", ""), reverse=True)

    def sample(self):
        with self.lock:
            jobs = self.jobs()
            details = {}
            for dataset in self.snapshot.datasets(jobs):
                detail = self.snapshot.detail(dataset)
                details[dataset["id"]] = detail
                for source in ("web", "android"):
                    state = detail[source]
                    key = (dataset["id"], source)
                    stamp = (state["label"], state["process"]["pid"], state["process"]["alive"])
                    if self.previous.get(key) != stamp:
                        message = f"{detail['label']} · {state['label']} · PID {state['process']['pid'] or '확인 불가'}"
                        self.event("상태 감지", message, source, state["tone"], dataset["id"])
                        self.previous[key] = stamp
            self.details = details
            self.updated_at = now()
            self.last_error = ""
        self.ensure_android_diagnostics()

    def ensure_android_diagnostics(self):
        """Keep the read-only recorder alive while an Android worker is observed."""
        if os.name != "nt" or time.monotonic() - getattr(self, "diagnostics_checked", float("-inf")) < 60:
            return
        self.diagnostics_checked = time.monotonic()
        with self.lock:
            active = {key for key, detail in self.details.items()
                      if detail["android"]["process"]["pid"] and detail["android"]["process"]["alive"] is True}
            dataset = next((item for item in self.snapshot.datasets(self.jobs()) if item["id"] in active), None)
        if dataset is None:
            return
        root = COLLECTORS / "runtime" / "diagnostics" / "android-watch"
        current = read_json(root / "current.json")
        if current.get("directory"):
            session = Path(current["directory"]).resolve()
            if session.is_relative_to(root.resolve()) and (session / "stop.request").exists():
                return  # An explicit recorder stop remains in effect.
        info = process_info(current.get("pid"), current.get("process_started_at", ""))
        if current.get("pid") and info["alive"] is not False:
            return
        executable = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if not executable:
            return
        root.mkdir(parents=True, exist_ok=True)
        launch_id = uuid.uuid4().hex
        with (root / f"monitor-{launch_id}.out.log").open("ab") as stdout, (root / f"monitor-{launch_id}.err.log").open("ab") as stderr:
            child = subprocess.Popen([
                executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                str(COLLECTORS / "scripts" / "diagnostics" / "watch-android-diagnostics.ps1"),
                "-DataDir", str(dataset["path"]), "-OutputRoot", str(root),
            ], stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                creationflags=CREATION_FLAGS)
        with self.lock:
            self.event("Android 진단 기록 요청", f"읽기 전용 기록기 PID {child.pid}. 에뮬레이터나 수집기를 재시작하지 않습니다.", "android", dataset=dataset["id"])

    def loop(self):
        while not self.stop.is_set():
            try:
                self.sample()
            except Exception as error:
                with self.lock:
                    self.last_error = f"상태 파일을 읽지 못했습니다. 직전 화면을 유지합니다: {error}"
            self.stop.wait(3)

    def overview(self, korean="all", source="collector"):
        korean_selection(korean)
        if source not in {"collector", "refresh"}:
            raise ValueError("수집기 또는 Refresh 탭을 선택하세요.")
        with self.lock:
            datasets = [project_korean(detail, korean) for detail in self.details.values() if detail.get('source', 'collector') == source]
            jobs = self.jobs()
            busy = any(detail["web"]["process"]["alive"] is not False and detail["web"]["process"]["pid"] for detail in self.details.values())
            busy = busy or any(job.get("kind") == "collector" and job.get("state") in {"queued", "starting", "running", "stopping"} and job["alive"] is not False for job in jobs)
            return {"updated_at": self.updated_at, "server_time": now(), "datasets": datasets, "jobs": jobs[:15], "korean": korean,
                    "error": self.last_error, "collector_busy": bool(busy), "data_dir": str(self.snapshot.root.relative_to(COLLECTORS))}

    def recommendation_search(self, payload):
        return self.recommendations.search(payload)

    def set_korean(self, payload):
        value = payload.get("korean")
        if "korean" not in payload or (value is not None and type(value) is not bool):
            raise ValueError("한국·외국·미확인(-) 중에서 선택하세요.")
        reel_url = payload.get("url")
        def identity(url):
            match = re.fullmatch(r"https://(?:www\.)?instagram\.com/reels?/([A-Za-z0-9_-]+)/?(?:[?#].*)?", str(url or ""), re.I)
            return match[1] if match else ""
        code = identity(reel_url)
        if not code:
            raise ValueError("저장된 Instagram 릴스 주소를 확인하세요.")
        with self.lock:
            dataset = next((item for item in self.snapshot.datasets(self.jobs()) if item["id"] == payload.get("dataset")), None)
            if dataset is None or not dataset["path"].resolve().is_relative_to(COLLECTORS.resolve()):
                raise ValueError("기존 데이터셋을 선택한 뒤 다시 저장하세요.")
            rows, warning = self.snapshot.rows(dataset)
            if warning:
                raise ValueError("데이터 파일을 읽는 중입니다. 잠시 후 다시 저장하세요.")
            if not any(identity(row.get("url")) == code for row in rows):
                raise ValueError("선택한 데이터셋에 저장된 릴스만 수정할 수 있습니다.")
        result = save_korean_override(dataset["path"], reel_url, value)
        with self.lock:
            self.snapshot.cache.pop(dataset["id"], None)
            self.sample()
            return {**result, "korean": value, "korean_source": "manual", "message": "릴스 구분을 저장했습니다. 모든 수집 회차에 적용됩니다."}

    def environment(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.environment_cache[0] < 20:
                return self.environment_cache[1]
        from src.reels.android_reel_metrics import default_adb_path
        adb = default_adb_path()
        roots = [adb.parent.parent, *(Path(os.environ[name]) for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT") if os.environ.get(name))]
        emulator = next((root / "emulator" / "emulator.exe" for root in roots if (root / "emulator" / "emulator.exe").is_file()), None)
        emulator = str(emulator or shutil.which("emulator.exe") or "")
        result = {"avds": [], "devices": [], "adb_available": adb.is_file(), "error": ""}
        try:
            if adb.is_file():
                process = subprocess.run([str(adb), "devices"], capture_output=True, text=True, errors="replace", timeout=12, creationflags=CREATION_FLAGS)
                if process.returncode:
                    result["error"] = (process.stderr or process.stdout).strip()[:1000]
                result["devices"] = [parts[0] for line in process.stdout.splitlines() if len(parts := line.split()) >= 2 and parts[1] == "device"]
            if emulator:
                process = subprocess.run([emulator, "-list-avds"], capture_output=True, text=True, errors="replace", timeout=12, creationflags=CREATION_FLAGS)
                if not process.returncode:
                    result["avds"] = [line.strip() for line in process.stdout.splitlines() if re.fullmatch(r"[A-Za-z0-9_.-]+", line.strip())]
        except (OSError, subprocess.TimeoutExpired) as error:
            result["error"] = str(error)
        with self.lock:
            self.environment_cache = (time.monotonic(), result)
        return result

    def start(self, payload: dict, kind: str):
        config = settings(payload if kind == "collector" else {"avd": payload.get("avd", ""), "device": payload.get("device", ""),
                                                              "data_dir": str(self.snapshot.root)})
        with self.lock:
            self.sample()
            if kind == "collector" and self.overview()["collector_busy"]:
                raise ValueError("이미 실행 중인 수집기가 있습니다. 같은 브라우저 프로필의 중복 실행을 막았습니다.")
            if kind == "android" and any(job.get("kind") == "android" and job.get("alive") is not False and job.get("state") in {"queued", "starting", "running"} for job in self.jobs()):
                raise ValueError("Android 준비 작업이 이미 실행 중입니다.")
            if kind == "android" or config["mode"] == "hybrid":
                environment = self.environment(force=True)
                if not environment["adb_available"]:
                    raise ValueError("ADB를 찾지 못했습니다. Android SDK 설치와 환경변수를 확인하세요.")
                if config["device"] and config["device"] not in environment["devices"]:
                    raise ValueError("선택한 기기가 온라인 상태가 아닙니다. 기기를 켜고 연결 목록을 새로고침하세요.")
                if not environment["devices"] and config["avd"] not in environment["avds"] and len(environment["avds"]) != 1:
                    raise ValueError("시작할 Android 에뮬레이터를 선택하세요.")
            identifier = uuid.uuid4().hex
            path = RUNTIME / "runs" / f"{identifier}.json"
            job = {**config, "id": identifier, "kind": kind, "state": "queued", "created_at": now(), "pid": 0}
            write_json(path, job)
            try:
                with path.with_suffix(".log").open("ab") as output:
                    process = subprocess.Popen([sys.executable, "-u", "-B", str(BASE / "backend" / "runner.py"), identifier],
                                               cwd=COLLECTORS, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                               creationflags=CREATION_FLAGS, env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
                write_json(path.with_suffix(".launch"), {"pid": process.pid})
                # runner writes its real PID (Windows venv launchers can create a child).
                self.event("수집 시작 요청" if kind == "collector" else "Android 준비 요청", f"{config['command']} · {config['mode']} · 실행 번호 {identifier[:8]}")
                return {"id": identifier, "message": "실행을 요청했습니다. 아래 실행 기록에서 진행 상태를 확인하세요.", "launcher_pid": process.pid}
            except OSError as error:
                write_json(path, {**job, "state": "failed", "finished_at": now(), "error": str(error)})
                raise ValueError(f"실행하지 못했습니다: {error}") from error

    def request_stop(self, identifier: str):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("실행 번호가 올바르지 않습니다.")
        with self.lock:
            path = RUNTIME / "runs" / f"{identifier}.json"
            job = read_json(path)
            if job.get("kind") != "collector" or job.get("state") not in {"starting", "running", "stopping"}:
                raise ValueError("이 화면에서 실행 중인 수집기만 중지할 수 있습니다.")
            if owner_info(job)["alive"] is not True:
                raise ValueError("대상 수집기의 실행 상태를 확인할 수 없습니다.")
            write_json(path.with_suffix(".stop"), {"requested_at": now()})
            self.event("수집 중지 요청", f"실행 번호 {identifier[:8]} · 현재 저장을 마무리하고 종료합니다.", tone="warning")
            return {"message": "중지를 요청했습니다. 저장과 종료가 완료될 때까지 실행 기록을 확인하세요."}


class Handler(BaseHTTPRequestHandler):
    def allowed_host(self):
        return self.headers.get("Host", "") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def send(self, value, status=200, mime="application/json; charset=utf-8"):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed_host():
            self.send({"error": "이 PC의 localhost 주소로 접속하세요."}, 403)
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                self.send({"app": "collector-monitor", "data_root": str(self.server.monitor.snapshot.root)})
            elif parsed.path == "/api/overview":
                self.send(self.server.monitor.overview(query.get("korean", ["all"])[0], query.get("source", ["collector"])[0]))
            elif parsed.path == "/api/recommendation/status":
                self.send(self.server.monitor.recommendations.configuration())
            elif parsed.path == "/api/recommendation/dataset":
                self.send(self.server.monitor.recommendations.dataset())
            elif parsed.path == "/api/recommendation/guide":
                query = parse_qs(parsed.query)
                self.send(self.server.monitor.recommendations.guide(query.get("filename", [""])[0]))
            elif parsed.path == "/api/recommendation/video-plan":
                self.send(self.server.monitor.recommendations.video_plan(query.get("filename", [""])[0]))
            elif parsed.path == "/api/recommendation/video":
                path = self.server.monitor.recommendations.video(query.get("filename", [""])[0])
                size = path.stat().st_size
                start, end, partial = 0, size-1, False
                requested = self.headers.get("Range")
                if requested:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                    if not match or not any(match.groups()):
                        self.send({"error":"영상 범위를 확인하세요."},416); return
                    left, right = match.groups()
                    start = int(left) if left else max(0,size-int(right))
                    end = min(size-1,int(right)) if left and right else size-1
                    if start > end or start >= size:
                        self.send_response(416); self.send_header("Content-Range",f"bytes */{size}"); self.end_headers(); return
                    partial = True
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type","video/mp4")
                self.send_header("Content-Length",str(end-start+1))
                self.send_header("Accept-Ranges","bytes")
                self.send_header("Cache-Control","private, max-age=86400")
                self.send_header("X-Content-Type-Options","nosniff")
                if partial: self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
                self.end_headers()
                with path.open("rb") as stream:
                    stream.seek(start); remaining = end-start+1
                    while remaining:
                        chunk = stream.read(min(65536,remaining))
                        if not chunk: break
                        self.wfile.write(chunk); remaining -= len(chunk)
            elif parsed.path == "/api/analysis":
                self.send(read_analysis(DATASET_FILE))
            elif parsed.path == "/api/recommendation/job":
                query = parse_qs(parsed.query)
                self.send(self.server.monitor.recommendations.job(query.get("id", [""])[0]))
            elif parsed.path == "/api/environment":
                self.send(self.server.monitor.environment())
            elif parsed.path in {"/api/data", "/api/events"}:
                with self.server.monitor.lock:
                    selected = self.server.monitor.details.get(query.get("dataset", [""])[0])
                    if selected and "source" in query and selected.get('source', 'collector') != query['source'][0]:
                        selected = None
                    if not selected:
                        self.send({"items": [], "total": 0, "page": 1, "pages": 1})
                        return
                    if parsed.path == "/api/events":
                        observed = [event for event in self.server.monitor.events if not event.get("dataset") or event["dataset"] == selected["id"]]
                        logs = [event for event in selected["logs"] if event["event"] not in {"reel_saved", "reel_metric_collected", "reel_metric_started"}]
                        items = sorted(observed + logs, key=lambda event: timestamp(event["at"]), reverse=True)
                        self.send({"items": items[:100]})
                        return
                    text = query.get("q", [""])[0].strip().casefold()[:200]
                    category = query.get("filter", ["all"])[0]
                    size = int(number(query.get("size", [20])[0], "페이지 크기", 10, 100))
                    korean = korean_selection(query.get("korean", ["all"])[0])
                    rows = [row for row in selected["rows"] if matches_korean(row, korean)]
                    collection_round = query.get("round", ["all"])[0]
                    if collection_round != "all":
                        if collection_round != "unknown" and (not collection_round.isdecimal() or int(collection_round) < 1):
                            raise ValueError("수집 회차를 확인하세요.")
                        def matches_round(row):
                            value = str(row.get("collection_number") or "").strip()
                            number = int(value) if value.isdecimal() and int(value) > 0 else None
                            return number is None if collection_round == "unknown" else number == int(collection_round)
                        rows = [row for row in rows if matches_round(row)]
                    if text:
                        rows = [row for row in rows if text in " ".join(str(row.get(key, "")) for key in ("username", "title", "hashtags", "url")).casefold()]
                    if category in {"available", "missing"}:
                        rows = [row for row in rows if bool(row["android_available"]) == (category == "available")]
                    pages = max(1, math.ceil(len(rows) / size))
                    page = min(pages, int(number(query.get("page", [1])[0], "페이지", 1, 1000000)))
                    self.send({"items": rows[(page - 1) * size:page * size], "total": len(rows), "page": page, "pages": pages, "warning": selected["warning"]})
            elif parsed.path in {"/", "/index.html", "/app.js", "/theme.js", "/styles.css", "/visualize.html", "/visualize.js", "/visualize.css", "/recommend.html", "/recommend.js", "/recommend.css", "/analysis.html", "/analysis.js", "/analysis.css", "/workspace-cache.js", "/korean-filter.js", "/scatter.js", "/chart-board.js"}:
                name = parsed.path.lstrip("/") or "index.html"
                body = (BASE / "web" / name).read_bytes()
                if name in {"index.html", "visualize.html", "recommend.html", "analysis.html"}:
                    body = body.replace(b"__MONITOR_TOKEN__", TOKEN.encode())
                self.send(body, mime={".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}[Path(name).suffix])
            elif parsed.path == "/favicon.ico":
                self.send(b"", 204, "image/x-icon")
            else:
                self.send({"error": "페이지를 찾을 수 없습니다."}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OSError) as error:
            self.send({"error": str(error)}, 400)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        expected = f"http://{self.headers.get('Host', '')}"
        if not self.allowed_host() or origin != expected or not secrets.compare_digest(self.headers.get("X-Monitor-Token", ""), TOKEN):
            self.send({"error": "이 화면을 새로고침한 뒤 다시 요청하세요."}, 403)
            return
        if self.path == "/api/visualize":
            if not VISUALIZATION_SLOTS.acquire(blocking=False):
                self.send({"error": "다른 파일을 읽고 있습니다. 잠시 후 다시 선택하세요."}, 429)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_UPLOAD_BYTES:
                    raise ValueError("비어 있지 않은 20 MiB 이하 파일을 선택하세요.")
                if self.headers.get("Content-Type", "").split(";")[0] != "application/octet-stream":
                    raise ValueError("파일 요청 형식을 확인하세요. 화면을 새로고침한 뒤 다시 선택하세요.")
                self.connection.settimeout(30)
                content = self.rfile.read(length)
                if len(content) != length:
                    raise ValueError("파일 전송이 완료되지 않았습니다. 다시 선택하세요.")
                self.send(read_visualization_file(content, unquote(self.headers.get("X-File-Name", ""))[:300]))
            except (ValueError, OSError) as error:
                self.send({"error": str(error)}, 400)
            finally:
                VISUALIZATION_SLOTS.release()
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send({"error": "JSON 요청만 지원합니다."}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("요청 크기를 확인하세요.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("요청 형식이 올바르지 않습니다.")
            if self.path == "/api/preview":
                config = settings(payload)
                command = [".\\collector.ps1"] + (["-Avd", config["avd"]] if config["avd"] else []) + config["args"]
                self.send({"command": subprocess.list2cmdline(command)})
            elif self.path in {"/api/start", "/api/android"}:
                self.send(self.server.monitor.start(payload, "collector" if self.path == "/api/start" else "android"), 202)
            elif self.path == "/api/stop":
                self.send(self.server.monitor.request_stop(str(payload.get("id", ""))), 202)
            elif self.path == "/api/korean":
                self.send(self.server.monitor.set_korean(payload))
            elif self.path == "/api/recommendation/search":
                if not RECOMMENDATION_SEARCH_SLOTS.acquire(blocking=False):
                    self.send({"error": "다른 사례 검색이 진행 중입니다. 잠시 후 다시 시도하세요."}, 429)
                    return
                try:
                    self.send(self.server.monitor.recommendation_search(payload))
                finally:
                    RECOMMENDATION_SEARCH_SLOTS.release()
            elif self.path == "/api/recommendation/generate":
                self.send(self.server.monitor.recommendations.start(payload), 202)
            else:
                self.send({"error": "지원하지 않는 작업입니다."}, 404)
        except (ValueError, OSError, RuntimeError) as error:
            self.send({"error": str(error)}, 400)

    def log_message(self, format, *args):
        if len(args) < 2 or str(args[1]) not in {"200", "204"}:
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default="data/collector")
    options = parser.parse_args()
    if not 1024 <= options.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    monitor = Monitor(data_root(options.data_dir))
    server = ThreadingHTTPServer(("127.0.0.1", options.port), Handler)
    server.daemon_threads = True
    server.monitor = monitor
    write_json(RUNTIME / "server.json", {"pid": os.getpid(), "started_at": now(), "port": options.port})
    monitor.sample()
    threading.Thread(target=monitor.loop, daemon=True).start()
    print(f"Collector monitor: http://127.0.0.1:{options.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set()
        with monitor.lock:
            monitor.event("모니터 종료", "수집기는 별도로 계속 실행됩니다.")
        server.server_close()


if __name__ == "__main__":
    main()
