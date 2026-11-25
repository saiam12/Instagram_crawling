"""Serial supervisor for fashion and beauty collection datasets."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import re
import signal
import shutil
import sys
import tempfile
import time
import traceback
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Sequence

from .fashion_beauty_scheduler import (
    DatasetConfig,
    DueJob,
    RunConfig,
    due_jobs,
    initial_count_in_window,
    hashtag_at,
    window_dataset,
)
from . import collection_pause
from .korean_classification import record_is_korean, apply_korean_overrides
from .android_reel_metrics import AndroidMetricResult, android_result_matches_reel, merge_android_metrics
from .browser_runtime import load_playwright, locate_browser_executable, safe_close
from .instagram_reels_browser import REEL_HISTORY_DIRECTORY, REEL_HISTORY_FILENAME, android_recovery_failure, android_metric_queue_counts, clear_collection_stop_request, enqueue_android_app_restart, reset_android_recovery_for_new_run, launch_collection_context, normalize_reel_url, parse_args, rate_limit_state_for_context, relay_new_collection_log_lines, run_collector, run_collectors_in_shared_context, wait_for_stop_or_timeout
from .android_metric_worker import start_android_metric_worker
from .process_lock import process_is_alive
from .reel_store import open_csv_snapshot
from .fashion_analyzer import analyze_qualifying_fashion_reels
from .collection_diagnostics import cumulative_collector_summaries
from src.exporters.instagram_collector import write_xlsx_workbook, sync_combined_xlsx, REEL_INTERNAL_EXPORT_FIELDS


Invocation = Callable[..., Awaitable[int]]
Clock = Callable[[], datetime]

_PUBLIC_SOURCES = {
    "reels_csv": "reels.csv",
    "reels_json": "reels.json",
    "reels_xlsx": "reels.xlsx",
    "users_csv": "users.csv",
    "users_json": "users.json",
    "users_xlsx": "users.xlsx",
}

_LOCK_READ_ATTEMPTS = 3
_LOCK_READ_RETRY_SECONDS = 0.05
_LOCK_MALFORMED_GRACE_SECONDS = 2.0
_COLLECTOR_RETRY_BASE_SECONDS = 5 * 60.0
_COLLECTOR_RETRY_MAX_SECONDS = 30 * 60.0
_COLLECTOR_RETRY_MAX_ATTEMPTS = 5
_RATE_LIMIT_RETRY_SECONDS = 20 * 60.0
_RECOLLECTION_BATCH_SIZE = 50
_RECOLLECTION_BROWSER_KEEP_SECONDS = 5 * 60


class RecollectionBrowserSession:
    """Keep the foreground browser open between nearby due recollections."""

    def __init__(self) -> None:
        self.runtime: Any = None
        self.browser: Any = None
        self.context: Any = None
        self._context_closed = False

    async def run(self, options: Sequence[Any], stop_event: asyncio.Event | None) -> list[int]:
        if self._context_closed:
            await self.close()
        if self.context is None:
            try:
                self.runtime = await load_playwright()().start()
                self.browser, self.context = await launch_collection_context(
                    self.runtime.chromium, locate_browser_executable(), options[0]
                )
                self._context_closed = False
                context = self.context
                def mark_closed(*_args: Any) -> None:
                    if self.context is context:
                        self._context_closed = True
                context.on("close", mark_closed)
            except BaseException:
                await self.close()
                raise
        results = await run_collectors_in_shared_context(
            options,
            external_stop_event=stop_event,
            shared_context=self.context,
            shared_browser=self.browser,
            shared_playwright_runtime=self.runtime,
        )
        # A repeated 429 makes this context terminal. The supervisor keeps
        # the shared-profile cooldown before it is allowed to create another.
        if rate_limit_state_for_context(self.context).terminate_after_resume:
            await self.close()
        return results

    async def close(self) -> None:
        await safe_close(self.context)
        await safe_close(self.browser)
        if self.runtime is not None:
            try:
                await self.runtime.stop()
            except Exception:
                pass
        self.context = self.browser = self.runtime = None
        self._context_closed = False


@dataclass(frozen=True)
class WorkDecision:
    due_jobs: tuple[DueJob, ...]
    collected_count: int


def _due_retry_delay(attempt: int) -> float:
    """Back off failed Instagram work enough to avoid amplifying HTTP 429s."""
    exponent = min(max(0, attempt - 1), 3)
    return min(_COLLECTOR_RETRY_BASE_SECONDS * float(2**exponent), _COLLECTOR_RETRY_MAX_SECONDS)


def _collector_access_failure(dataset: DatasetConfig) -> str:
    """Read the access failure recorded by the most recent child collector."""
    try:
        payload = json.loads((dataset.data_root / "collector_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    for key in ("failure_code", "follower_last_status"):
        status = str(payload.get(key) or "").strip().casefold()
        if status in {"rate_limited", "login_required", "challenge_required"}:
            return status
    return ""


def _collector_rate_limit_delay(dataset: DatasetConfig) -> float:
    """Honor a longer server cooldown recorded by the browser collector."""
    try:
        payload = json.loads((dataset.data_root / "collector_status.json").read_text(encoding="utf-8"))
        delay = float(payload.get("retry_after_seconds") or 0)
    except (OSError, ValueError, TypeError):
        return _RATE_LIMIT_RETRY_SECONDS
    return max(_RATE_LIMIT_RETRY_SECONDS, delay) if math.isfinite(delay) else _RATE_LIMIT_RETRY_SECONDS


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def datasets(config: RunConfig) -> tuple[DatasetConfig, ...]:
    base = config.data_root / ".datasets"
    fashion_root = config.data_root if config.base_output else base / "fashion"
    beauty_root = config.data_root if config.base_output else base / "beauty"
    available = {
        "fashion": DatasetConfig("fashion", fashion_root, config.fashion_keywords, config.base_output),
        "beauty": DatasetConfig("beauty", beauty_root, config.beauty_keywords, config.base_output),
    }
    return tuple(available[name] for name in config.domains)


def active_dataset_name(config: RunConfig, started_at: datetime, now: datetime) -> Literal["fashion", "beauty"]:
    if len(config.domains) == 1:
        return config.domains[0]
    return window_dataset(started_at, now, config.discovery_interval_minutes)


def dataset_by_name(config: RunConfig, name: str) -> DatasetConfig:
    for dataset in datasets(config):
        if dataset.name == name:
            return dataset
    raise ValueError(f"Unknown dataset: {name}")


def read_history(dataset: DatasetConfig) -> list[dict[str, Any]]:
    history = dataset.data_root / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    archives = sorted(history.parent.glob(f"{history.stem}_legacy_*{history.suffix}"))
    for source in [*archives, history]:
        if not source.exists():
            continue
        with open_csv_snapshot(source) as file:
            for row in csv.DictReader(file):
                key = (str(row.get("url", "")), str(row.get("collected_at", "")), str(row.get("collection_number", "")))
                if all(key[:2]):
                    rows[key] = dict(row)
    seen_pairs = {key[:2] for key in rows}
    journal = Path(f"{history}.pending.jsonl")
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                key = (str(row.get("url", "")), str(row.get("collected_at", "")))
                if all(key) and key not in seen_pairs:
                    rows[(*key, "")] = row
                    seen_pairs.add(key)
    return apply_korean_overrides(rows.values(), dataset.data_root)


def _window_bounds_at(now: datetime, interval_minutes: float) -> tuple[datetime, datetime]:
    interval_seconds = float(interval_minutes) * 60
    if interval_seconds <= 0:
        raise ValueError("discovery_interval_minutes must be greater than zero")
    epoch = datetime(1970, 1, 1, tzinfo=now.tzinfo)
    elapsed = (now - epoch).total_seconds()
    start = epoch + timedelta(seconds=(elapsed // interval_seconds) * interval_seconds)
    return start, start + timedelta(seconds=interval_seconds)


def decide_next_work(
    config: RunConfig,
    rows: list[dict[str, Any]],
    now: datetime,
    *,
    dataset: DatasetConfig | None = None,
    window_start: datetime | None = None,
) -> WorkDecision:
    selected = dataset or datasets(config)[0]
    start, end = (
        (window_start, window_start + timedelta(minutes=config.discovery_interval_minutes))
        if window_start is not None
        else _window_bounds_at(now, config.discovery_interval_minutes)
    )
    used = initial_count_in_window(rows, start, end)
    return WorkDecision(
        due_jobs=tuple(due_jobs(selected, rows, now, timedelta(minutes=config.recollection_interval_minutes), target_snapshots=config.target_snapshots)),
        collected_count=used,
    )


def _public_root(dataset: DatasetConfig) -> Path:
    if dataset.base_output:
        return dataset.data_root
    if dataset.data_root.parent.name == ".datasets":
        return dataset.data_root.parent.parent
    return dataset.data_root.parent


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _elapsed_hours(previous_value: Any, collected_value: Any) -> str:
    try:
        previous = datetime.fromisoformat(str(previous_value).strip().replace("Z", "+00:00"))
        collected = datetime.fromisoformat(str(collected_value).strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (collected - previous).total_seconds() / 3_600)
    rounded = math.floor(elapsed * 10 + 0.5) / 10
    if elapsed > 0 and rounded == 0:
        rounded = 0.1
    displayed = f"{rounded:.1f}".rstrip("0").rstrip(".")
    return f"+{displayed}hour"


def _hour_field_name(field: str) -> str:
    return field.replace("days_since_previous", "hours_since_previous")


def _project_hour_intervals(
    kind: Literal["reels", "users"],
    fields: list[str],
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]], bool]:
    original_fields = fields
    if kind == "reels":
        fields = [field for field in fields if not any(field == name or field.endswith("_" + name) for name in REEL_INTERNAL_EXPORT_FIELDS)]
    transformed_fields = [_hour_field_name(field) for field in fields]
    has_row_interval = "days_since_previous" in fields
    snapshot_fields = {
        match.group(1): field
        for field in fields
        if (match := re.fullmatch(r"(\d+(?:st|nd|rd|th) collect)_collected_at", field))
    }
    for label, collected_field in snapshot_fields.items():
        source_elapsed = f"{label}_days_since_previous"
        hour_field = f"{label}_hours_since_previous"
        if source_elapsed not in fields and hour_field not in transformed_fields:
            transformed_fields.insert(transformed_fields.index(collected_field) + 1, hour_field)
    changed = transformed_fields != original_fields
    if not changed:
        return fields, rows, False

    projected: list[dict[str, Any]] = []
    previous_by_identity: dict[str, Any] = {}
    for source in rows:
        row = {_hour_field_name(field): source.get(field, "") for field in fields}
        if has_row_interval:
            identity = str(
                source.get("url")
                if kind == "reels"
                else source.get("user_id") or source.get("username") or ""
            )
            previous = previous_by_identity.get(identity)
            collected = source.get("collected_at", "")
            row["hours_since_previous"] = (
                _elapsed_hours(previous, collected) if previous and collected else ""
            )
            if collected:
                previous_by_identity[identity] = collected
        else:
            previous = source.get("collected_at", "")
            for label, collected_field in snapshot_fields.items():
                collected = source.get(collected_field, "")
                row[f"{label}_hours_since_previous"] = (
                    _elapsed_hours(previous, collected) if previous and collected else ""
                )
                if collected:
                    previous = collected
        projected.append({field: row.get(field, "") for field in transformed_fields})
    return transformed_fields, projected, True


def _read_csv_projection(
    source: Path, kind: Literal["reels", "users"]
) -> tuple[list[str], list[dict[str, Any]], bool]:
    with source.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        fields = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    return _project_hour_intervals(kind, fields, rows)


def _atomic_write_csv(destination: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore", lineterminator="\r\n")
            writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_xlsx(destination: Path, sheet_name: str, fields: list[str], rows: list[dict[str, Any]]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    matrix = [fields, *[[str(row.get(field, "") or "") for field in fields] for row in rows]]
    updated = destination.with_name(f"{destination.stem}_updated{destination.suffix}")
    try:
        write_xlsx_workbook(destination, [(sheet_name, matrix)])
    except PermissionError:
        write_xlsx_workbook(updated, [(sheet_name, matrix)])
        return updated
    try:
        updated.unlink(missing_ok=True)
    except PermissionError:
        pass
    return destination


def dataset_publication_signature(dataset: DatasetConfig) -> tuple:
    """Track exporter inputs, including results written by the Android worker."""
    history = dataset.data_root / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    sources = [history, history.parent / "account_countries.json", history.parent / "korean_overrides.json",
               *(dataset.data_root / name for name in _PUBLIC_SOURCES.values())]
    completed = history.parent / "android_metric_queue" / "completed"
    sources.extend(sorted(completed.glob("*.json")))
    signature = []
    for source in sources:
        try:
            stat = source.stat()
        except FileNotFoundError:
            signature.append((str(source), None, None))
        else:
            signature.append((str(source), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def publish_dataset_outputs(dataset: DatasetConfig) -> dict[str, Path]:
    """Atomically publish only domain-prefixed exports from one workspace."""
    public_root = _public_root(dataset)
    written: dict[str, Path] = {}
    projections: dict[str, tuple[list[str], list[dict[str, Any]], bool]] = {}
    reel_history = dataset.data_root / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME

    # The generic collector creates a wide, one-row-per-Reel public projection.
    # Scheduled domains retain one row per snapshot; account properties and
    # explicit classifications are shared across that user's snapshots.
    if reel_history.exists():
        rows = read_history(dataset)
        completed_dir = dataset.data_root / REEL_HISTORY_DIRECTORY / "android_metric_queue" / "completed"
        by_snapshot = {(str(row.get("url", "")), str(row.get("collected_at", ""))): row for row in rows}
        for completed in sorted(completed_dir.glob("*.json")):
            try:
                result = json.loads(completed.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(result, dict):
                continue
            target = result.get("target") if isinstance(result.get("target"), dict) else {}
            row = by_snapshot.get((str(target.get("url", "")), str(target.get("collected_at", ""))))
            if row is None:
                continue
            metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
            if metrics and not android_result_matches_reel(result, row):
                continue
            row.update(merge_android_metrics(row, AndroidMetricResult(
                metrics=metrics,
                compact_fields=tuple(result.get("compact_fields") or ()),
                recollection=bool(result.get("recollection")),
                like_count_private=result.get("like_count_private"),
                comment_count_disabled=result.get("status") == "collected" and result.get("comment_count_disabled") is True,
            )))
            if result.get("audio_name") and not row.get("audio_name"):
                row["audio_name"] = result["audio_name"]
            if result.get("android_mismatch"):
                row["android_mismatch"] = result["android_mismatch"]
        with open_csv_snapshot(reel_history) as file:
            active_fields = list(csv.DictReader(file).fieldnames or [])
        fields = list(dict.fromkeys([*active_fields, *(field for row in rows for field in row)]))
        rows.sort(key=lambda row: (str(row.get("collected_at", "")), str(row.get("url", ""))))
        fields, rows, _changed = _project_hour_intervals("reels", fields, rows)
        destinations = {
            "reels_csv": public_root / f"{'' if dataset.name == 'fashion' else dataset.name + '_'}reels.csv",
            "reels_json": public_root / f"{'' if dataset.name == 'fashion' else dataset.name + '_'}reels.json",
            "reels_xlsx": public_root / f"{'' if dataset.name == 'fashion' else dataset.name + '_'}reels.xlsx",
        }
        _atomic_write_csv(destinations["reels_csv"], fields, rows)
        _write_json_atomic(destinations["reels_json"], [{**row, "korean": record_is_korean(row)} for row in rows])
        destinations["reels_xlsx"] = _atomic_write_xlsx(destinations["reels_xlsx"], "reels", fields, rows)
        written.update(destinations)

    for kind, source_name in _PUBLIC_SOURCES.items():
        if reel_history.exists() and kind.startswith("reels_"):
            continue
        source = dataset.data_root / source_name
        if not source.exists():
            continue
        destination = public_root / f"{'' if dataset.name == 'fashion' else dataset.name + '_'}{source_name}"
        record_kind: Literal["reels", "users"] = "reels" if kind.startswith("reels") else "users"
        if kind.endswith("_csv"):
            projection = _read_csv_projection(source, record_kind)
            projections[record_kind] = projection
            fields, rows, changed = projection
            if changed:
                _atomic_write_csv(destination, fields, rows)
            else:
                _atomic_copy(source, destination)
        elif kind == "reels_json":
            try:
                payload = json.loads(source.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, list) and all(isinstance(row, dict) for row in payload):
                json_fields = list(dict.fromkeys(field for row in payload for field in row))
                _fields, projected_rows, changed = _project_hour_intervals("reels", json_fields, payload)
                _write_json_atomic(destination, [{**row, "korean": record_is_korean(row)} for row in projected_rows])
            else:
                _atomic_copy(source, destination)
        elif kind.endswith("_xlsx") and record_kind in projections:
            fields, rows, changed = projections[record_kind]
            if changed:
                destination = _atomic_write_xlsx(destination, record_kind, fields, rows)
            else:
                _atomic_copy(source, destination)
        else:
            _atomic_copy(source, destination)
        written[kind] = destination
    if dataset.name == "fashion":
        sync_combined_xlsx(public_root)
    return written


def _write_json_atomic(destination: Path, value: Any) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for attempt in range(10):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)
    finally:
        temporary.unlink(missing_ok=True)


def write_dataset_status(dataset: DatasetConfig, state: dict[str, Any]) -> None:
    _write_json_atomic(_public_root(dataset) / f"{'' if dataset.name == 'fashion' else dataset.name + '_'}collector_status.json", state)


class SupervisorLock:
    """Long-lived lock for the supervisor, separate from collector.lock.json."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.acquired = False

    async def acquire(self) -> "SupervisorLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = {
            "pid": os.getpid(),
            "started_at": utc_now().isoformat().replace("+00:00", "Z"),
            "data_root": str(self.path.parent.parent),
        }
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(content, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                self.acquired = True
                return self
            except FileExistsError:
                existing_pid = await self._read_existing_pid()
                if existing_pid and process_is_alive(existing_pid):
                    raise RuntimeError(
                        f"Another fashion/beauty supervisor is already running (PID {existing_pid})."
                    )
                self.path.unlink(missing_ok=True)
        raise RuntimeError(f"Could not acquire supervisor lock: {self.path}")

    async def _read_existing_pid(self) -> int:
        """Read a lock owner without stealing a file another process is writing."""
        for attempt in range(_LOCK_READ_ATTEMPTS):
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                pid = int(existing.get("pid", 0))
                if pid <= 0:
                    raise ValueError("lock PID must be positive")
                return pid
            except FileNotFoundError:
                return 0
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                if attempt + 1 < _LOCK_READ_ATTEMPTS:
                    await asyncio.sleep(_LOCK_READ_RETRY_SECONDS)
        try:
            age_seconds = max(0.0, time.time() - self.path.stat().st_mtime)
        except FileNotFoundError:
            return 0
        if age_seconds <= _LOCK_MALFORMED_GRACE_SECONDS:
            raise RuntimeError("The fashion/beauty supervisor lock is still initializing; try again shortly.")
        return 0

    async def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if current.get("pid") == os.getpid():
                self.path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False

    async def __aenter__(self) -> "SupervisorLock":
        return await self.acquire()

    async def __aexit__(self, _type: object, _value: object, _traceback: object) -> None:
        await self.release()


async def invoke_generic_collector(
    *,
    config: RunConfig,
    dataset: Literal["fashion", "beauty"] | str,
    mode: Literal["discover", "recollect"] | str,
    hashtags: Sequence[str] = (),
    urls: Sequence[str] = (),
    max_items: int | None = None,
    progress_offset: int = 0,
    stop_event: asyncio.Event | None = None,
    discovery_stop_event: asyncio.Event | None = None,
) -> int:
    options, urls_file = _generic_collector_options(
        config=config,
        dataset=dataset,
        mode=mode,
        hashtags=hashtags,
        urls=urls,
        max_items=max_items,
        progress_offset=progress_offset,
    )
    options.discovery_stop_event = discovery_stop_event
    try:
        if stop_event is None:
            return await run_collector(options)
        return await run_collector(
            options,
            external_stop_event=stop_event,
            register_signal_handler=False,
        )
    finally:
        if urls_file is not None:
            urls_file.unlink(missing_ok=True)


def _generic_collector_options(
    *,
    config: RunConfig,
    dataset: Literal["fashion", "beauty"] | str,
    mode: Literal["discover", "recollect"] | str,
    hashtags: Sequence[str] = (),
    urls: Sequence[str] = (),
    max_items: int | None = None,
    progress_offset: int = 0,
) -> tuple[Any, Path | None]:
    selected = dataset_by_name(config, dataset)
    selected.data_root.mkdir(parents=True, exist_ok=True)
    arguments = [
        "--data-dir", str(selected.data_root),
        "--progress-offset", str(max(0, progress_offset)),
        "--direct-reel-info-wait-seconds", str(config.direct_reel_info_wait_seconds),
        "--exact-metric-attempts", str(config.exact_metric_attempts),
        "--exact-metric-retry-delay-seconds", str(config.exact_metric_retry_delay_seconds),
        "--hashtag-candidates-per-keyword", str(config.hashtag_candidates_per_keyword),
    ]
    if config.background:
        arguments.append("--background")
    if config.android_adb_path:
        arguments.extend(["--android-adb-path", str(config.android_adb_path)])
    if config.android_device_id:
        arguments.extend(["--android-device-id", config.android_device_id])
    if config.collector_mode == "web":
        arguments.append("--no-android-metrics")
    elif config.collect_hashtag_media_count:
        arguments.append("--collect-hashtag-media-count")
    urls_file: Path | None = None

    if mode == "discover":
        if not hashtags:
            raise ValueError("Discovery requires at least one hashtag")
        arguments.extend(
            [
                "--hashtag-query",
                " OR ".join(hashtags),
                "--max-items",
                str(0 if max_items is None else max_items),
                "--new-urls-only",
                "--followers-after-reels",
                "--max-upload-days",
                str(config.max_upload_age_days),
            ]
        )
    elif mode == "recollect":
        if not urls:
            raise ValueError("Recollection requires at least one URL")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".fashion_beauty_urls.", suffix=".txt", dir=selected.data_root
        )
        os.close(descriptor)
        urls_file = Path(temporary_name)
        urls_file.write_text("".join(f"{url}\n" for url in urls), encoding="utf-8")
        arguments.extend(
            ["--urls-file", str(urls_file), "--max-items", str(len(urls)), "--disable-recollect-cooldown"]
        )
    else:
        raise ValueError(f"Unknown collection mode: {mode}")
    options = parse_args(arguments)
    options.profile_android_recollection = (
        mode == "recollect" and config.collector_mode == "hybrid"
    )
    # The long-running scheduler owns one Android log relay for the whole run,
    # including the gaps between individual browser collection invocations.
    options.relay_detached_android_logs = False
    # Let the scheduler own the cooldown so due Android recollections can run
    # while new web discovery is paused.
    options.rate_limit_return_immediately = True
    return options, urls_file


async def invoke_generic_recollection_batches(
    *,
    config: RunConfig,
    batches: Sequence[tuple[str, Sequence[str]]],
    stop_event: asyncio.Event | None = None,
    browser_session: RecollectionBrowserSession | None = None,
) -> list[int]:
    """Collect one due-URL batch per domain in a shared authenticated browser."""
    prepared = [
        _generic_collector_options(
            config=config,
            dataset=dataset,
            mode="recollect",
            urls=urls,
        )
        for dataset, urls in batches
    ]
    try:
        options = [options for options, _urls_file in prepared]
        if browser_session is not None:
            return await browser_session.run(options, stop_event)
        if len(options) == 1:
            return [
                await run_collector(
                    options[0],
                    external_stop_event=stop_event,
                    register_signal_handler=stop_event is None,
                )
            ]
        return await run_collectors_in_shared_context(options, external_stop_event=stop_event)
    finally:
        for _options, urls_file in prepared:
            if urls_file is not None:
                urls_file.unlink(missing_ok=True)


def active_window_index(
    started_at: datetime,
    now: datetime,
    interval_minutes: float,
    *,
    alternating_domains: bool = True,
) -> int:
    interval_seconds = float(interval_minutes) * 60
    if interval_seconds <= 0:
        raise ValueError("discovery_interval_minutes must be greater than zero")
    overall_index = max(0, int((now - started_at).total_seconds() // interval_seconds))
    return overall_index // 2 + 1 if alternating_domains else overall_index + 1


def active_hashtag_position(
    config: RunConfig,
    started_at: datetime,
    now: datetime,
) -> int:
    """Fallback position when no explicit scheduler hashtag cursor is supplied."""
    if config.new_only and len(config.domains) == 1:
        return 1
    return active_window_index(
        started_at,
        now,
        config.discovery_interval_minutes,
        alternating_domains=len(config.domains) > 1,
    )


def _active_window_start(started_at: datetime, now: datetime, interval_minutes: float) -> datetime:
    interval_seconds = float(interval_minutes) * 60
    overall_index = max(0, int((now - started_at).total_seconds() // interval_seconds))
    return started_at + timedelta(seconds=overall_index * interval_seconds)


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_deadline(started_at: datetime, hours: float | None) -> datetime:
    return started_at + timedelta(hours=hours) if hours is not None else datetime.max.replace(tzinfo=started_at.tzinfo)


def current_status(
    config: RunConfig,
    dataset: DatasetConfig,
    started_at: datetime,
    discovery_ends_at: datetime,
    ends_at: datetime,
    now: datetime,
    *,
    last_error: str = "",
    collector_failures: int = 0,
    completed: bool = False,
    hashtag_position: int | None = None,
) -> dict[str, Any]:
    rows = read_history(dataset)
    active_name = active_dataset_name(config, started_at, now) if now < discovery_ends_at else None
    window_start = _active_window_start(started_at, now, config.discovery_interval_minutes)
    window_end = window_start + timedelta(minutes=config.discovery_interval_minutes)
    current_count = initial_count_in_window(rows, window_start, window_end)
    pending = (
        []
        if config.new_only
        else [
            job for job in due_jobs(
                dataset, rows, now, timedelta(minutes=config.recollection_interval_minutes),
                started_at if config.domains == ("fashion",) else None,
                config.target_snapshots,
            )
            if job.due_at >= started_at
        ]
    )
    active_keywords: Sequence[str] = ()
    if active_name == dataset.name:
        active_keywords = (hashtag_at(
            dataset.keywords,
            hashtag_position or active_hashtag_position(config, started_at, now),
        ),)
    return {
        "dataset": dataset.name,
        "collector_mode": config.collector_mode,
        "state": (
            "completed"
            if completed or now >= ends_at
            else ("new_only" if config.new_only else ("recollection_only" if now >= discovery_ends_at else "running"))
        ),
        "started_at": _isoformat(started_at),
        "discovery_ends_at": _isoformat(discovery_ends_at) if config.discovery_hours is not None else None,
        "ends_at": _isoformat(ends_at) if config.duration_hours is not None else None,
        "duration_unlimited": config.duration_hours is None,
        "discovery_unlimited": config.discovery_hours is None,
        "updated_at": _isoformat(now),
        "active_keywords": list(active_keywords),
        "max_upload_age_days": config.max_upload_age_days,
        "recollection_interval_minutes": config.recollection_interval_minutes,
        "window_collected": current_count,
        "due_recollections": len(pending),
        "overdue_recollections": sum(job.due_at < now for job in pending),
        "collector_failures": collector_failures,
        "last_error": last_error,
    }


async def _invoke_before_deadline(
    invoke: Invocation,
    clock: Clock,
    deadline: datetime,
    graceful_stop_event: asyncio.Event | None = None,
    **kwargs: Any,
) -> tuple[int, str]:
    mode = str(kwargs.get("mode", "collector"))
    remaining = (deadline - clock()).total_seconds()
    if remaining <= 0:
        return 2, f"{mode} deadline reached before collector invocation"
    task = asyncio.create_task(invoke(**kwargs)) if graceful_stop_event is not None else None
    stopped_at_deadline = False
    try:
        try:
            awaitable = asyncio.shield(task) if task is not None else invoke(**kwargs)
            # Recollection URLs have already been accepted as a finite batch.
            # The run deadline stops new batches, not enrichment of saved rows.
            result = int(await awaitable) if mode == "recollect" else int(
                await collection_pause.wait_for_active_time(awaitable, timeout=remaining)
            )
        except TimeoutError:
            if task is None:
                return 2, f"{mode} collector exceeded its deadline"
            graceful_stop_event.set()
            stopped_at_deadline = True
            result = int(await task)
    except Exception as error:
        return 2, f"{mode} collector failed: {error}"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    if mode != "recollect" and clock() > deadline and not stopped_at_deadline:
        return 2, f"{mode} collector completed after its deadline"
    if result:
        return result, f"{mode} collector exited with code {result}"
    return 0, ""


async def _invoke_recollection_batches_before_deadline(
    invoke: Invocation,
    clock: Clock,
    deadline: datetime,
    *,
    config: RunConfig,
    batches: Sequence[tuple[str, tuple[DueJob, ...]]],
    stop_event: asyncio.Event,
    browser_session: RecollectionBrowserSession | None = None,
) -> list[tuple[tuple[DueJob, ...], int, str]]:
    """Run the due batches together, preserving a separate outcome per domain."""
    if invoke is not invoke_generic_collector:
        results = await asyncio.gather(
            *(
                _invoke_before_deadline(
                    invoke,
                    clock,
                    deadline,
                    config=config,
                    dataset=dataset,
                    mode="recollect",
                    urls=[job.url for job in jobs],
                    stop_event=stop_event,
                )
                for dataset, jobs in batches
            )
        )
        return [
            (jobs, result, error)
            for (_dataset, jobs), (result, error) in zip(batches, results, strict=True)
        ]

    remaining = (deadline - clock()).total_seconds()
    if remaining <= 0:
        return [(jobs, 2, "recollect deadline reached before collector invocation") for _dataset, jobs in batches]
    try:
        results = await invoke_generic_recollection_batches(
            config=config,
            batches=[(dataset, [job.url for job in jobs]) for dataset, jobs in batches],
            stop_event=stop_event,
            browser_session=browser_session,
        )
    except Exception as error:
        return [(jobs, 2, f"recollect collector failed: {error}") for _dataset, jobs in batches]
    return [
        (
            jobs,
            result,
            "" if result == 0 else f"recollect collector exited with code {result}",
        )
        for (_dataset, jobs), result in zip(batches, results, strict=True)
    ]


async def run_fashion_beauty_collection(
    config: RunConfig,
    *,
    invoke: Invocation = invoke_generic_collector,
    clock: Clock = utc_now,
) -> int:
    started_at = config.resume_started_at or clock()
    config = replace(config, resume_started_at=started_at)
    ends_at = _run_deadline(started_at, config.duration_hours)
    stop_event = asyncio.Event()
    retry_state: dict[str, Any] = {}
    restart_count = 0
    last_error = ""

    def publish_retry_status() -> None:
        for dataset in datasets(config):
            path = _public_root(dataset) / f"{'' if dataset.name == 'fashion' else dataset.name + '_'}collector_status.json"
            try:
                status = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    status = {}
            except (OSError, ValueError):
                status = {}
            status.update(dataset=dataset.name, state="retry_wait" if clock() < ends_at else "completed_with_errors",
                          started_at=_isoformat(started_at), ends_at=_isoformat(ends_at) if config.duration_hours is not None else None, duration_unlimited=config.duration_hours is None, updated_at=_isoformat(clock()),
                          last_error=last_error, supervisor_restarts=restart_count)
            try:
                write_dataset_status(dataset, status)
            except OSError as write_error:
                print(f"[SUPERVISOR] Status write deferred: {write_error}", file=sys.stderr)
    with cumulative_collector_summaries():
        # Keep ownership and the original deadline even when a failed iteration
        # must rebuild its browser and background tasks.
        async with SupervisorLock(config.data_root / ".datasets" / "fashion_beauty_scheduler.lock.json"):
            while clock() < ends_at and not stop_event.is_set():
                try:
                    if not retry_state.get("stop_requests_reset"):
                        for dataset in datasets(config):
                            clear_collection_stop_request(dataset.data_root)
                        retry_state["stop_requests_reset"] = True
                    return await _run_fashion_beauty_collection(
                        config, invoke=invoke, clock=clock, lock_held=True,
                        lifecycle_stop_event=stop_event, retry_state=retry_state,
                    )
                except InterruptedError:
                    raise
                except Exception as error:
                    if stop_event.is_set():
                        return 0
                    restart_count += 1
                    last_error = str(error)
                    print(f"[SUPERVISOR] Iteration failed; saved work retained, retrying: {error}", file=sys.stderr)
                    traceback.print_exc()
                    remaining = max(0.0, (ends_at - clock()).total_seconds())
                    publish_retry_status()
                    if remaining:
                        await wait_for_stop_or_timeout(stop_event, min(30.0, remaining))
            if last_error:
                publish_retry_status()
            return 1 if last_error else 0


async def _run_fashion_beauty_collection(
    config: RunConfig,
    *,
    invoke: Invocation = invoke_generic_collector,
    clock: Clock = utc_now,
    lock_held: bool = False,
    lifecycle_stop_event: asyncio.Event | None = None,
    retry_state: dict[str, Any] | None = None,
) -> int:
    started_at = config.resume_started_at or clock()
    discovery_ends_at = _run_deadline(started_at, config.discovery_hours)
    ends_at = _run_deadline(started_at, config.duration_hours)
    recollection_interval = timedelta(minutes=config.recollection_interval_minutes)
    first_collected_at = started_at if config.domains == ("fashion",) else None
    stop_event = lifecycle_stop_event if lifecycle_stop_event is not None else asyncio.Event()
    retry_state = retry_state if retry_state is not None else {}
    lock_path = config.data_root / ".datasets" / "fashion_beauty_scheduler.lock.json"
    last_errors = retry_state.setdefault("last_errors", {name: "" for name in config.domains})
    collector_failures = retry_state.setdefault("collector_failures", {name: 0 for name in config.domains})
    retry_attempts = retry_state.setdefault("retry_attempts", {})
    retry_not_before = retry_state.setdefault("retry_not_before", {})
    discovery_retry_attempts = retry_state.setdefault("discovery_retry_attempts", {})
    discovery_retry_not_before = retry_state.setdefault("discovery_retry_not_before", {})
    discovery_rate_limit_not_before = retry_state.get("discovery_rate_limit_not_before")
    hashtag_positions = retry_state.setdefault("hashtag_positions", {name: 1 for name in config.domains})
    exit_code = 0
    completed_early = False
    interrupt_count = 0
    previous_sigint = signal.getsignal(signal.SIGINT)
    android_log_relays: list[asyncio.Task[None]] = []
    fashion_analyzer_task: asyncio.Task[None] | None = None
    browser_session = (
        RecollectionBrowserSession()
        if invoke is invoke_generic_collector and not config.background
        else None
    )
    published_outputs = retry_state.setdefault("published_outputs", {})
    publication_signatures = retry_state.setdefault("publication_signatures", {})
    fashion_analyzer_requested = False
    prepared_recollections = retry_state.setdefault("prepared_recollections", set())

    def request_fashion_analysis() -> None:
        """Run the high-reaction-rate analyzer without delaying collection."""
        nonlocal fashion_analyzer_task, fashion_analyzer_requested
        if "fashion" not in config.domains:
            return
        fashion_analyzer_requested = True
        if fashion_analyzer_task is None or fashion_analyzer_task.done():
            async def worker() -> None:
                nonlocal fashion_analyzer_requested
                while fashion_analyzer_requested:
                    fashion_analyzer_requested = False
                    try:
                        summary = await analyze_qualifying_fashion_reels(
                            dataset_by_name(config, "fashion").data_root,
                        )
                        if summary.get("queued"):
                            print(
                                "[ANALYZER] Fashion reaction_rate >= 9000%: "
                                f"queued={summary['queued']} succeeded={summary['succeeded']} "
                                f"failed={summary['failed']}"
                            )
                    except Exception as error:
                        print(f"[ANALYZER] Fashion analysis failed: {error}", file=sys.stderr)

            fashion_analyzer_task = asyncio.create_task(worker())

    def handle_interrupt(_signum: int, _frame: Any) -> None:
        nonlocal interrupt_count
        interrupt_count += 1
        if interrupt_count == 1:
            stop_event.set()
            print("중지 요청: 예약 수집을 멈추고 현재 저장 작업을 마무리합니다.", file=sys.stderr)
        else:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        async with (nullcontext() if lock_held else SupervisorLock(lock_path)):
            if config.collector_mode == "hybrid":
                if not retry_state.get("android_reset_done"):
                    for dataset in datasets(config):
                        if reset_android_recovery_for_new_run(dataset.data_root):
                            print(f"[{dataset.name.upper()}] 이전 Android 복구 실패 상태를 해제했습니다. 보존된 큐로 다시 시도합니다.")
                    retry_state["android_reset_done"] = True
                android_log_relays = [
                    asyncio.create_task(relay_new_collection_log_lines(dataset.data_root, "android"))
                    for dataset in datasets(config)
                ]
            request_fashion_analysis()
            while not stop_event.is_set() and clock() < ends_at:
                await collection_pause.wait()
                now = clock()
                configured_datasets = datasets(config)
                maintenance_pending = False
                if config.collector_mode == "hybrid":
                    for dataset in configured_datasets:
                        queue_counts = android_metric_queue_counts(dataset.data_root)
                        maintenance_pending |= any(queue_counts.values())
                        if queue_counts["pending"] or queue_counts["completed"]:
                            start_android_metric_worker(
                                dataset.data_root,
                                adb_path=config.android_adb_path,
                                device_id=config.android_device_id,
                            )
                histories = {dataset.name: read_history(dataset) for dataset in configured_datasets}
                pending = (
                    []
                    if config.new_only
                    else sorted(
                        (
                            job
                            for dataset in configured_datasets
                            for job in due_jobs(
                                dataset, histories[dataset.name], now, recollection_interval, first_collected_at,
                                config.target_snapshots,
                            )
                            if job.due_at >= started_at
                        ),
                        key=lambda job: (job.due_at, job.dataset, job.url),
                    )
                )
                scheduled_before_end = (
                    []
                    if config.new_only
                    else sorted(
                        (
                            job
                            for dataset in configured_datasets
                            for job in due_jobs(
                                dataset, histories[dataset.name], ends_at, recollection_interval, first_collected_at,
                                config.target_snapshots,
                            )
                            if started_at <= job.due_at < ends_at
                        ),
                        key=lambda job: (job.due_at, job.dataset, job.url),
                    )
                )

                app_restart_times: list[datetime] = []
                if config.collector_mode == "hybrid" and recollection_interval >= timedelta(hours=1):
                    for dataset in configured_datasets:
                        upcoming = next((job for job in scheduled_before_end if job.dataset == dataset.name and job.due_at > now), None)
                        if upcoming is None or (upcoming.dataset, upcoming.due_at) in prepared_recollections:
                            continue
                        restart_at = upcoming.due_at - timedelta(minutes=1)
                        if now >= restart_at:
                            enqueue_android_app_restart(dataset.data_root, upcoming.url, upcoming.due_at)
                            prepared_recollections.add((upcoming.dataset, upcoming.due_at))
                        else:
                            app_restart_times.append(restart_at)

                wait_seconds = 30.0
                invoked = False
                eligible_jobs = [
                    job
                    for job in pending
                    if retry_not_before.get((job.dataset, job.url, job.due_at), now) <= now
                ]
                if browser_session is not None and browser_session.context is not None and not eligible_jobs:
                    next_recollection_at = min(
                        [
                            retry_not_before[(job.dataset, job.url, job.due_at)]
                            for job in pending
                        ] + [job.due_at for job in scheduled_before_end if job.due_at > now],
                        default=None,
                    )
                    if (
                        now < discovery_ends_at
                        or next_recollection_at is None
                        or (next_recollection_at - now).total_seconds() > _RECOLLECTION_BROWSER_KEEP_SECONDS
                    ):
                        await browser_session.close()

                browser_retry_at = retry_state.get("browser_retry_not_before")
                if browser_retry_at is not None and now < browser_retry_at:
                    wait_seconds = min(30.0, (browser_retry_at - now).total_seconds())
                elif eligible_jobs:
                    recollection_during_discovery_rate_limit = bool(
                        discovery_rate_limit_not_before is not None
                        and discovery_rate_limit_not_before > now
                    )
                    recollection_batches = [
                        (
                            dataset.name,
                            tuple(job for job in eligible_jobs if job.dataset == dataset.name)[:_RECOLLECTION_BATCH_SIZE],
                        )
                        for dataset in configured_datasets
                        if any(job.dataset == dataset.name for job in eligible_jobs)
                    ]
                    invoked = True
                    outcomes = await _invoke_recollection_batches_before_deadline(
                        invoke,
                        clock,
                        ends_at,
                        config=config,
                        batches=recollection_batches,
                        stop_event=stop_event,
                        browser_session=browser_session,
                    )
                    for recollection_jobs, result, error in outcomes:
                        dataset_name = recollection_jobs[0].dataset
                        if result:
                            access_failure = _collector_access_failure(
                                dataset_by_name(config, dataset_name)
                            )
                            if exit_code == 0:
                                exit_code = result
                            last_errors[dataset_name] = error
                            collector_failures[dataset_name] += 1
                            wait_seconds = 0.0
                            rate_limit_retry_at: datetime | None = None
                            if access_failure == "rate_limited" or recollection_during_discovery_rate_limit:
                                rate_limit_retry_at = min(
                                    clock() + timedelta(seconds=_collector_rate_limit_delay(
                                        dataset_by_name(config, dataset_name)
                                    )),
                                    ends_at,
                                )
                                retry_state["browser_retry_not_before"] = max(
                                    retry_state.get("browser_retry_not_before") or rate_limit_retry_at,
                                    rate_limit_retry_at,
                                )
                                reason = (
                                    "Instagram rate limit detected"
                                    if access_failure == "rate_limited"
                                    else "recollection failed during discovery rate-limit cooldown"
                                )
                                last_errors[dataset_name] = f"{error}; {reason}, failed batch paused for 20 minutes"
                            for job in recollection_jobs:
                                job_key = (job.dataset, job.url, job.due_at)
                                attempt = retry_attempts.get(job_key, 0) + 1
                                retry_attempts[job_key] = attempt
                                retry_delay = _due_retry_delay(attempt)
                                if rate_limit_retry_at is not None:
                                    retry_not_before[job_key] = rate_limit_retry_at
                                    retry_delay = max(
                                        0.0,
                                        (rate_limit_retry_at - clock()).total_seconds(),
                                    )
                                elif attempt >= _COLLECTOR_RETRY_MAX_ATTEMPTS:
                                    retry_not_before[job_key] = ends_at
                                    last_errors[dataset_name] = (
                                        f"{error}; retry deferred for the rest of this run after {attempt} failures"
                                    )
                                else:
                                    retry_not_before[job_key] = clock() + timedelta(seconds=retry_delay)
                                wait_seconds = max(wait_seconds, retry_delay)
                        else:
                            last_errors[dataset_name] = ""
                            if any(job.dataset == "fashion" for job in recollection_jobs):
                                request_fashion_analysis()
                            before = Counter(
                                normalized["url"] for row in histories[dataset_name]
                                if (normalized := normalize_reel_url(row.get("url"))) is not None
                            )
                            after = Counter(
                                normalized["url"] for row in read_history(dataset_by_name(config, dataset_name))
                                if (normalized := normalize_reel_url(row.get("url"))) is not None
                            )
                            for job in recollection_jobs:
                                job_key = (job.dataset, job.url, job.due_at)
                                if after[job.url] > before[job.url]:
                                    retry_attempts.pop(job_key, None)
                                    retry_not_before.pop(job_key, None)
                                else:
                                    attempt = retry_attempts.get(job_key, 0) + 1
                                    retry_attempts[job_key] = attempt
                                    retry_not_before[job_key] = (
                                        ends_at if attempt >= _COLLECTOR_RETRY_MAX_ATTEMPTS
                                        else clock() + timedelta(seconds=_due_retry_delay(attempt))
                                    )
                                    collector_failures[dataset_name] += 1
                                    last_errors[dataset_name] = f"Exact profile metrics unavailable for {job.url}"
                elif pending:
                    wait_seconds = min(
                        30.0,
                        max(
                            0.0,
                            min(
                                (retry_not_before[(job.dataset, job.url, job.due_at)] - now).total_seconds()
                                for job in pending
                            ),
                        ),
                    )
                elif now < discovery_ends_at:
                    selected = dataset_by_name(config, active_dataset_name(config, started_at, now))
                    window_start = _active_window_start(started_at, now, config.discovery_interval_minutes)
                    window_end = min(
                        window_start + timedelta(minutes=config.discovery_interval_minutes),
                        discovery_ends_at,
                        ends_at,
                    )
                    decision = decide_next_work(
                        config,
                        histories[selected.name],
                        now,
                        dataset=selected,
                        window_start=window_start,
                    )
                    hashtag_position = hashtag_positions[selected.name]
                    window_key = (selected.name, window_start)
                    test_remaining = max(0, 10 - sum(initial_count_in_window(items, started_at, ends_at) for items in histories.values())) if config.test_mode else 0
                    retry_at = discovery_retry_not_before.get(window_key)
                    if (
                        discovery_rate_limit_not_before is not None
                        and discovery_rate_limit_not_before > now
                    ):
                        wait_seconds = min(
                            30.0,
                            max(0.0, (discovery_rate_limit_not_before - now).total_seconds()),
                        )
                    elif retry_at is not None and retry_at > now:
                        wait_seconds = min(30.0, max(0.0, (retry_at - now).total_seconds()))
                    elif config.test_mode and not test_remaining:
                        wait_seconds = 30.0
                    else:
                        invocation_deadline = window_end
                        discovery_stop_event = asyncio.Event()
                        invoked = True
                        result, error = await _invoke_before_deadline(
                            invoke,
                            clock,
                            invocation_deadline,
                            graceful_stop_event=discovery_stop_event,
                            config=config,
                            dataset=selected.name,
                            mode="discover",
                            hashtags=(hashtag_at(selected.keywords, hashtag_position),),
                            max_items=test_remaining if config.test_mode else 0,
                            progress_offset=decision.collected_count,
                            stop_event=stop_event,
                            discovery_stop_event=discovery_stop_event,
                        )
                        if result:
                            print(
                                f"[PYTHON] {selected.name}: collector invocation failed: {error}",
                                file=sys.stderr,
                            )
                            access_failure = _collector_access_failure(selected)
                            if exit_code == 0:
                                exit_code = result
                            last_errors[selected.name] = error
                            collector_failures[selected.name] += 1
                            attempt = discovery_retry_attempts.get(window_key, 0) + 1
                            discovery_retry_attempts[window_key] = attempt
                            if access_failure == "rate_limited":
                                retry_at = min(
                                    clock() + timedelta(seconds=_collector_rate_limit_delay(selected)),
                                    ends_at,
                                )
                                discovery_rate_limit_not_before = retry_at
                                retry_state["discovery_rate_limit_not_before"] = retry_at
                                last_errors[selected.name] = (
                                    f"{error}; Instagram rate limit detected, discovery paused until {retry_at.isoformat()}"
                                )
                                print(
                                    f"[PYTHON] {selected.name}: Instagram HTTP 429; "
                                    f"discovery is paused until {retry_at:%H:%M:%S}; "
                                    "due recollections remain eligible.",
                                    file=sys.stderr,
                                )
                            else:
                                retry_at = min(
                                    clock() + timedelta(seconds=_due_retry_delay(attempt)),
                                    window_end,
                                )
                                if attempt >= _COLLECTOR_RETRY_MAX_ATTEMPTS:
                                    retry_at = window_end
                                    last_errors[selected.name] = (
                                        f"{error}; retry deferred until the next discovery window after {attempt} failures"
                                    )
                            discovery_retry_not_before[window_key] = retry_at
                        else:
                            last_errors[selected.name] = ""
                            if selected.name == "fashion":
                                request_fashion_analysis()
                            hashtag_positions[selected.name] += 1
                            discovery_retry_attempts.pop(window_key, None)
                            discovery_retry_not_before.pop(window_key, None)
                            discovery_rate_limit_not_before = None
                            retry_state["discovery_rate_limit_not_before"] = None

                if (
                    not invoked
                    and not pending
                    and now >= discovery_ends_at
                    and scheduled_before_end
                ):
                    wait_seconds = max(
                        0.0,
                        min((job.due_at - now).total_seconds() for job in scheduled_before_end),
                    )

                status_now = clock()
                for dataset in configured_datasets:
                    publication_error = ""
                    # The generic collector already publishes the standard
                    # reels.* and users.* files atomically for base-output
                    # runs. Do not create domain-prefixed copies there.
                    if not config.base_output:
                        try:
                            signature = dataset_publication_signature(dataset)
                            previous_outputs = published_outputs.get(dataset.name, {})
                            if (
                                publication_signatures.get(dataset.name) != signature
                                or any(not path.exists() for path in previous_outputs.values())
                            ):
                                published_outputs[dataset.name] = publish_dataset_outputs(dataset)
                                # Failed publications keep the old signature for retry.
                                publication_signatures[dataset.name] = signature
                        except OSError as error:
                            publication_error = str(error)
                            maintenance_pending = True
                            print(f"[{dataset.name.upper()}] Output publication deferred: {error}", file=sys.stderr)
                    dataset_status = current_status(
                            config,
                            dataset,
                            started_at,
                            discovery_ends_at,
                            ends_at,
                            status_now,
                            last_error=last_errors[dataset.name],
                            collector_failures=collector_failures[dataset.name],
                            completed=status_now >= ends_at,
                            hashtag_position=hashtag_positions[dataset.name],
                        )
                    recovery_error = android_recovery_failure(dataset.data_root)
                    if recovery_error:
                        dataset_status.update(android_state="recovery_wait", android_error=recovery_error)
                    if publication_error:
                        dataset_status["publication_error"] = publication_error
                    try:
                        write_dataset_status(dataset, dataset_status)
                    except OSError as error:
                        maintenance_pending = True
                        print(f"[{dataset.name.upper()}] Status publication deferred: {error}", file=sys.stderr)
                if (
                    not invoked
                    and status_now >= discovery_ends_at
                    and not pending
                    and not scheduled_before_end
                    and not maintenance_pending
                ):
                    completed_early = True
                    print("[SCHEDULER] All collection work finished; no scheduled recollections or Android/output work remain.", flush=True)
                    break
                if status_now < ends_at and not stop_event.is_set():
                    if invoked:
                        await asyncio.sleep(0)
                    else:
                        remaining_run_seconds = max(0.0, (ends_at - status_now).total_seconds())
                        await wait_for_stop_or_timeout(
                            stop_event,
                            min(wait_seconds, remaining_run_seconds, 30.0 if maintenance_pending else wait_seconds,
                                min((max(0.0, (at - status_now).total_seconds()) for at in app_restart_times), default=remaining_run_seconds)),
                        )
    finally:
        if completed_early or clock() >= ends_at or stop_event.is_set():
            for dataset in datasets(config):
                try:
                    status = current_status(config, dataset, started_at, discovery_ends_at, ends_at, clock(),
                                            last_error=last_errors[dataset.name], collector_failures=collector_failures[dataset.name],
                                            completed=completed_early)
                    if completed_early:
                        status.update(completion_reason="all_collection_work_finished", finished_at=_isoformat(clock()))
                    elif clock() < ends_at:
                        status["state"] = "stopped"
                    recovery_error = android_recovery_failure(dataset.data_root)
                    if recovery_error:
                        status.update(android_state="recovery_wait", android_error=recovery_error)
                    write_dataset_status(dataset, status)
                except (OSError, ValueError) as error:
                    print(f"[{dataset.name.upper()}] Final status deferred: {error}", file=sys.stderr)
        if browser_session is not None:
            await browser_session.close()
        if fashion_analyzer_task is not None:
            if not fashion_analyzer_task.done():
                fashion_analyzer_task.cancel()
            try:
                await fashion_analyzer_task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                print(f"[ANALYZER] Fashion analysis stopped: {error}", file=sys.stderr)
        for relay in android_log_relays:
            relay.cancel()
        if android_log_relays:
            await asyncio.gather(*android_log_relays, return_exceptions=True)
        signal.signal(signal.SIGINT, previous_sigint)
        for dataset in datasets(config):
            stem = "reels" if config.base_output else f"{'' if dataset.name == 'fashion' else dataset.name + '_'}reels"
            published = published_outputs.get(dataset.name, {})
            outputs = [published.get(f"reels_{extension}", _public_root(dataset) / f"{stem}.{extension}") for extension in ("xlsx", "csv", "json")]
            saved_paths = [str(path.resolve()) for path in outputs if path.exists()]
            if saved_paths:
                print(f"[{dataset.name.upper()}] 최종 릴스 저장 위치: " + ", ".join(saved_paths))
    return exit_code


__all__ = [
    "SupervisorLock",
    "WorkDecision",
    "active_dataset_name",
    "active_hashtag_position",
    "active_window_index",
    "current_status",
    "dataset_by_name",
    "datasets",
    "decide_next_work",
    "invoke_generic_collector",
    "invoke_generic_recollection_batches",
    "publish_dataset_outputs",
    "read_history",
    "run_fashion_beauty_collection",
    "utc_now",
    "write_dataset_status",
]
