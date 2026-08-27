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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Sequence

from .fashion_beauty_scheduler import (
    DatasetConfig,
    DueJob,
    RunConfig,
    due_jobs,
    initial_count_in_window,
    keyword_group,
    window_dataset,
)
from .instagram_reels_browser import (
    REEL_HISTORY_DIRECTORY,
    REEL_HISTORY_FILENAME,
    parse_args,
    process_is_alive,
    run_collector,
    wait_for_stop_or_timeout,
)
from exporters.instagram_collector import write_xlsx_workbook


Invocation = Callable[..., Awaitable[int]]
Clock = Callable[[], datetime]

_PUBLIC_SOURCES = {
    "reels_csv": "reels.csv",
    "reels_json": "reels.json",
    "reels_xlsx": "reels.xlsx",
    "users_csv": "users.csv",
    "users_xlsx": "users.xlsx",
}

_LOCK_READ_ATTEMPTS = 3
_LOCK_READ_RETRY_SECONDS = 0.05
_LOCK_MALFORMED_GRACE_SECONDS = 2.0
_DUE_RETRY_MAX_SECONDS = 30.0


@dataclass(frozen=True)
class WorkDecision:
    due_jobs: tuple[DueJob, ...]
    discover: bool
    remaining_capacity: int


def _due_retry_delay(attempt: int) -> float:
    exponent = min(max(0, attempt - 1), 5)
    return min(float(2**exponent), _DUE_RETRY_MAX_SECONDS)


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
    if not history.exists():
        return []
    with history.open("r", newline="", encoding="utf-8-sig") as file:
        return [dict(row) for row in csv.DictReader(file)]


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
    remaining = max(0, config.max_new_items_per_window - used)
    return WorkDecision(
        due_jobs=tuple(due_jobs(selected, rows, now)),
        discover=remaining > 0,
        remaining_capacity=remaining,
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
    changed = transformed_fields != fields
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


def _atomic_write_xlsx(destination: Path, sheet_name: str, fields: list[str], rows: list[dict[str, Any]]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        matrix = [fields, *[[str(row.get(field, "") or "") for field in fields] for row in rows]]
        write_xlsx_workbook(temporary, [(sheet_name, matrix)])
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def publish_dataset_outputs(dataset: DatasetConfig) -> dict[str, Path]:
    """Atomically publish only domain-prefixed exports from one workspace."""
    public_root = _public_root(dataset)
    written: dict[str, Path] = {}
    projections: dict[str, tuple[list[str], list[dict[str, Any]], bool]] = {}
    reel_history = dataset.data_root / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME

    # The generic collector creates a wide, one-row-per-Reel public projection.
    # Scheduled domains instead expose their raw append-only history so every
    # recollection is a new row and earlier snapshots never change.
    if reel_history.exists():
        fields, rows, _changed = _read_csv_projection(reel_history, "reels")
        destinations = {
            "reels_csv": public_root / f"{dataset.name}_reels.csv",
            "reels_json": public_root / f"{dataset.name}_reels.json",
            "reels_xlsx": public_root / f"{dataset.name}_reels.xlsx",
        }
        _atomic_write_csv(destinations["reels_csv"], fields, rows)
        _write_json_atomic(destinations["reels_json"], rows)
        _atomic_write_xlsx(destinations["reels_xlsx"], "reels", fields, rows)
        written.update(destinations)

    for kind, source_name in _PUBLIC_SOURCES.items():
        if reel_history.exists() and kind.startswith("reels_"):
            continue
        source = dataset.data_root / source_name
        if not source.exists():
            continue
        destination = public_root / f"{dataset.name}_{source_name}"
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
                if changed:
                    _write_json_atomic(destination, projected_rows)
                else:
                    _atomic_copy(source, destination)
            else:
                _atomic_copy(source, destination)
        elif kind.endswith("_xlsx") and record_kind in projections:
            fields, rows, changed = projections[record_kind]
            if changed:
                _atomic_write_xlsx(destination, record_kind, fields, rows)
            else:
                _atomic_copy(source, destination)
        else:
            _atomic_copy(source, destination)
        written[kind] = destination
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
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_dataset_status(dataset: DatasetConfig, state: dict[str, Any]) -> None:
    _write_json_atomic(_public_root(dataset) / f"{dataset.name}_collector_status.json", state)


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
) -> int:
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
    urls_file: Path | None = None

    if mode == "discover":
        if not hashtags:
            raise ValueError("Discovery requires at least one hashtag")
        arguments.extend(
            [
                "--hashtag-query",
                " OR ".join(hashtags),
                "--max-items",
                str(config.new_items_per_window if max_items is None else max_items),
                "--new-urls-only",
                "--followers-after-reels",
                "--max-upload-age-days",
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

    try:
        options = parse_args(arguments)
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


def _active_window_start(started_at: datetime, now: datetime, interval_minutes: float) -> datetime:
    interval_seconds = float(interval_minutes) * 60
    overall_index = max(0, int((now - started_at).total_seconds() // interval_seconds))
    return started_at + timedelta(seconds=overall_index * interval_seconds)


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
) -> dict[str, Any]:
    rows = read_history(dataset)
    active_name = active_dataset_name(config, started_at, now) if now < discovery_ends_at else None
    window_start = _active_window_start(started_at, now, config.discovery_interval_minutes)
    window_end = window_start + timedelta(minutes=config.discovery_interval_minutes)
    current_count = initial_count_in_window(rows, window_start, window_end)
    pending = (
        []
        if config.new_only
        else [job for job in due_jobs(dataset, rows, now) if job.due_at >= started_at]
    )
    active_keywords: Sequence[str] = ()
    if active_name == dataset.name:
        active_keywords = keyword_group(
            dataset.keywords,
            active_window_index(
                started_at,
                now,
                config.discovery_interval_minutes,
                alternating_domains=len(config.domains) > 1,
            ),
            config.keywords_per_window,
        )
    return {
        "dataset": dataset.name,
        "state": (
            "completed"
            if now >= ends_at
            else ("new_only" if config.new_only else ("recollection_only" if now >= discovery_ends_at else "running"))
        ),
        "started_at": _isoformat(started_at),
        "discovery_ends_at": _isoformat(discovery_ends_at),
        "ends_at": _isoformat(ends_at),
        "updated_at": _isoformat(now),
        "active_keywords": list(active_keywords),
        "max_upload_age_days": config.max_upload_age_days,
        "window_target": config.new_items_per_window,
        "window_collected": current_count,
        "window_cap": config.max_new_items_per_window,
        "window_cap_reached": current_count >= config.max_new_items_per_window,
        "due_recollections": len(pending),
        "overdue_recollections": sum(job.due_at < now for job in pending),
        "collector_failures": collector_failures,
        "last_error": last_error,
    }


async def _invoke_before_deadline(
    invoke: Invocation,
    clock: Clock,
    deadline: datetime,
    **kwargs: Any,
) -> tuple[int, str]:
    mode = str(kwargs.get("mode", "collector"))
    remaining = (deadline - clock()).total_seconds()
    if remaining <= 0:
        return 2, f"{mode} deadline reached before collector invocation"
    try:
        result = int(await asyncio.wait_for(invoke(**kwargs), timeout=remaining))
    except TimeoutError:
        return 2, f"{mode} collector exceeded its deadline"
    except Exception as error:
        return 2, f"{mode} collector failed: {error}"
    if clock() > deadline:
        return 2, f"{mode} collector completed after its deadline"
    if result:
        return result, f"{mode} collector exited with code {result}"
    return 0, ""


async def run_fashion_beauty_collection(
    config: RunConfig,
    *,
    invoke: Invocation = invoke_generic_collector,
    clock: Clock = utc_now,
) -> int:
    started_at = clock()
    discovery_ends_at = started_at + timedelta(hours=config.discovery_hours)
    ends_at = started_at + timedelta(hours=config.duration_hours)
    stop_event = asyncio.Event()
    lock_path = config.data_root / ".datasets" / "fashion_beauty_scheduler.lock.json"
    last_errors = {name: "" for name in config.domains}
    collector_failures = {name: 0 for name in config.domains}
    retry_attempts: dict[tuple[str, str, datetime], int] = {}
    retry_not_before: dict[tuple[str, str, datetime], datetime] = {}
    completed_new_only_windows: set[tuple[str, datetime]] = set()
    exit_code = 0
    interrupt_count = 0
    previous_sigint = signal.getsignal(signal.SIGINT)

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
        async with SupervisorLock(lock_path):
            while not stop_event.is_set() and clock() < ends_at:
                now = clock()
                configured_datasets = datasets(config)
                histories = {dataset.name: read_history(dataset) for dataset in configured_datasets}
                pending = (
                    []
                    if config.new_only
                    else sorted(
                        (
                            job
                            for dataset in configured_datasets
                            for job in due_jobs(dataset, histories[dataset.name], now)
                            if job.due_at >= started_at
                        ),
                        key=lambda job: (job.due_at, job.dataset, job.url),
                    )
                )

                wait_seconds = 30.0
                invoked = False
                eligible_jobs = [
                    job
                    for job in pending
                    if retry_not_before.get((job.dataset, job.url, job.due_at), now) <= now
                ]

                if eligible_jobs:
                    next_job = eligible_jobs[0]
                    job_key = (next_job.dataset, next_job.url, next_job.due_at)
                    invoked = True
                    result, error = await _invoke_before_deadline(
                        invoke,
                        clock,
                        ends_at,
                        config=config,
                        dataset=next_job.dataset,
                        mode="recollect",
                        urls=[next_job.url],
                        stop_event=stop_event,
                    )
                    if result:
                        if exit_code == 0:
                            exit_code = result
                        last_errors[next_job.dataset] = error
                        collector_failures[next_job.dataset] += 1
                        attempt = retry_attempts.get(job_key, 0) + 1
                        retry_attempts[job_key] = attempt
                        wait_seconds = _due_retry_delay(attempt)
                        retry_not_before[job_key] = clock() + timedelta(seconds=wait_seconds)
                    else:
                        retry_attempts.pop(job_key, None)
                        retry_not_before.pop(job_key, None)
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
                    decision = decide_next_work(
                        config,
                        histories[selected.name],
                        now,
                        dataset=selected,
                        window_start=window_start,
                    )
                    window_key = (selected.name, window_start)
                    if decision.discover and (not config.new_only or window_key not in completed_new_only_windows):
                        batch_size = min(config.new_items_per_window, decision.remaining_capacity)
                        invocation_deadline = min(
                            window_start + timedelta(minutes=config.discovery_interval_minutes),
                            discovery_ends_at,
                            ends_at,
                        )
                        invoked = True
                        result, error = await _invoke_before_deadline(
                            invoke,
                            clock,
                            invocation_deadline,
                            config=config,
                            dataset=selected.name,
                            mode="discover",
                            hashtags=keyword_group(
                                selected.keywords,
                                active_window_index(
                                    started_at,
                                    now,
                                    config.discovery_interval_minutes,
                                    alternating_domains=len(config.domains) > 1,
                                ),
                                config.keywords_per_window,
                            ),
                            max_items=batch_size,
                            progress_offset=config.max_new_items_per_window - decision.remaining_capacity,
                            stop_event=stop_event,
                        )
                        if result:
                            if exit_code == 0:
                                exit_code = result
                            last_errors[selected.name] = error
                            collector_failures[selected.name] += 1
                        elif config.new_only:
                            # One pass deliberately examines every candidate from this
                            # keyword group.  Do not rescan the same group merely
                            # because fewer than the requested number qualified.
                            completed_new_only_windows.add(window_key)

                status_now = clock()
                for dataset in configured_datasets:
                    # The generic collector already publishes the standard
                    # reels.* and users.* files atomically for base-output
                    # runs. Do not create domain-prefixed copies there.
                    if not config.base_output:
                        publish_dataset_outputs(dataset)
                    write_dataset_status(
                        dataset,
                        current_status(
                            config,
                            dataset,
                            started_at,
                            discovery_ends_at,
                            ends_at,
                            status_now,
                            last_error=last_errors[dataset.name],
                            collector_failures=collector_failures[dataset.name],
                        ),
                    )
                if status_now < ends_at and not stop_event.is_set():
                    if invoked:
                        await asyncio.sleep(0)
                    else:
                        remaining_run_seconds = max(0.0, (ends_at - status_now).total_seconds())
                        await wait_for_stop_or_timeout(
                            stop_event,
                            min(wait_seconds, remaining_run_seconds),
                        )
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    return exit_code


__all__ = [
    "SupervisorLock",
    "WorkDecision",
    "active_dataset_name",
    "active_window_index",
    "current_status",
    "dataset_by_name",
    "datasets",
    "decide_next_work",
    "invoke_generic_collector",
    "publish_dataset_outputs",
    "read_history",
    "run_fashion_beauty_collection",
    "utc_now",
    "write_dataset_status",
]
