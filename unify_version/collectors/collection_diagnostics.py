"""Non-invasive diagnostics shared by the browser and Android collectors."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit


KST = timezone(timedelta(hours=9))
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|cookie|set-cookie|token|session|password|passwd|secret)\s*[:=]\s*[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.I)
_SHORTCODE_PATTERN = re.compile(r"/(?:reels?|p)/([^/?#]+)", re.I)


def _timestamp() -> str:
    return datetime.now(KST).isoformat(timespec="milliseconds")


def _elapsed_text(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3_600)
    minutes, seconds_value = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_value:02d}"


def sanitize_url(value: object) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "[REDACTED_URL]"
    if not parsed.scheme or not parsed.netloc:
        return text
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sanitize_text(value: object) -> str:
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", str(value or ""))
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return _URL_PATTERN.sub(lambda match: sanitize_url(match.group(0)), text)


def _safe_value(key: str, value: object) -> object:
    normalized = key.casefold()
    if any(secret in normalized for secret in ("authorization", "cookie", "token", "password", "secret", "session")):
        return "[REDACTED]"
    if "url" in normalized:
        return sanitize_url(value)
    if isinstance(value, dict):
        return {str(item_key): _safe_value(str(item_key), item_value) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(key, item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


def sanitize_log_value(key: str, value: object) -> object:
    """Sanitize a value before it enters either legacy or diagnostic logs."""
    return _safe_value(key, value)


class CollectorDiagnostics:
    """Write one human log, one JSONL stream, and the first limit snapshot."""

    def __init__(self, data_dir: Path | str, *, run_mode: str, component: str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.run_mode = run_mode if run_mode in {"foreground", "background"} else "foreground"
        self.component = component
        self.started_monotonic = time.monotonic()
        self.started_at = _timestamp()
        self.attempted_media = 0
        self.success_count = 0
        self.failed_count = 0
        self.retry_count = 0
        self.element_timeouts = 0
        self.ui_render_failures = 0
        self.http_429_confirmed = 0
        self.rate_limit_suspected_count = 0
        self._media_started_at: float | None = None
        self._media_timestamps: deque[float] = deque()
        self._retry_timestamps: deque[float] = deque()
        self._media_durations: list[float] = []
        self._recent_network: deque[dict[str, object]] = deque(maxlen=50)
        self._peak_one_minute = 0
        self._current: dict[str, object] = {}
        self._current_stage = ""
        self._stage_started: dict[str, float] = {}
        self._ui_status: dict[str, bool | None] = {
            "media_render_ok": None,
            "video_render_ok": None,
            "profile_render_ok": None,
            "metadata_render_ok": None,
        }
        self._first_limit: dict[str, object] | None = None
        self._last_successful: dict[str, object] = {}
        self._suspected_keys: set[tuple[int, str, str]] = set()
        self._finished = False
        self._lock = threading.RLock()
        logs_dir = self.data_dir / "logs"
        self.diagnostics_dir = self.data_dir / "diagnostics"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Diagnostics are observational and must never stop collection.
            # Later writes use the same best-effort policy.
            pass
        stamp = datetime.now(KST).strftime("%Y-%m-%d_%H%M%S")
        self.log_path, self.events_path = self._unique_log_paths(logs_dir, stamp)
        self.emit("COLLECTOR_START", start_time=self.started_at)

    @staticmethod
    def _unique_log_paths(logs_dir: Path, stamp: str) -> tuple[Path, Path]:
        suffix = ""
        number = 0
        while True:
            log_path = logs_dir / f"instagram_collector_{stamp}{suffix}.log"
            events_path = logs_dir / f"instagram_events_{stamp}{suffix}.jsonl"
            if not log_path.exists() and not events_path.exists():
                return log_path, events_path
            number += 1
            suffix = f"_{number}"

    @property
    def media_active(self) -> bool:
        return self._media_started_at is not None

    @property
    def current_stage(self) -> str:
        return self._current_stage

    def _elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    def _trim_windows(self, now: float) -> None:
        for timestamps in (self._media_timestamps, self._retry_timestamps):
            while timestamps and now - timestamps[0] > 300:
                timestamps.popleft()

    def _stats(self) -> dict[str, object]:
        now = time.monotonic()
        self._trim_windows(now)
        elapsed = self._elapsed_seconds()
        runtime_minutes = max(elapsed / 60, 1 / 60)
        recent_one = sum(now - timestamp <= 60 for timestamp in self._media_timestamps)
        recent_five = len(self._media_timestamps)
        average_duration = sum(self._media_durations) / len(self._media_durations) if self._media_durations else 0.0
        return {
            "elapsed": _elapsed_text(elapsed),
            "elapsed_seconds": round(elapsed, 3),
            "media_per_min": round(self.attempted_media / runtime_minutes, 2),
            "success_per_min": round(self.success_count / runtime_minutes, 2),
            "failure_per_min": round(self.failed_count / runtime_minutes, 2),
            "recent_1min_media": recent_one,
            "recent_5min_media": recent_five,
            "avg_processing_time": round(average_duration, 3),
            "peak_1min_media": self._peak_one_minute,
            "retry_count_recent": sum(now - timestamp <= 300 for timestamp in self._retry_timestamps),
        }

    def _base_event(self, event: str) -> dict[str, object]:
        return {
            "timestamp": _timestamp(),
            "event": event,
            "component": self.component,
            "run_mode": self.run_mode,
            "elapsed_seconds": round(self._elapsed_seconds(), 3),
            "attempted_media": self.attempted_media,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "current_stage": self._current_stage,
            **self._current,
        }

    def emit(self, event: str, **values: object) -> dict[str, object]:
        with self._lock:
            payload = self._base_event(event)
            payload.update({key: _safe_value(key, value) for key, value in values.items()})
            try:
                with self.events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                excluded = {
                    "timestamp", "event", "component", "run_mode", "elapsed_seconds",
                    "attempted_media", "success_count", "failed_count", "current_stage",
                }
                details = " ".join(
                    f"{key}={value}" for key, value in payload.items()
                    if key not in excluded and value not in (None, "", False)
                )
                line = (
                    f"[{payload['timestamp']}] [{self.component}/{self.run_mode}] {event} "
                    f"elapsed={_elapsed_text(float(payload['elapsed_seconds']))} "
                    f"attempted={self.attempted_media} success={self.success_count} failed={self.failed_count}"
                )
                if self._current_stage:
                    line += f" stage={self._current_stage}"
                if details:
                    line += f" | {details}"
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass
            return payload

    def begin_media(self, *, current_url: str = "", shortcode: str = "", username: str = "") -> None:
        with self._lock:
            now = time.monotonic()
            self.attempted_media += 1
            self._media_started_at = now
            self._media_timestamps.append(now)
            self._trim_windows(now)
            self._peak_one_minute = max(
                self._peak_one_minute,
                sum(now - timestamp <= 60 for timestamp in self._media_timestamps),
            )
            self._current = {"media_index": self.attempted_media}
            self._current_stage = ""
            self._stage_started.clear()
            self._ui_status = {key: None for key in self._ui_status}
            self.update_media(current_url=current_url, shortcode=shortcode, username=username)
            self.emit("MEDIA_START")

    def update_media(self, *, current_url: str = "", shortcode: str = "", username: str = "") -> None:
        with self._lock:
            if current_url:
                sanitized = sanitize_url(current_url)
                self._current["current_url"] = sanitized
                if not shortcode and not self._current.get("shortcode"):
                    match = _SHORTCODE_PATTERN.search(sanitized)
                    shortcode = match.group(1) if match else ""
            if shortcode:
                self._current["shortcode"] = shortcode
            if username:
                self._current["username"] = username

    def finish_media(self, result: str, *, success: bool, error: str = "", count_as_failure: bool = True) -> None:
        with self._lock:
            if self._media_started_at is None:
                return
            duration = max(0.0, time.monotonic() - self._media_started_at)
            self._media_durations.append(duration)
            if success:
                self.success_count += 1
                self._last_successful = {
                    "shortcode": self._current.get("shortcode", ""),
                    "username": self._current.get("username", ""),
                }
            elif count_as_failure:
                self.failed_count += 1
            self.emit(
                "MEDIA_RESULT", result=result, success=success, error=error,
                duration_seconds=round(duration, 3), ui_status=dict(self._ui_status),
            )
            self.emit("STATS", **self._stats())
            self._media_started_at = None

    def stage_start(self, stage: str, **values: object) -> None:
        with self._lock:
            self._current_stage = stage
            self._stage_started[stage] = time.monotonic()
            self.emit("STAGE_START", stage=stage, **values)

    def _stage_duration(self, stage: str) -> float | None:
        started = self._stage_started.pop(stage, None)
        return round(time.monotonic() - started, 3) if started is not None else None

    def stage_success(self, stage: str, **values: object) -> None:
        with self._lock:
            self._current_stage = stage
            self.emit("STAGE_SUCCESS", stage=stage, stage_duration_seconds=self._stage_duration(stage), **values)

    def stage_failed(self, stage: str, *, reason: str, **values: object) -> None:
        with self._lock:
            self._current_stage = stage
            self.emit("STAGE_FAILED", stage=stage, reason=reason, stage_duration_seconds=self._stage_duration(stage), **values)
            if reason == "ELEMENT_LOOKUP_FAILED":
                self.emit("ELEMENT_LOOKUP_FAILED", target=values.get("target", stage))

    def stage_timeout(self, stage: str, **values: object) -> None:
        with self._lock:
            self._current_stage = stage
            self.element_timeouts += 1
            self.emit("STAGE_TIMEOUT", stage=stage, stage_duration_seconds=self._stage_duration(stage), **values)
            self.emit("ELEMENT_TIMEOUT", target=values.get("target", stage))

    def ui_event(self, event: str, **values: object) -> None:
        with self._lock:
            if event == "MEDIA_RENDER_OK":
                self._ui_status["media_render_ok"] = True
            elif event == "UI_RENDER_FAILED":
                self._ui_status["media_render_ok"] = False
                self.ui_render_failures += 1
            elif event == "VIDEO_RENDER_OK":
                self._ui_status["video_render_ok"] = True
            elif event == "PROFILE_RENDER_OK":
                self._ui_status["profile_render_ok"] = True
            elif event == "METADATA_RENDER_OK":
                self._ui_status["metadata_render_ok"] = True
            elif event == "METADATA_MISSING":
                self._ui_status["metadata_render_ok"] = False
            self.emit(event, ui_status=dict(self._ui_status), **values)

    def record_retry(self, *, stage: str, target: str, attempt: int, total: int, reason: str, previous_wait: float) -> None:
        with self._lock:
            self.retry_count += 1
            self._retry_timestamps.append(time.monotonic())
            self._current_stage = stage
            self.emit(
                "RETRY", retry_count=self.retry_count, retry_reason=reason,
                retry_target=target, attempt=f"{attempt}/{total}",
                previous_wait_seconds=round(max(0.0, previous_wait), 3),
            )

    def network_unavailable(self, reason: str) -> None:
        self.emit("NETWORK_STATUS_UNAVAILABLE", network_status="unavailable", reason=reason)

    def network_response(self, *, url: str, status: int, resource_type: str = "", duration_ms: float | None = None) -> None:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        normalized_host = host.casefold()
        if normalized_host != "instagram.com" and not normalized_host.endswith(".instagram.com"):
            return
        # Keep the passive trace useful without writing every image/video chunk.
        # Error responses are always retained regardless of resource type.
        normalized_resource_type = resource_type.casefold()
        if normalized_resource_type not in {"document", "fetch", "xhr"} and status < 400:
            return
        values: dict[str, object] = {
            "timestamp": _timestamp(), "host": host, "path": parsed.path,
            "status": status, "resource_type": normalized_resource_type,
        }
        if duration_ms is not None and duration_ms >= 0:
            values["duration_ms"] = round(duration_ms, 1)
        with self._lock:
            self._recent_network.append(values)
        self.emit("NETWORK_RESPONSE", **values)

    def http_429(self, *, url: str, resource_type: str = "", duration_ms: float | None = None, retry_after: float | None = None) -> None:
        with self._lock:
            self.http_429_confirmed += 1
            event = self.emit(
                "HTTP_429_CONFIRMED", current_url=url, resource_type=resource_type,
                duration_ms=duration_ms, retry_after_seconds=retry_after,
                ui_status=dict(self._ui_status), network_status="available",
            )
            self._write_first_limit_snapshot(event, "HTTP_429_CONFIRMED")

    def rate_limit_suspected(self, reason: str) -> None:
        with self._lock:
            key = (self.attempted_media, self._current_stage, reason.casefold())
            if key in self._suspected_keys:
                return
            self._suspected_keys.add(key)
            self.rate_limit_suspected_count += 1
            event = self.emit(
                "RATE_LIMIT_SUSPECTED", reason=reason,
                ui_status=dict(self._ui_status), network_status="unavailable",
            )
            self._write_first_limit_snapshot(event, "RATE_LIMIT_SUSPECTED")

    def _write_first_limit_snapshot(self, event: dict[str, object], event_name: str) -> None:
        if self._first_limit is not None:
            return
        stats = self._stats()
        snapshot: dict[str, object] = {
            "event": event_name,
            "timestamp": event.get("timestamp", _timestamp()),
            "elapsed_seconds": stats["elapsed_seconds"],
            "attempted_media": self.attempted_media,
            "success_media": self.success_count,
            "failed_media": self.failed_count,
            "current_media": self._current.get("shortcode", ""),
            "current_stage": self._current_stage,
            "run_mode": self.run_mode,
            "component": self.component,
            "recent_1min_media": stats["recent_1min_media"],
            "recent_5min_media": stats["recent_5min_media"],
            "retry_count_recent": stats["retry_count_recent"],
            "ui_status": dict(self._ui_status),
            "network_status": "available" if event_name == "HTTP_429_CONFIRMED" else "unavailable",
            "recent_network": list(self._recent_network),
        }
        self._first_limit = snapshot
        stamp = datetime.now(KST).strftime("%Y-%m-%d_%H%M%S")
        destination = self.diagnostics_dir / f"rate_limit_{stamp}.json"
        suffix = 0
        while destination.exists():
            suffix += 1
            destination = self.diagnostics_dir / f"rate_limit_{stamp}_{suffix}.json"
        try:
            destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
        self.emit("RATE_LIMIT_SNAPSHOT_WRITTEN", snapshot_path=destination)

    def adb_command(self, arguments: Sequence[str], *, duration: float, status: str, error: str = "") -> None:
        self.emit(
            "ADB_COMMAND", command=" ".join(str(argument) for argument in arguments),
            duration_ms=round(max(0.0, duration) * 1_000, 1), status=status, error=error,
        )

    def finish(self, status: str, *, error: str = "") -> None:
        with self._lock:
            if self._finished:
                return
            if self.media_active:
                self.finish_media("FAILED", success=False, error=error or "collector stopped during media processing")
            self._finished = True
            stats = self._stats()
            self.emit(
                "COLLECTOR_FINISH", status=status, error=error,
                retry_count=self.retry_count, element_timeouts=self.element_timeouts,
                ui_render_failures=self.ui_render_failures,
                http_429_confirmed=self.http_429_confirmed,
                rate_limit_suspected_count=self.rate_limit_suspected_count,
                **stats,
            )
            summary = [
                "========== COLLECTOR SUMMARY ==========",
                f"Component: {self.component}",
                f"Run mode: {self.run_mode}",
                f"Runtime: {stats['elapsed']}",
                "",
                f"Attempted media: {self.attempted_media}",
                f"Successful: {self.success_count}",
                f"Failed: {self.failed_count}",
                "",
                f"Average media/min: {stats['media_per_min']}",
                f"Average success/min: {stats['success_per_min']}",
                f"Average failure/min: {stats['failure_per_min']}",
                f"Recent 1-min media: {stats['recent_1min_media']}",
                f"Recent 5-min media: {stats['recent_5min_media']}",
                f"Peak 1-min media count: {stats['peak_1min_media']}",
                f"Average processing time: {stats['avg_processing_time']}s",
                "",
                f"Retry count: {self.retry_count}",
                f"Element timeouts: {self.element_timeouts}",
                f"UI render failures: {self.ui_render_failures}",
                "",
                f"HTTP 429 confirmed: {self.http_429_confirmed}",
                f"Rate limit suspected: {self.rate_limit_suspected_count}",
            ]
            if self._first_limit:
                summary.extend([
                    "", f"First limit event: {self._first_limit['event']}",
                    f"  elapsed: {_elapsed_text(float(self._first_limit['elapsed_seconds']))}",
                    f"  attempted media: {self._first_limit['attempted_media']}",
                    f"  successful media: {self._first_limit['success_media']}",
                    f"  stage: {self._first_limit['current_stage']}",
                ])
            if self._last_successful:
                summary.extend([
                    "", "Last successful media:",
                    f"  shortcode: {self._last_successful.get('shortcode', '')}",
                    f"  username: {self._last_successful.get('username', '')}",
                ])
            summary.extend(["", f"Status: {status}", "======================================="])
            summary_text = "\n".join(summary)
            try:
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(summary_text + "\n")
            except OSError:
                pass
            print(summary_text, flush=True)
