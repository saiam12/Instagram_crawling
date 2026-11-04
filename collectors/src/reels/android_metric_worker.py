"""Durable Android job execution and result publication."""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from . import collection_pause
from .android_metric_policy import (
    _is_android_device_connection_error,
    _is_android_ui_dump_timeout,
    _is_android_ui_read_failure,
    compare_python_and_android_counts,
    format_android_mismatch,
)
from .android_metric_queue import (
    ANDROID_PROFILE_VIEW_FALLBACK_ENABLED,
    REEL_HISTORY_DIRECTORY,
    _android_metric_queue_paths,
    android_metric_queue_counts,
    collection_stop_path,
)
from .android_reel_metrics import (
    ANDROID_COLLECTION_FIELDS,
    AndroidMetricResult,
    AndroidReelMetricsEnricher,
    android_result_matches_reel,
    apply_metric_visibility_rules,
    merge_android_metrics,
    write_hashtag_post_counts,
)
from .collection_diagnostics import CollectorDiagnostics
from .collection_log import append_collection_log, collection_log_path, print_new_collection_log_lines
from .collection_settings import (
    ANDROID_DEVICE_RECONNECT_SECONDS,
    ANDROID_METRIC_FAILURE_BACKOFF_SECONDS,
    ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL,
    ANDROID_METRIC_QUEUE_EXPORT_BATCH_SIZE,
    ANDROID_METRIC_QUEUE_IDLE_SECONDS,
    ANDROID_PROFILE_VIEW_MAX_ATTEMPTS,
    ANDROID_PROFILE_VIEW_RETRY_DELAY_SECONDS,
    ANDROID_RECOVERY_RETRY_SECONDS,
    ANDROID_TERMINAL_METRIC_FIELDS,
    ANDROID_UI_MAX_RETRY_DELAY_SECONDS,
    ANDROID_UI_RECOVERY_COOLDOWN_SECONDS,
    ANDROID_UI_RETRY_DELAY_SECONDS,
    COLLECTION_MAX_CONSECUTIVE_FAILURES,
    PYTHON_ANDROID_COMPARISON_COUNT_FIELDS,
    PYTHON_VERSION_ROOT,
    REEL_HISTORY_FILENAME,
)
from .instagram_follower_enricher import read_csv_objects
from .korean_classification import apply_korean_overrides
from .process_lock import AtomicProcessLock, process_is_alive
from .reel_history import (
    apply_observed_view_count,
    exact_visible_reel_count,
    write_count_history_xlsx,
    write_long_output_bundle,
)
from .reel_records import (
    UNAVAILABLE_LIKE_COUNT_MARKER,
    enrich_reel_collection_rows,
    exact_nonnegative_integer,
    isoformat_utc,
    parse_datetime,
    utc_now,
)
from .reel_store import write_csv_records, write_json_atomic


def android_metric_job_delay_seconds(job: dict[str, Any], *, now: datetime | None = None) -> float:
    """Count time already spent in the queue toward the handoff grace period."""
    delay = max(0.0, float(job.get("delay_seconds", 0.0) or 0.0))
    queued_at = parse_datetime(job.get("queued_at"))
    if queued_at is None:
        return delay
    elapsed = max(0.0, ((now or utc_now()) - queued_at).total_seconds())
    return max(0.0, delay - elapsed)


def read_collection_stop_request(data_dir: Path | str) -> dict[str, Any] | None:
    value = _read_android_metric_job(collection_stop_path(data_dir))
    return value if value and value.get("stop_requested") is True else None


def enqueue_android_metric_job(
    data_dir: Path | str,
    record: dict[str, Any],
    *,
    missing_python_fields: Sequence[str] = (),
    delay_seconds: float = 0.0,
    adb_path: Path | str | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = 0.35,
    collection_run_id: str = "",
    python_index: int = 0,
    python_total: int = 0,
    collection_number: int = 0,
    recollection: bool = False,
) -> str:
    """Atomically persist a URL for the independent Android worker."""
    url = str(record.get("url", "")).strip()
    collected_at = str(record.get("collected_at", "")).strip()
    if not url or not collected_at:
        raise ValueError("Android metric jobs require a Reel URL and collected_at timestamp.")
    paths = _android_metric_queue_paths(data_dir)
    paths["pending"].mkdir(parents=True, exist_ok=True)
    job_id = f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}"
    payload = {
        "job_id": job_id,
        "queued_at": isoformat_utc(),
        "target": {"url": url, "collected_at": collected_at, "username": str(record.get("username") or "")},
        "python_metrics": {
            field: record.get(field, "")
            for field in (*PYTHON_ANDROID_COMPARISON_COUNT_FIELDS, "ad")
        },
        "missing_python_fields": list(missing_python_fields),
        "delay_seconds": max(0.0, float(delay_seconds)),
        "collection_run_id": str(collection_run_id),
        "python_index": max(0, int(python_index)),
        "python_total": max(0, int(python_total)),
        "collection_number": max(0, int(collection_number)),
        "recollection": recollection,
        "android": {
            "adb_path": str(adb_path) if adb_path else "",
            "device_id": str(device_id or ""),
            "ui_delay_seconds": max(0.1, float(ui_delay_seconds)),
        },
    }
    write_json_atomic(paths["pending"] / f"{job_id}.json", payload)
    return job_id


def _read_android_metric_job(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _android_completion_matches_job(completed: Any, job: dict[str, Any], fallback_job_id: str) -> bool:
    if not isinstance(completed, dict):
        return False
    target = job.get("target")
    completed_target = completed.get("target")
    return bool(
        isinstance(target, dict)
        and target.get("url") and target.get("collected_at")
        and isinstance(completed_target, dict)
        and all(str(target.get(field, "")) == completed_target.get(field) for field in ("url", "collected_at"))
        and completed.get("job_id") == str(job.get("job_id", fallback_job_id))
        and completed.get("kind") == str(job.get("kind") or "")
        and completed.get("recollection") is bool(job.get("recollection"))
        and completed.get("status") in ("collected", "unavailable", "skipped")
        and isinstance(completed.get("metrics"), dict)
        and isinstance(completed.get("completed_at"), str)
        and parse_datetime(completed.get("completed_at")) is not None
    )


def _recover_android_metric_working_jobs(paths: dict[str, Path]) -> None:
    paths["pending"].mkdir(parents=True, exist_ok=True)
    paths["working"].mkdir(parents=True, exist_ok=True)
    for working in paths["working"].glob("*.json"):
        destination = paths["pending"] / working.name
        job = _read_android_metric_job(working) or {}
        completed_path = paths["completed"] / working.name
        published = _read_android_metric_job(completed_path)
        completed = next((
            candidate for candidate in (job.get("saved_completion"), published)
            if _android_completion_matches_job(candidate, job, working.stem)
        ), None)
        if completed is not None:
            # A completion write may succeed before its mirror or working-file
            # cleanup fails. Finish that commit without collecting new values.
            if published != completed:
                write_json_atomic(completed_path, completed)
            if job.get("recollection"):
                mirror = paths["recollection_completed"] / working.name
                if _read_android_metric_job(mirror) != completed:
                    write_json_atomic(mirror, completed)
            if job.get("kind") == "profile_view_count" and exact_nonnegative_integer(completed["metrics"].get("view_count")) is None:
                mirror = paths["completed"].parent / "view_failed" / working.name
                failed_view = {**completed, "target": dict(job["target"]), "needs_review": True}
                if _read_android_metric_job(mirror) != failed_view:
                    write_json_atomic(mirror, failed_view)
            working.unlink(missing_ok=True)
            continue
        if destination.exists():
            working.unlink(missing_ok=True)
            continue
        try:
            os.replace(working, destination)
        except FileNotFoundError:
            pass


def _android_view_target_is_collected(data_dir: Path, target: dict[str, Any]) -> bool:
    history = data_dir / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    rows = read_csv_objects(history)[1] if history.exists() else []
    return any(
        row.get("url") == target.get("url")
        and row.get("collected_at") == target.get("collected_at")
        and exact_visible_reel_count(str(row.get("view_count", ""))) is not None
        for row in rows
    )


def prune_collected_android_view_jobs(data_dir: Path) -> int:
    """Remove only pending view helpers satisfied by the same stored snapshot."""
    pending = data_dir / REEL_HISTORY_DIRECTORY / "android_metric_queue" / "pending"
    jobs = [(path, _read_android_metric_job(path)) for path in pending.glob("*.json")]
    jobs = [(path, job) for path, job in jobs if job and job.get("kind") == "profile_view_count"]
    if not jobs:
        return 0
    history = data_dir / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    rows = read_csv_objects(history)[1] if history.exists() else []
    collected = {
        (str(row.get("url", "")), str(row.get("collected_at", "")))
        for row in rows if exact_visible_reel_count(str(row.get("view_count", ""))) is not None
    }
    removed = 0
    for path, job in jobs:
        target = job.get("target") or {}
        if (str(target.get("url", "")), str(target.get("collected_at", ""))) in collected:
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass  # The worker may have claimed it; it rechecks before navigation.
    return removed


def _claim_android_metric_job(paths: dict[str, Path]) -> tuple[Path, dict[str, Any]] | None:
    paths["pending"].mkdir(parents=True, exist_ok=True)
    paths["working"].mkdir(parents=True, exist_ok=True)

    def job_priority(path: Path) -> tuple[int, datetime, str]:
        job = _read_android_metric_job(path) or {}
        kind = str(job.get("kind") or "")
        # The related-tag job is awaited before candidate Reel collection.
        # Exact one-tag jobs are best-effort idle work and must yield to every
        # actual Reel metric job.
        priority = (
            -1 if kind == "app_restart"
            else 0 if kind == "related_hashtag_post_counts"
            else 3 if kind == "profile_view_count"
            else 2 if kind == "hashtag_post_count"
            else 1
        )
        queued_at = str(job.get("retry_queued_at") or job.get("queued_at") or "")
        try:
            timestamp = datetime.fromisoformat(queued_at.replace("Z", "+00:00"))
        except ValueError:
            timestamp = datetime.min.replace(tzinfo=timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return priority, timestamp, path.name

    for pending in sorted(
        paths["pending"].glob("*.json"),
        key=job_priority,
    ):
        queued_job = _read_android_metric_job(pending) or {}
        if queued_job.get("kind") == "profile_view_count" and not ANDROID_PROFILE_VIEW_FALLBACK_ENABLED:
            continue  # Preserve disabled view jobs without claiming or retrying them.
        retry_at = parse_datetime(queued_job.get("next_attempt_at"))
        if retry_at is not None and retry_at > utc_now():
            continue
        working = paths["working"] / pending.name
        try:
            os.replace(pending, working)
        except FileNotFoundError:
            continue
        job = _read_android_metric_job(working)
        if job is not None:
            if not job.get("kind"):
                status = _read_android_metric_job(paths["status"]) or {}
                job["android_queue_index"] = max(0, int(status.get("reel_attempts_started", 0) or 0)) + 1
                write_json_atomic(working, job)
                current, total = android_reel_progress(paths, job, fallback_current=1)
                _write_android_metric_worker_status(
                    paths, reel_attempts_started=current, reel_attempts_total=total,
                )
            return working, job
        working.unlink(missing_ok=True)
    return None


def _write_android_metric_completion(
    paths: dict[str, Path],
    working_path: Path,
    job: dict[str, Any],
    result: AndroidMetricResult,
) -> None:
    paths["completed"].mkdir(parents=True, exist_ok=True)
    target = job.get("target") if isinstance(job.get("target"), dict) else {}
    payload = {
        "job_id": str(job.get("job_id", working_path.stem)),
        "kind": str(job.get("kind") or ""),
        "completed_at": isoformat_utc(),
        "attempts": max(0, int(job.get("attempts", 0) or 0)),
        "target": {
            "url": str(target.get("url", "")),
            "collected_at": str(target.get("collected_at", "")),
        },
        "metrics": dict(result.metrics),
        "compact_fields": list(result.compact_fields),
        "recollection": bool(job.get("recollection")),
        "python_metrics": dict(job.get("python_metrics")) if isinstance(job.get("python_metrics"), dict) else {},
        "android_mismatch": str(job.get("android_mismatch", "")),
        "audio_name": result.audio_name,
        "like_count_private": result.like_count_private,
        "comment_count_disabled": result.comment_count_disabled,
        "status": result.status,
        "error": result.error[:500],
        "observed_reel_url": result.observed_reel_url,
        "observed_username": result.observed_username,
    }
    if result.like_count_private is True:
        payload["metrics"]["like_count"] = UNAVAILABLE_LIKE_COUNT_MARKER
    # Retain the accepted result until both publication and cleanup finish.
    # A browser process may consume the public completion before that point.
    job["saved_completion"] = payload
    write_json_atomic(working_path, job)
    write_json_atomic(paths["completed"] / working_path.name, payload)
    if job.get("kind") == "profile_view_count" and exact_nonnegative_integer(result.metrics.get("view_count")) is None:
        write_json_atomic(paths["completed"].parent / "view_failed" / working_path.name, {
            **payload, "target": dict(target), "needs_review": True,
        })
    if job.get("recollection"):
        write_json_atomic(paths["recollection_completed"] / working_path.name, payload)
    working_path.unlink(missing_ok=True)


def android_metric_terminal_line(
    result: AndroidMetricResult,
    attempts: int,
    *,
    current: int,
    total: int,
    reel_url: str,
    python_metrics: dict[str, Any] | None = None,
) -> str:
    """Format the only per-Reel Android line shown in the collector terminal."""
    python_values = python_metrics or {}
    combined_values: dict[str, object] = {**python_values, **result.metrics}
    if result.like_count_private is True:
        combined_values["like_count"] = UNAVAILABLE_LIKE_COUNT_MARKER
    if result.status == "collected":
        combined_values = apply_metric_visibility_rules(
            combined_values,
            comment_count_disabled=result.comment_count_disabled is True,
        )
    fields: list[str] = []
    for field in ANDROID_TERMINAL_METRIC_FIELDS:
        if field == "view_count":
            continue
        value = combined_values.get(field, "")
        rendered = f"{value:,}" if isinstance(value, int) else str(value or "unavailable")
        fields.append(f"{field}={rendered}")
    fields.append(f"attempt_count={max(0, int(attempts))}")
    return f"[Android] [{max(1, int(current))}/{max(1, int(total))}] {reel_url} | " + ", ".join(fields)


def android_reel_progress(
    paths: dict[str, Path],
    job: dict[str, Any],
    *,
    fallback_current: int,
) -> tuple[int, int]:
    """Count actual queue attempts across browser batches and worker restarts."""
    status = _read_android_metric_job(paths["status"]) or {}
    index = int(job.get("android_queue_index", 0) or 0)
    current = max(1, index or fallback_current)
    started = max(index, int(status.get("reel_attempts_started", 0) or 0))
    pending_reels = sum(
        1 for path in paths["pending"].glob("*.json")
        if not (_read_android_metric_job(path) or {}).get("kind")
    )
    total = max(current, int(status.get("reel_attempts_total", 0) or 0), started + pending_reels)
    return current, total


def _requeue_android_metric_job(
    paths: dict[str, Path],
    working_path: Path,
    job: dict[str, Any],
    error: str,
    *,
    to_tail: bool = False,
    show_in_terminal: bool = False,
) -> None:
    """Preserve a Reel job when ADB temporarily loses its emulator."""
    previous_total = android_reel_progress(paths, job, fallback_current=1)[1] if job.get("android_queue_index") else 0
    job["last_device_error"] = str(error)[:500]
    paths["pending"].mkdir(parents=True, exist_ok=True)
    if to_tail:
        latest = utc_now()
        for pending in paths["pending"].glob("*.json"):
            queued_job = _read_android_metric_job(pending) or {}
            queued_at = str(queued_job.get("retry_queued_at") or queued_job.get("queued_at") or "")
            try:
                timestamp = datetime.fromisoformat(queued_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            latest = max(latest, timestamp)
        job["retry_queued_at"] = (latest + timedelta(microseconds=1)).isoformat(timespec="microseconds")
    write_json_atomic(working_path, job)
    os.replace(working_path, paths["pending"] / working_path.name)
    if job.get("android_queue_index"):
        current, total = android_reel_progress(paths, job, fallback_current=1)
        total = max(total, previous_total + 1)
        _write_android_metric_worker_status(paths, reel_attempts_total=total)
        retry_at = parse_datetime(job.get("next_attempt_at"))
        retry_seconds = max(0, math.ceil((retry_at - utc_now()).total_seconds())) if retry_at else ANDROID_DEVICE_RECONNECT_SECONDS
        target = job.get("target") or {}
        terminal_line = (
            f"[Android] [{current}/{total}] {target.get('url', '')} | "
            f"status=requeued, retry_in={retry_seconds:g}s, attempt_count={job.get('attempts', 0)}"
        )
        append_collection_log(
            paths["root"].parent.parent, "android", "reel_metric_deferred",
            show_in_terminal=show_in_terminal, url=target.get("url", ""),
            error=error, retry_seconds=retry_seconds, terminal_line=terminal_line,
        )


def apply_completed_android_metric_jobs(
    data_dir: Path | str,
    *,
    export_outputs: bool = True,
) -> dict[str, int]:
    """Merge completed app metrics into history without racing browser writes."""
    destination = Path(data_dir).resolve()
    paths = _android_metric_queue_paths(destination)
    completed_paths = sorted(paths["completed"].glob("*.json")) if paths["completed"].exists() else []
    if not completed_paths:
        return {"applied": 0, "updated": 0, "pending": 0}
    history_path = destination / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    if not history_path.exists() or not history_path.stat().st_size:
        return {"applied": 0, "updated": 0, "pending": len(completed_paths)}
    lock = AtomicProcessLock(paths["history_lock"], wait_seconds=30)
    if not lock.acquire():
        return {"applied": 0, "updated": 0, "pending": len(completed_paths)}
    try:
        fields, rows = read_csv_objects(history_path)
        target_rows = {
            (str(row.get("url", "")), str(row.get("collected_at", ""))): row
            for row in rows
        }
        applied_paths: list[Path] = []
        updated = 0
        for completed in completed_paths:
            result = _read_android_metric_job(completed)
            target = result.get("target") if isinstance(result, dict) and isinstance(result.get("target"), dict) else {}
            row = target_rows.get((str(target.get("url", "")), str(target.get("collected_at", ""))))
            if row is None or result is None:
                continue
            metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
            expected_username = str(row.get("username") or target.get("username") or "").strip().lstrip('@').casefold()
            actual_username = str(result.get("observed_username") or "").strip().lstrip('@').casefold()
            if metrics and not android_result_matches_reel(result, row):
                quarantine = completed.parent.parent / "identity_unverified"
                quarantine.mkdir(exist_ok=True)
                os.replace(completed, quarantine / completed.name)
                append_collection_log(destination, "android", "reel_identity_rejected",
                                      url=target.get("url"), expected_username=expected_username,
                                      observed_url=result.get("observed_reel_url"), observed_username=actual_username)
                continue
            if result.get("kind") == "profile_view_count":
                count = exact_nonnegative_integer(metrics.get("view_count"))
                if count is not None and apply_observed_view_count(rows, row, count, "instagram_android_profile", str(result.get("completed_at") or "")):
                    fields = list(dict.fromkeys([*fields, "view_count_observed_at", "view_count_source"]))
                    updated += 1
                elif count is not None and exact_visible_reel_count(str(row.get("view_count", ""))) is None:
                    reason = "observation_outside_snapshot_window"
                    write_json_atomic(paths["root"] / "view_failed" / completed.name, {
                        **result, "status": "not_applied", "reason": reason, "needs_review": True,
                    })
                    append_collection_log(destination, "android", "profile_view_assist_not_applied",
                                          url=target.get("url"), collected_at=target.get("collected_at"),
                                          observed_at=result.get("completed_at"), reason=reason, view_count=count)
                applied_paths.append(completed)
                continue
            mismatch = str(result.get("android_mismatch", "") or "")
            if mismatch and row.get("android_mismatch") != mismatch:
                row["android_mismatch"] = mismatch
                updated += 1
                if "android_mismatch" not in fields:
                    fields.append("android_mismatch")
            merged = merge_android_metrics(row, AndroidMetricResult(
                metrics=metrics,
                compact_fields=tuple(result.get("compact_fields") or ()),
                recollection=bool(result.get("recollection")),
                like_count_private=result.get("like_count_private"),
                comment_count_disabled=result.get("comment_count_disabled"),
                status=str(result.get("status") or "unavailable"),
            ))
            for field in ANDROID_TERMINAL_METRIC_FIELDS:
                if row.get(field) != merged.get(field):
                    row[field] = merged.get(field, "")
                    updated += 1
            applied_paths.append(completed)
        if applied_paths:
            rows = enrich_reel_collection_rows(apply_korean_overrides(rows, destination))
            fields = list(dict.fromkeys([*fields, "korean", "korean_source", "account_country"]))
            write_csv_records(history_path, rows, fields)
            if export_outputs:
                write_long_output_bundle(history_path, rows, fields)
            write_count_history_xlsx(
                destination,
                rows,
                android_results=[
                    result
                    for completed in applied_paths
                    if (result := _read_android_metric_job(completed)) is not None
                ],
            )
            for completed in applied_paths:
                completed.unlink(missing_ok=True)
        return {
            "applied": len(applied_paths),
            "updated": updated,
            "pending": sum(path.exists() for path in completed_paths),
        }
    except OSError as error:
        # Completion files are removed only after every output is saved. A
        # workbook held open by Excel can therefore be merged on a later pass.
        append_collection_log(destination, "android", "completed_metric_merge_deferred", error=str(error))
        return {"applied": 0, "updated": 0, "pending": sum(path.exists() for path in completed_paths)}
    finally:
        lock.release()


def publish_scheduled_android_outputs(data_dir: Path | str) -> dict[str, Path]:
    destination = Path(data_dir).resolve()
    if destination.parent.name != ".datasets" or destination.name not in {"fashion", "beauty"}:
        return {}
    from .fashion_beauty_collection import publish_dataset_outputs
    from .fashion_beauty_scheduler import DatasetConfig

    return publish_dataset_outputs(DatasetConfig(destination.name, destination, ()))


def _write_android_metric_worker_status(paths: dict[str, Path], **patch: Any) -> None:
    current = _read_android_metric_job(paths["status"]) or {}
    current.update(patch)
    current["updated_at"] = isoformat_utc()
    write_json_atomic(paths["status"], current)


def _defer_android_worker_recovery(paths: dict[str, Path], error: str, **patch: Any) -> None:
    """Persist a bounded retry delay without consuming durable Reel jobs."""
    _write_android_metric_worker_status(
        paths, state="recovery_failed", device_error=error, stop_reason=error,
        retry_at=isoformat_utc(utc_now() + timedelta(seconds=ANDROID_RECOVERY_RETRY_SECONDS)),
        **patch,
    )


def python_metrics_for_android_job(data_dir: Path | str, job: dict[str, Any]) -> dict[str, Any]:
    """Use the queued browser snapshot, with history fallback for old jobs."""
    queued = job.get("python_metrics")
    if isinstance(queued, dict):
        return {
            field: queued.get(field, "")
            for field in (*PYTHON_ANDROID_COMPARISON_COUNT_FIELDS, "ad")
        }
    target = job.get("target") if isinstance(job.get("target"), dict) else {}
    url = str(target.get("url", ""))
    collected_at = str(target.get("collected_at", ""))
    history_path = Path(data_dir).resolve() / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    if not url or not history_path.exists() or not history_path.stat().st_size:
        return {}
    _fields, rows = read_csv_objects(history_path)
    for row in reversed(rows):
        if str(row.get("url", "")) == url and str(row.get("collected_at", "")) == collected_at:
            return {
                field: row.get(field, "")
                for field in (*PYTHON_ANDROID_COMPARISON_COUNT_FIELDS, "ad")
            }
    return {}


def run_android_metric_worker(
    data_dir: Path | str,
    *,
    adb_path: Path | str | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = 0.35,
    idle_seconds: float = ANDROID_METRIC_QUEUE_IDLE_SECONDS,
    attach_existing: bool = True,
) -> int:
    """Drain the durable Android queue; safe to relaunch after any interruption."""
    destination = Path(data_dir).resolve()
    stop_request = read_collection_stop_request(destination)
    if stop_request is not None:
        append_collection_log(
            destination,
            "android",
            "worker_not_started",
            show_in_terminal=attach_existing,
            reason=stop_request.get("reason", "collection failure limit reached"),
        )
        return 0
    paths = _android_metric_queue_paths(destination)
    worker_lock = AtomicProcessLock(paths["worker_lock"])
    if not worker_lock.acquire():
        owner = _read_android_metric_job(paths["worker_lock"]) or {}
        owner_pid = int(owner.get("pid", 0) or 0)
        append_collection_log(
            destination, "android", "worker_not_started",
            show_in_terminal=attach_existing, reason="another worker is already running",
        )
        if not attach_existing:
            return 0
        log_path = collection_log_path(destination, "android")
        position = log_path.stat().st_size if log_path.exists() else 0
        try:
            while owner_pid and process_is_alive(owner_pid):
                time.sleep(0.25)
                position = print_new_collection_log_lines(log_path, "android", position)
            print_new_collection_log_lines(log_path, "android", position)
        except KeyboardInterrupt:
            if owner_pid and process_is_alive(owner_pid):
                os.kill(owner_pid, signal.SIGTERM)
            return 130
        return 0
    paths["worker_starting"].unlink(missing_ok=True)
    _recover_android_metric_working_jobs(paths)
    diagnostics = CollectorDiagnostics(
        destination,
        run_mode="foreground" if attach_existing else "background",
        component="android",
    )
    diagnostics.network_unavailable(
        "ADB/UIAutomator does not intercept network responses generated by the Instagram Android app."
    )
    diagnostics_status = "failed"
    enricher = AndroidReelMetricsEnricher(
        adb_path=Path(adb_path) if adb_path else None,
        device_id=device_id,
        ui_delay_seconds=ui_delay_seconds,
        diagnostics=diagnostics,
    )
    processed = processed_reels = collected = unavailable = consecutive_failures = emulator_restarts = 0
    ui_timeout_streak = 0
    ui_failed_targets: set[str] = set()
    app_recovery_stage = 0
    next_app_recovery_at = 0.0
    recovery_failed = False
    idle_started = time.monotonic()
    pause_reel_when_idle = False
    idle_pause_attempted = False
    _write_android_metric_worker_status(
        paths,
        state="running",
        pid=os.getpid(),
        processed=0,
        collected=0,
        unavailable=0,
        consecutive_failures=0,
        emulator_restarts=0,
        device_error="",
        stop_reason="",
        retry_at="",
        **android_metric_queue_counts(destination),
    )
    log_path = collection_log_path(destination, "android")
    log_position = log_path.stat().st_size if log_path.exists() else 0
    terminal_log_stop = threading.Event()
    terminal_log_thread: threading.Thread | None = None
    append_collection_log(
        destination,
        "android",
        "worker_started",
        pid=os.getpid(),
        device_id=device_id or "default",
    )
    try:
        if attach_existing:
            def relay_worker_log() -> None:
                position = log_position
                while not terminal_log_stop.wait(0.25):
                    position = print_new_collection_log_lines(log_path, "android", position)
                print_new_collection_log_lines(log_path, "android", position)

            terminal_log_thread = threading.Thread(target=relay_worker_log, daemon=True)
            terminal_log_thread.start()
        while True:
            while collection_pause.is_paused() and read_collection_stop_request(destination) is None:
                time.sleep(0.1)
            if read_collection_stop_request(destination) is not None:
                diagnostics_status = "stopped"
                return 0
            # Wait before claiming a job so an absent device cannot consume
            # the retry budget of every pending Reel.
            check_ready = getattr(enricher, "_ready", None)
            readiness_error = (
                check_ready()
                if callable(check_ready) and android_metric_queue_counts(destination)["pending"]
                else ""
            )
            if _is_android_device_connection_error(readiness_error):
                enricher.reset_connection()
                _write_android_metric_worker_status(
                    paths, state="waiting_for_device", device_error=readiness_error,
                    **android_metric_queue_counts(destination),
                )
                time.sleep(max(0.1, ANDROID_DEVICE_RECONNECT_SECONDS))
                continue
            if readiness_error:
                _defer_android_worker_recovery(paths, readiness_error, **android_metric_queue_counts(destination))
                recovery_failed = True
                diagnostics_status = 'recovery_failed'
                return 1
            prune_collected_android_view_jobs(destination)
            claimed = _claim_android_metric_job(paths)
            if claimed is None:
                if pause_reel_when_idle and not idle_pause_attempted:
                    idle_pause_attempted = True
                    pause_current_reel = getattr(enricher, "pause_current_reel", None)
                    reel_paused_for_idle = bool(
                        callable(pause_current_reel) and pause_current_reel()
                    )
                    if reel_paused_for_idle:
                        append_collection_log(destination, "android", "reel_paused_for_queue_idle")
                merge = apply_completed_android_metric_jobs(destination)
                counts = android_metric_queue_counts(destination)
                deferred_ui_job = next((
                    queued_job for pending in paths["pending"].glob("*.json")
                    if (queued_job := _read_android_metric_job(pending))
                    and queued_job.get("ui_read_failures")
                ), {}) if counts["pending"] else {}
                _write_android_metric_worker_status(
                    paths,
                    state="requeued_after_ui_read_failure" if deferred_ui_job else "idle",
                    device_error=str(deferred_ui_job.get("last_ui_error") or ""),
                    processed=processed,
                    collected=collected,
                    unavailable=unavailable,
                    consecutive_failures=consecutive_failures,
                    **counts,
                    completed_applied=merge["applied"],
                    completed_updated=merge["updated"],
                    completed_unmatched=merge["pending"],
                )
                if not counts["pending"] and not counts["working"] and time.monotonic() - idle_started >= max(0.5, idle_seconds):
                    diagnostics_status = "completed"
                    return 0
                time.sleep(0.5)
                continue
            idle_started = time.monotonic()
            pause_reel_when_idle = False
            idle_pause_attempted = False
            working_path, job = claimed
            tag_job_kind = str(job.get("kind") or "")
            if tag_job_kind == "app_restart":
                due_at = parse_datetime(job.get("due_at"))
                if due_at is not None and utc_now() < due_at:
                    append_collection_log(destination, "android", "scheduled_app_restart_started", due_at=job["due_at"])
                    error = enricher.recover_app(str((job.get("target") or {}).get("url", "")))
                    append_collection_log(destination, "android", "scheduled_app_restart_failed" if error else "scheduled_app_restart_completed",
                                          due_at=job["due_at"], error=error)
                else:
                    append_collection_log(destination, "android", "scheduled_app_restart_expired", due_at=job.get("due_at", ""))
                working_path.unlink(missing_ok=True)
                continue
            if tag_job_kind == "profile_view_count":
                target = job.get("target") or {}
                def metrics_waiting() -> bool:
                    return any((_read_android_metric_job(p) or {}).get("kind") != "profile_view_count" for p in paths["pending"].glob("*.json"))
                # Web may already have completed this exact snapshot.
                if _android_view_target_is_collected(destination, target):
                    working_path.unlink(missing_ok=True)
                    continue
                last_view_check = 0.0
                view_collected = False
                def should_yield_view() -> bool:
                    nonlocal last_view_check, view_collected
                    now = time.monotonic()
                    if now - last_view_check >= 0.5:
                        last_view_check = now
                        view_collected = _android_view_target_is_collected(destination, target)
                    return view_collected or metrics_waiting()
                result = enricher.collect_profile_view_count(
                    str(target.get("url") or ""), str(target.get("username") or ""), should_yield=should_yield_view,
                )
                if _android_view_target_is_collected(destination, target):
                    working_path.unlink(missing_ok=True)
                    continue
                if not result.metrics and (metrics_waiting() or "yielded" in result.error.casefold()):
                    os.replace(working_path, paths["pending"] / working_path.name)
                    continue
                job["attempts"] = max(0, int(job.get("attempts", 0) or 0)) + 1
                if (
                    exact_nonnegative_integer(result.metrics.get("view_count")) is None
                    and result.error != "Invalid view target identity"
                    and job["attempts"] < ANDROID_PROFILE_VIEW_MAX_ATTEMPTS
                ):
                    delay = ANDROID_PROFILE_VIEW_RETRY_DELAY_SECONDS * job["attempts"]
                    job["next_attempt_at"] = isoformat_utc(utc_now() + timedelta(seconds=delay))
                    _requeue_android_metric_job(paths, working_path, job, result.error, to_tail=True)
                    append_collection_log(destination, "android", "profile_view_assist_retry", url=target.get("url"), attempt=job["attempts"], retry_seconds=delay, error=result.error)
                    continue
                _write_android_metric_completion(paths, working_path, job, result)
                apply_completed_android_metric_jobs(destination)
                append_collection_log(destination, "android", "profile_view_assist_finished", url=target.get("url"), status=result.status, metrics=result.metrics, error=result.error, attempts=job["attempts"])
                continue
            if tag_job_kind in {"related_hashtag_post_counts", "hashtag_post_count"}:
                hashtags = job.get("hashtags") if isinstance(job.get("hashtags"), list) else []
                is_idle_exact_tag_job = tag_job_kind == "hashtag_post_count"
                hashtag_status = "collected"
                hashtag_error = ""
                hashtag_rows: list[dict[str, object]] = []
                append_collection_log(
                    destination,
                    "android",
                    "idle_hashtag_post_search_started" if is_idle_exact_tag_job else "related_hashtag_search_started",
                    job_id=job.get("job_id", working_path.stem),
                    hashtags=hashtags,
                )
                try:
                    collect_tags = (
                        enricher.collect_hashtag_post_counts
                        if is_idle_exact_tag_job
                        else enricher.collect_related_hashtag_post_counts
                    )
                    hashtag_rows = collect_tags([str(value) for value in hashtags])
                    if not any(row.get("status") == "collected" for row in hashtag_rows):
                        hashtag_status = "unavailable"
                        failed_row = next((row for row in hashtag_rows if row.get("error")), {})
                        hashtag_error = str(failed_row.get("error", "No exact hashtag post count was visible."))[:500]
                    for row in hashtag_rows:
                        append_collection_log(
                            destination,
                            "android",
                            "idle_hashtag_post_collected" if is_idle_exact_tag_job else "related_hashtag_collected",
                            query_hashtag=row.get("query_hashtag", ""),
                            hashtag=row.get("hashtag", ""),
                            post_count=row.get("post_count", ""),
                            status=row.get("status", ""),
                            error=row.get("error", ""),
                        )
                except Exception as error:
                    hashtag_status = "unavailable"
                    hashtag_error = str(error)[:500]
                    append_collection_log(
                        destination,
                        "android",
                        "idle_hashtag_post_search_unavailable" if is_idle_exact_tag_job else "related_hashtag_search_unavailable",
                        job_id=job.get("job_id", working_path.stem),
                        hashtags=hashtags,
                        error=hashtag_error,
                    )
                    print(f"Android hashtag post-count worker failed: {error}", file=sys.stderr)
                if _is_android_device_connection_error(hashtag_error):
                    _requeue_android_metric_job(paths, working_path, job, hashtag_error)
                    reset_connection = getattr(enricher, "reset_connection", None)
                    if callable(reset_connection):
                        reset_connection()
                    counts = android_metric_queue_counts(destination)
                    _write_android_metric_worker_status(
                        paths,
                        state="waiting_for_device",
                        processed=processed,
                        collected=collected,
                        unavailable=unavailable,
                        consecutive_failures=0,
                        device_error=hashtag_error,
                        **counts,
                    )
                    append_collection_log(
                        destination,
                        "android",
                        "worker_waiting_for_device",
                        job_kind=tag_job_kind,
                        error=hashtag_error,
                        retry_seconds=ANDROID_DEVICE_RECONNECT_SECONDS,
                    )
                    time.sleep(max(0.1, ANDROID_DEVICE_RECONNECT_SECONDS))
                    continue
                if hashtag_rows:
                    write_hashtag_post_counts(destination, hashtag_rows)
                    collected += sum(row.get("status") == "collected" for row in hashtag_rows)
                    unavailable += sum(row.get("status") != "collected" for row in hashtag_rows)
                elif hashtag_status != "collected":
                    unavailable += len(hashtags)
                paths["hashtag_completed"].mkdir(parents=True, exist_ok=True)
                write_json_atomic(
                    paths["hashtag_completed"] / working_path.name,
                    {
                        "job_id": str(job.get("job_id", working_path.stem)),
                        "completed_at": isoformat_utc(),
                        "status": hashtag_status,
                        "error": hashtag_error,
                    },
                )
                working_path.unlink(missing_ok=True)
                processed += 1
                continue
            delay = android_metric_job_delay_seconds(job)
            if delay:
                time.sleep(delay)
            target = job.get("target") if isinstance(job.get("target"), dict) else {}
            try:
                attempts = max(0, int(job.get("attempts", 0) or 0))
                ui_timeout_attempts = max(0, int(job.get("ui_timeout_attempts", 0) or 0))
                infrastructure_attempts = max(ui_timeout_attempts, int(job.get("infrastructure_attempts", 0) or 0))
                if "infrastructure_attempts" not in job and (
                    _is_android_ui_read_failure(job.get("last_device_error"))
                    or _is_android_device_connection_error(job.get("last_device_error"))
                ):
                    infrastructure_attempts = attempts
            except (TypeError, ValueError):
                attempts = 0
                ui_timeout_attempts = 0
                infrastructure_attempts = 0
            normal_attempts = max(0, attempts - infrastructure_attempts)
            python_metrics = python_metrics_for_android_job(destination, job)
            diagnostics.begin_media(
                current_url=str(target.get("url", "")),
                collection_number=job.get("collection_number"),
            )
            append_collection_log(
                destination,
                "android",
                "reel_metric_started",
                job_id=job.get("job_id", working_path.stem),
                url=target.get("url", ""),
                delayed_seconds=delay,
                previous_attempts=attempts,
            )
            result = AndroidMetricResult(
                status="unavailable",
                error=(
                    f"Android retry limit ({ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL}) was already exhausted."
                ),
            )
            device_connection_error = ""
            ui_dump_timeout_error = ""
            ui_read_attempted = False
            while normal_attempts < ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL:
                attempts += 1
                normal_attempts += 1
                if normal_attempts > 1:
                    diagnostics.record_retry(
                        stage=diagnostics.current_stage or "OPEN_REEL",
                        target="android_reel_metrics",
                        attempt=normal_attempts,
                        total=ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL,
                        reason=result.error or "Android metrics unavailable",
                        previous_wait=max(0.1, float(ui_delay_seconds)),
                    )
                job["attempts"] = attempts
                write_json_atomic(working_path, job)
                if attempts > 1:
                    prepare_reel_retry = getattr(enricher, "prepare_reel_retry", None)
                    if callable(prepare_reel_retry):
                        try:
                            prepare_reel_retry()
                        except Exception as error:
                            append_collection_log(
                                destination,
                                "android",
                                "reel_retry_reset_failed",
                                job_id=job.get("job_id", working_path.stem),
                                url=target.get("url", ""),
                                attempt=attempts,
                                error=str(error)[:500],
                            )
                try:
                    ui_read_attempted = True
                    result = enricher.enrich(
                        str(target.get("url", "")), recollection=bool(job.get("recollection")),
                        expected_username=str(target.get("username") or ""),
                    )
                except Exception as error:
                    result = AndroidMetricResult(status="unavailable", error=str(error)[:500])
                if _is_android_ui_read_failure(result.error):
                    infrastructure_attempts += 1
                    normal_attempts -= 1
                    job["infrastructure_attempts"] = infrastructure_attempts
                    if _is_android_ui_dump_timeout(result.error):
                        ui_timeout_attempts += 1
                        job["ui_timeout_attempts"] = ui_timeout_attempts
                    job["last_ui_error"] = result.error
                    ui_dump_timeout_error = result.error
                    break
                if _is_android_device_connection_error(result.error):
                    infrastructure_attempts += 1
                    normal_attempts -= 1
                    job["infrastructure_attempts"] = infrastructure_attempts
                    device_connection_error = result.error
                    break
                if result.status == "skipped":
                    break
                if result.status == "collected":
                    mismatches = compare_python_and_android_counts(
                        python_metrics,
                        {field: value for field, value in result.metrics.items() if field not in ANDROID_COLLECTION_FIELDS},
                    )
                    if not mismatches:
                        break
                    append_collection_log(
                        destination,
                        "android",
                        "reel_metric_mismatch",
                        job_id=job.get("job_id", working_path.stem),
                        url=target.get("url", ""),
                        attempt=attempts,
                        maximum_attempts=ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL,
                        mismatches=mismatches,
                    )
                    detail = format_android_mismatch(attempts, mismatches)
                    job["android_mismatch"] = " | ".join(
                        filter(None, (str(job.get("android_mismatch", "")), detail))
                    )
                    write_json_atomic(working_path, job)
                    result = AndroidMetricResult(
                        status="unavailable",
                        error=(
                            "Python/Android count mismatch: "
                            + ", ".join(sorted(mismatches))
                        ),
                    )
                append_collection_log(
                    destination,
                    "android",
                    "reel_metric_retry",
                    job_id=job.get("job_id", working_path.stem),
                    url=target.get("url", ""),
                    attempt=attempts,
                    maximum_attempts=ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL,
                    error=result.error,
                )
                if normal_attempts < ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL:
                    time.sleep(max(0.1, float(ui_delay_seconds)))
            if ui_dump_timeout_error:
                diagnostics.finish_media(
                    "DEFERRED", success=False, error=ui_dump_timeout_error,
                    count_as_failure=False,
                )
                ui_read_failures = max(0, int(job.get("ui_read_failures", 0) or 0)) + 1
                job["ui_read_failures"] = ui_read_failures
                retry_seconds = min(
                    ANDROID_UI_MAX_RETRY_DELAY_SECONDS,
                    ANDROID_UI_RETRY_DELAY_SECONDS * 2 ** min(ui_read_failures - 1, 6),
                )
                job["next_attempt_at"] = isoformat_utc(utc_now() + timedelta(seconds=retry_seconds))
                _requeue_android_metric_job(
                    paths, working_path, job, ui_dump_timeout_error, to_tail=True,
                    show_in_terminal=not attach_existing,
                )
                ui_timeout_streak += 1
                ui_failed_targets.add(str(target.get("url", "")))
                _write_android_metric_worker_status(
                    paths,
                    state="requeued_after_ui_read_failure",
                    processed=processed,
                    collected=collected,
                    unavailable=unavailable,
                    consecutive_failures=consecutive_failures,
                    ui_timeout_streak=ui_timeout_streak,
                    distinct_ui_failures=len(ui_failed_targets),
                    ui_read_failures=ui_read_failures,
                    retry_seconds=retry_seconds,
                    device_error=ui_dump_timeout_error,
                    **android_metric_queue_counts(destination),
                )
                append_collection_log(
                    destination, "android", "worker_ui_dump_timeout" if _is_android_ui_dump_timeout(ui_dump_timeout_error) else "worker_ui_unreadable",
                    url=target.get("url", ""),
                    error=ui_dump_timeout_error,
                    consecutive_timeouts=ui_timeout_streak,
                    retry_seconds=retry_seconds,
                    ui_read_failures=ui_read_failures,
                )
                # One troublesome Reel must not restart the app repeatedly.
                # Keep draining other jobs during recovery cooldowns.
                if (ui_timeout_streak >= 3
                        and (len(ui_failed_targets) >= 3 or ui_read_failures >= 3)
                        and time.monotonic() >= next_app_recovery_at):
                    recovery_timeout_seconds = 12.0
                    app_recovery_stage = min(app_recovery_stage + 1, 3)
                    _write_android_metric_worker_status(paths, state="recovering_ui", **android_metric_queue_counts(destination))
                    append_collection_log(destination, "android", "worker_ui_recovery_started", targets=sorted(ui_failed_targets), stage=app_recovery_stage, timeout_seconds=recovery_timeout_seconds)
                    try:
                        recovery_error = enricher.recover_app(str(target.get("url", "")), timeout_seconds=recovery_timeout_seconds)
                    except Exception as error:
                        recovery_error = str(error)
                    if recovery_error:
                        append_collection_log(destination, "android", "worker_ui_recovery_failed", error=recovery_error)
                    else:
                        append_collection_log(destination, "android", "worker_ui_recovery_succeeded")
                        app_recovery_stage = 0
                        reset_ui_read_timeout = getattr(enricher, "reset_ui_read_timeout", None)
                        if callable(reset_ui_read_timeout):
                            reset_ui_read_timeout()
                    recovery_delay = min(600.0, ANDROID_UI_RECOVERY_COOLDOWN_SECONDS * 2 ** max(0, app_recovery_stage - 1))
                    next_app_recovery_at = time.monotonic() + recovery_delay
                    append_collection_log(destination, "android", "worker_ui_recovery_cooldown", retry_seconds=recovery_delay, app_restarts=app_recovery_stage)
                    ui_failed_targets.clear()
                    ui_timeout_streak = 0
                    _write_android_metric_worker_status(paths, state="requeued_after_app_restart" if recovery_error else "running", device_error=recovery_error, distinct_ui_failures=0, ui_timeout_streak=0, **android_metric_queue_counts(destination))
                continue
            ui_timeout_streak = 0
            if ui_read_attempted and result.status == "collected":
                ui_failed_targets.clear()
                app_recovery_stage = 0
                reset_ui_read_timeout = getattr(enricher, "reset_ui_read_timeout", None)
                if callable(reset_ui_read_timeout):
                    reset_ui_read_timeout()
            if device_connection_error:
                diagnostics.finish_media(
                    "DEFERRED",
                    success=False,
                    error=device_connection_error,
                    count_as_failure=False,
                )
                _requeue_android_metric_job(
                    paths, working_path, job, device_connection_error,
                    show_in_terminal=not attach_existing,
                )
                reset_connection = getattr(enricher, "reset_connection", None)
                if callable(reset_connection):
                    reset_connection()
                consecutive_failures = 0
                _write_android_metric_worker_status(
                    paths,
                    state="waiting_for_device",
                    processed=processed,
                    collected=collected,
                    unavailable=unavailable,
                    consecutive_failures=consecutive_failures,
                    device_error=device_connection_error,
                    **android_metric_queue_counts(destination),
                )
                append_collection_log(
                    destination,
                    "android",
                    "worker_waiting_for_device",
                    error=device_connection_error,
                    retry_seconds=ANDROID_DEVICE_RECONNECT_SECONDS,
                )
                time.sleep(max(0.1, ANDROID_DEVICE_RECONNECT_SECONDS))
                continue
            progress_current, progress_total = android_reel_progress(
                paths,
                job,
                fallback_current=processed_reels + 1,
            )
            terminal_line = android_metric_terminal_line(
                result,
                attempts,
                current=progress_current,
                total=progress_total,
                reel_url=str(target.get("url", "")),
                python_metrics=python_metrics,
            )
            diagnostics.stage_start("SAVE_RESULT")
            try:
                _write_android_metric_completion(paths, working_path, job, result)
            except Exception as error:
                diagnostics.stage_failed("SAVE_RESULT", reason=type(error).__name__, error=str(error))
                diagnostics.finish_media("FAILED", success=False, error=str(error))
                raise
            diagnostics.stage_success("SAVE_RESULT")
            diagnostics.finish_media(
                "SUCCESS" if result.status == "collected" else "FAILED",
                success=result.status == "collected",
                error=result.error,
            )
            pause_reel_when_idle = True
            append_collection_log(
                destination,
                "android",
                "reel_metric_collected" if result.status == "collected" else "reel_metric_unavailable",
                job_id=job.get("job_id", working_path.stem),
                url=target.get("url", ""),
                status=result.status,
                metrics=result.metrics,
                audio_name=result.audio_name,
                like_count_private=result.like_count_private,
                comment_count_disabled=result.comment_count_disabled,
                error=result.error,
                attempts=attempts,
                terminal_line=terminal_line,
            )
            if not attach_existing:
                print(terminal_line, flush=True)
            processed += 1
            processed_reels += 1
            collected += int(result.status == "collected")
            unavailable += int(result.status != "collected")
            if result.status == "collected":
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            if processed % ANDROID_METRIC_QUEUE_EXPORT_BATCH_SIZE == 0:
                apply_completed_android_metric_jobs(destination)
                try:
                    publish_scheduled_android_outputs(destination)
                except OSError as error:
                    print(f"Android public export failed: {error}", file=sys.stderr)
            if consecutive_failures >= COLLECTION_MAX_CONSECUTIVE_FAILURES:
                counts = android_metric_queue_counts(destination)
                _write_android_metric_worker_status(
                    paths,
                    state="backing_off",
                    processed=processed,
                    collected=collected,
                    unavailable=unavailable,
                    consecutive_failures=consecutive_failures,
                    **counts,
                )
                append_collection_log(
                    destination,
                    "android",
                    "worker_backing_off_after_failures",
                    consecutive_failures=consecutive_failures,
                    retry_seconds=ANDROID_METRIC_FAILURE_BACKOFF_SECONDS,
                    pending=counts["pending"],
                    url=str(target.get("url", "")),
                )
                # Android enrichment is optional in the detached worker. A
                # broken app surface must not publish the shared stop signal
                # that also terminates an otherwise healthy Python collector.
                if counts["pending"] or counts["working"]:
                    time.sleep(max(0.1, ANDROID_METRIC_FAILURE_BACKOFF_SECONDS))
                consecutive_failures = 0
    except InterruptedError:
        diagnostics_status = "stopped"
        raise
    except Exception as error:
        recovery_failed = True
        diagnostics_status = "recovery_failed"
        _defer_android_worker_recovery(paths, str(error), **android_metric_queue_counts(destination))
        append_collection_log(destination, "android", "worker_retry_scheduled", error=str(error), traceback=traceback.format_exc())
        return 1
    finally:
        try:
            merge = {"applied": 0, "updated": 0, "pending": 0}
            try:
                _recover_android_metric_working_jobs(paths)
                merge = apply_completed_android_metric_jobs(destination)
            except Exception as error:
                recovery_failed = True
                diagnostics_status = "recovery_failed"
                _defer_android_worker_recovery(paths, str(error), **android_metric_queue_counts(destination))
                append_collection_log(destination, "android", "worker_merge_deferred", error=str(error), traceback=traceback.format_exc())
            try:
                outputs = publish_scheduled_android_outputs(destination)
            except OSError as error:
                outputs = {}
                print(f"Android public export failed: {error}", file=sys.stderr)
            if outputs:
                print("최종 릴스 저장 위치: " + ", ".join(f"{kind.upper()}={path}" for kind, path in outputs.items() if kind.startswith("reels_")), flush=True)
            else:
                local_outputs = [destination / f"reels.{extension}" for extension in ("xlsx", "csv", "json")]
                saved_paths = [str(path) for path in local_outputs if path.exists()]
                if saved_paths:
                    print("최종 릴스 저장 위치: " + ", ".join(saved_paths), flush=True)
            counts = android_metric_queue_counts(destination)
            _write_android_metric_worker_status(
                paths,
                state="recovery_failed" if recovery_failed else "stopped",
                processed=processed,
                collected=collected,
                unavailable=unavailable,
                consecutive_failures=consecutive_failures,
                emulator_restarts=emulator_restarts,
                **counts,
                completed_applied=merge["applied"],
                completed_updated=merge["updated"],
                completed_unmatched=merge["pending"],
            )
            append_collection_log(
                destination,
                "android",
                "worker_stopped",
                processed=processed,
                collected=collected,
                unavailable=unavailable,
                consecutive_failures=consecutive_failures,
                emulator_restarts=emulator_restarts,
                completed_applied=merge["applied"],
                completed_unmatched=merge["pending"],
            )
        finally:
            terminal_log_stop.set()
            if terminal_log_thread is not None:
                terminal_log_thread.join()
            close = getattr(enricher, 'close', None)
            if callable(close):
                close()
            worker_lock.release()
            diagnostics.finish(diagnostics_status)


def start_android_metric_worker(
    data_dir: Path | str,
    *,
    adb_path: Path | str | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = 0.35,
) -> bool:
    """Start one detached worker when this data directory has no live worker."""
    stop_request = read_collection_stop_request(data_dir)
    if stop_request is not None:
        append_collection_log(
            data_dir,
            "android",
            "worker_not_started",
            reason=stop_request.get("reason", "collection failure limit reached"),
        )
        return False
    if collection_pause.is_paused():
        return False
    paths = _android_metric_queue_paths(data_dir)
    starting_lock = AtomicProcessLock(paths["worker_starting"], stale_after_seconds=60)
    if not starting_lock.acquire():
        return False
    try:
        owner = _read_android_metric_job(paths["worker_lock"])
        if owner is not None and process_is_alive(int(owner.get("pid", 0) or 0)):
            return False
        if read_collection_stop_request(data_dir) is not None or collection_pause.is_paused():
            return False
        status = _read_android_metric_job(paths["status"]) or {}
        retrying_recovery = status.get("state") == "recovery_failed"
        if retrying_recovery:
            counts = android_metric_queue_counts(data_dir)
            if not any(counts.values()):
                return False
            retry_at = parse_datetime(status.get("retry_at"))
            if retry_at is None:
                previous_update = parse_datetime(status.get("updated_at")) or utc_now()
                retry_at = previous_update + timedelta(seconds=ANDROID_RECOVERY_RETRY_SECONDS)
                # Persist migration of legacy failure records once, so repeated
                # launch checks cannot extend the cooldown indefinitely.
                status["retry_at"] = isoformat_utc(retry_at)
                write_json_atomic(paths["status"], status)
            if utc_now() < retry_at:
                return False
        launcher = PYTHON_VERSION_ROOT / "scripts" / "instagram_reels_python.py"
        command = [
            sys.executable,
            str(launcher),
            "android-worker",
            "--detached",
            "--data-dir",
            str(Path(data_dir).resolve()),
            "--android-ui-delay-seconds",
            str(max(0.1, float(ui_delay_seconds))),
        ]
        if adb_path:
            command.extend(["--android-adb-path", str(adb_path)])
        if device_id:
            command.extend(["--android-device-id", str(device_id)])
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        append_collection_log(data_dir, "android", "worker_launch_requested", command=command)
        if retrying_recovery:
            _write_android_metric_worker_status(
                paths, state="restart_requested", retry_at="",
                previous_recovery_error=str(status.get("stop_reason") or status.get("device_error") or ""),
            )
        process = subprocess.Popen(
            command,
            cwd=PYTHON_VERSION_ROOT,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        write_json_atomic(
            paths["worker_starting"],
            {"pid": process.pid, "started_at": isoformat_utc()},
        )
        starting_lock.acquired = False
        return True
    except OSError as error:
        _defer_android_worker_recovery(paths, str(error), **android_metric_queue_counts(data_dir))
        append_collection_log(data_dir, "android", "worker_launch_deferred", error=str(error), traceback=traceback.format_exc())
        return False
    finally:
        starting_lock.release()

