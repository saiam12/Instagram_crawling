"""Serial supervisor for isolated fashion and beauty collection datasets."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Sequence

from .fashion_beauty_scheduler import (
    BEAUTY_KEYWORDS,
    FASHION_KEYWORDS,
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


def datasets(config: RunConfig) -> tuple[DatasetConfig, DatasetConfig]:
    base = config.data_root / ".datasets"
    return (
        DatasetConfig("fashion", base / "fashion", FASHION_KEYWORDS),
        DatasetConfig("beauty", base / "beauty", BEAUTY_KEYWORDS),
    )


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
    selected = dataset or dataset_by_name(config, "fashion")
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


def publish_dataset_outputs(dataset: DatasetConfig) -> dict[str, Path]:
    """Atomically publish only domain-prefixed exports from one workspace."""
    public_root = _public_root(dataset)
    written: dict[str, Path] = {}
    for kind, source_name in _PUBLIC_SOURCES.items():
        source = dataset.data_root / source_name
        if not source.exists():
            continue
        destination = public_root / f"{dataset.name}_{source_name}"
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
) -> int:
    selected = dataset_by_name(config, dataset)
    selected.data_root.mkdir(parents=True, exist_ok=True)
    arguments = ["--data-dir", str(selected.data_root)]
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
        return await run_collector(parse_args(arguments))
    finally:
        if urls_file is not None:
            urls_file.unlink(missing_ok=True)


def active_window_index(started_at: datetime, now: datetime, interval_minutes: float) -> int:
    interval_seconds = float(interval_minutes) * 60
    if interval_seconds <= 0:
        raise ValueError("discovery_interval_minutes must be greater than zero")
    overall_index = max(0, int((now - started_at).total_seconds() // interval_seconds))
    return overall_index // 2 + 1


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
    active_name = window_dataset(started_at, now) if now < discovery_ends_at else None
    window_start = _active_window_start(started_at, now, config.discovery_interval_minutes)
    window_end = window_start + timedelta(minutes=config.discovery_interval_minutes)
    current_count = initial_count_in_window(rows, window_start, window_end)
    pending = due_jobs(dataset, rows, now)
    active_keywords: Sequence[str] = ()
    if active_name == dataset.name:
        active_keywords = keyword_group(
            dataset.keywords,
            active_window_index(started_at, now, config.discovery_interval_minutes),
        )
    return {
        "dataset": dataset.name,
        "state": "completed" if now >= ends_at else ("recollection_only" if now >= discovery_ends_at else "running"),
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
    last_errors = {"fashion": "", "beauty": ""}
    collector_failures = {"fashion": 0, "beauty": 0}
    retry_attempts: dict[tuple[str, str, datetime], int] = {}
    retry_not_before: dict[tuple[str, str, datetime], datetime] = {}
    exit_code = 0

    async with SupervisorLock(lock_path):
        while clock() < ends_at:
            now = clock()
            configured_datasets = datasets(config)
            histories = {dataset.name: read_history(dataset) for dataset in configured_datasets}
            pending = sorted(
                (
                    job
                    for dataset in configured_datasets
                    for job in due_jobs(dataset, histories[dataset.name], now)
                ),
                key=lambda job: (job.due_at, job.dataset, job.url),
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
                selected = dataset_by_name(config, window_dataset(started_at, now))
                window_start = _active_window_start(started_at, now, config.discovery_interval_minutes)
                decision = decide_next_work(
                    config,
                    histories[selected.name],
                    now,
                    dataset=selected,
                    window_start=window_start,
                )
                if decision.discover:
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
                            active_window_index(started_at, now, config.discovery_interval_minutes),
                        ),
                        max_items=batch_size,
                    )
                    if result:
                        if exit_code == 0:
                            exit_code = result
                        last_errors[selected.name] = error
                        collector_failures[selected.name] += 1

            status_now = clock()
            for dataset in configured_datasets:
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
            if status_now < ends_at:
                if invoked:
                    await asyncio.sleep(0)
                else:
                    remaining_run_seconds = max(0.0, (ends_at - status_now).total_seconds())
                    await wait_for_stop_or_timeout(
                        stop_event,
                        min(wait_seconds, remaining_run_seconds),
                    )
    return exit_code


__all__ = [
    "SupervisorLock",
    "WorkDecision",
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
