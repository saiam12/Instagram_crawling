from __future__ import annotations

import json
import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


LOG_TIMEZONE = timezone(timedelta(hours=9))
_SECRET_PATTERN = re.compile(
    r"(?i)(?:authorization|cookie|set-cookie|token|session|password|passwd|secret)\s*[:=]\s*[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_SHORTCODE_PATTERN = re.compile(r"/(?:reels?|p)/([^/?#]+)", re.IGNORECASE)


def _timestamp() -> str:
    return datetime.now(LOG_TIMEZONE).isoformat(timespec="milliseconds")


def _format_elapsed(seconds: float) -> str:
    whole = max(0, int(seconds))
    hours, remainder = divmod(whole, 3_600)
    minutes, seconds_value = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_value:02d}"


def sanitize_url(value: object) -> str:
    """Keep only an URL's origin and path; query strings may contain secrets."""
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "[REDACTED_URL]"
    if not parsed.scheme or not parsed.netloc:
        return text
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sanitize_text(value: object) -> str:
    text = str(value or "")
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(0).split(':', 1)[0].split('=', 1)[0]}=[REDACTED]", text)
    return _URL_PATTERN.sub(lambda match: sanitize_url(match.group(0)), text)


def _safe_value(key: str, value: object) -> object:
    normalized_key = key.casefold()
    if any(token in normalized_key for token in ("authorization", "cookie", "token", "password", "secret", "session")):
        return "[REDACTED]"
    if "url" in normalized_key:
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


class CollectorDiagnostics:
    """Write human-readable and JSONL diagnostics for one Android run."""

    def __init__(self, data_dir: Path | str, *, run_mode: str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.run_mode = run_mode if run_mode in {"foreground", "background"} else "foreground"
        self.started_monotonic = time.monotonic()
        self.started_at = _timestamp()
        stamp = datetime.now(LOG_TIMEZONE).strftime("%Y-%m-%d_%H%M%S")
        self.logs_dir = self.data_dir / "logs"
        self.diagnostics_dir = self.data_dir / "diagnostics"
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
        self._current: dict[str, object] = {}
        self._current_stage = ""
        self._stage_started: dict[str, float] = {}
        self._ui_status: dict[str, bool | None] = {
            "media_render_ok": None,
            "video_render_ok": None,
            "profile_render_ok": None,
            "metadata_render_ok": None,
        }
        self._first_rate_limit: dict[str, object] | None = None
        self._last_successful: dict[str, object] = {}
        self._finished = False
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        candidate_log = self.logs_dir / f"instagram_collector_{stamp}.log"
        candidate_events = self.logs_dir / f"instagram_events_{stamp}.jsonl"
        duplicate_number = 1
        while candidate_log.exists() or candidate_events.exists():
            candidate_log = self.logs_dir / f"instagram_collector_{stamp}_{duplicate_number}.log"
            candidate_events = self.logs_dir / f"instagram_events_{stamp}_{duplicate_number}.jsonl"
            duplicate_number += 1
        self.log_path = candidate_log
        self.events_path = candidate_events
        self.emit("COLLECTOR_START", start_time=self.started_at)
        self.emit(
            "NETWORK_STATUS_UNAVAILABLE",
            reason="The Android ADB/UIAutomator driver does not intercept Instagram HTTP responses; no direct API client is used.",
            network_status="unavailable",
        )

    @property
    def current_stage(self) -> str:
        return self._current_stage

    def _elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    def _trim_windows(self, now: float) -> None:
        for values in (self._media_timestamps, self._retry_timestamps):
            while values and now - values[0] > 300:
                values.popleft()

    def _recent_media(self, seconds: float, now: float) -> int:
        return sum(1 for timestamp in self._media_timestamps if now - timestamp <= seconds)

    def _stats(self) -> dict[str, object]:
        now = time.monotonic()
        self._trim_windows(now)
        elapsed = self._elapsed_seconds()
        runtime_minutes = max(elapsed / 60, 1 / 60)
        recent_one = self._recent_media(60, now)
        recent_five = self._recent_media(300, now)
        average_duration = sum(self._media_durations) / len(self._media_durations) if self._media_durations else 0.0
        peak_one = 0
        for timestamp in self._media_timestamps:
            peak_one = max(peak_one, sum(1 for candidate in self._media_timestamps if 0 <= timestamp - candidate <= 60))
        return {
            "elapsed": _format_elapsed(elapsed),
            "elapsed_seconds": round(elapsed, 3),
            "media_per_min": round(self.attempted_media / runtime_minutes, 2),
            "success_per_min": round(self.success_count / runtime_minutes, 2),
            "failure_per_min": round(self.failed_count / runtime_minutes, 2),
            "recent_1min_media": recent_one,
            "recent_5min_media": recent_five,
            "avg_processing_time": round(average_duration, 3),
            "peak_1min_media": peak_one,
            "retry_count_recent": sum(1 for timestamp in self._retry_timestamps if now - timestamp <= 300),
        }

    def _base_event(self, event: str) -> dict[str, object]:
        stats = self._stats()
        payload: dict[str, object] = {
            "timestamp": _timestamp(),
            "event": event,
            "run_mode": self.run_mode,
            "elapsed_seconds": stats["elapsed_seconds"],
            "attempted_media": self.attempted_media,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "current_stage": self._current_stage,
        }
        payload.update(self._current)
        return payload

    def _append_human(self, line: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip() + "\n")

    def emit(self, event: str, **values: object) -> dict[str, object]:
        payload = self._base_event(event)
        payload.update({key: _safe_value(key, value) for key, value in values.items()})
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        details = " ".join(
            f"{key}={value}" for key, value in payload.items()
            if key not in {"timestamp", "event", "run_mode", "elapsed_seconds", "attempted_media", "success_count", "failed_count", "current_stage"}
            and value not in (None, "", False)
        )
        line = (
            f"[{payload['timestamp']}] [{self.run_mode}] {event} "
            f"elapsed={_format_elapsed(float(payload['elapsed_seconds']))} "
            f"attempted={self.attempted_media} success={self.success_count} failed={self.failed_count}"
        )
        if self._current_stage:
            line += f" stage={self._current_stage}"
        if details:
            line += f" | {details}"
        self._append_human(line)
        return payload

    def begin_media(self) -> int:
        now = time.monotonic()
        self.attempted_media += 1
        self._media_started_at = now
        self._media_timestamps.append(now)
        self._current = {"media_index": self.attempted_media}
        self._current_stage = ""
        self._ui_status = {key: None for key in self._ui_status}
        self.emit("MEDIA_START")
        return self.attempted_media

    def update_media(
        self,
        *,
        shortcode: str = "",
        media_id: str = "",
        current_url: str = "",
        username: str = "",
        stage: str | None = None,
    ) -> None:
        if shortcode:
            self._current["shortcode"] = shortcode
        if media_id:
            self._current["media_id"] = media_id
        if current_url:
            sanitized = sanitize_url(current_url)
            self._current["current_url"] = sanitized
            if not self._current.get("shortcode"):
                match = _SHORTCODE_PATTERN.search(sanitized)
                if match:
                    self._current["shortcode"] = match.group(1)
        if username:
            self._current["username"] = username
        if stage:
            self._current_stage = stage

    def finish_media(
        self,
        result: str,
        *,
        success: bool,
        error: str = "",
        count_as_failure: bool = True,
    ) -> None:
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
            "MEDIA_RESULT",
            result=result,
            success=success,
            duration_seconds=round(duration, 3),
            duration=f"{duration:.2f}s",
            error=error,
            ui_status=dict(self._ui_status),
        )
        self.emit("STATS", **self._stats())
        self._media_started_at = None

    def stage_start(self, stage: str, **values: object) -> None:
        self._current_stage = stage
        self._stage_started[stage] = time.monotonic()
        self.emit("STAGE_START", stage=stage, **values)

    def _stage_duration(self, stage: str) -> float | None:
        started = self._stage_started.pop(stage, None)
        return round(max(0.0, time.monotonic() - started), 3) if started is not None else None

    def stage_success(self, stage: str, **values: object) -> None:
        self._current_stage = stage
        duration = self._stage_duration(stage)
        self.emit("STAGE_SUCCESS", stage=stage, stage_duration_seconds=duration, **values)

    def stage_failed(self, stage: str, *, reason: str, **values: object) -> None:
        self._current_stage = stage
        duration = self._stage_duration(stage)
        if stage == "OPEN_PROFILE":
            self._ui_status["profile_render_ok"] = False
        self.emit("STAGE_FAILED", stage=stage, reason=reason, stage_duration_seconds=duration, **values)
        if reason == "ELEMENT_LOOKUP_FAILED":
            self.emit("ELEMENT_LOOKUP_FAILED", target=stage)
        if reason == "METADATA_MISSING":
            self.ui_event("METADATA_MISSING", fields=values.get("fields", ""))

    def stage_timeout(self, stage: str, **values: object) -> None:
        self._current_stage = stage
        duration = self._stage_duration(stage)
        if stage == "OPEN_PROFILE":
            self._ui_status["profile_render_ok"] = False
        self.element_timeouts += 1
        self.emit("STAGE_TIMEOUT", stage=stage, stage_duration_seconds=duration, **values)
        self.emit("ELEMENT_TIMEOUT", target=stage)

    def ui_event(self, event: str, **values: object) -> None:
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

    def record_retry(
        self,
        *,
        stage: str,
        target: str,
        attempt: int,
        total: int,
        reason: str,
        previous_wait: float,
    ) -> None:
        now = time.monotonic()
        self.retry_count += 1
        self._retry_timestamps.append(now)
        self._current_stage = stage
        self.emit(
            "RETRY",
            retry_count=self.retry_count,
            retry_reason=reason,
            retry_target=target,
            attempt=f"{attempt}/{total}",
            previous_wait_seconds=round(previous_wait, 3),
        )

    def rate_limit_suspected(self, reason: str) -> None:
        self.rate_limit_suspected_count += 1
        event = self.emit("RATE_LIMIT_SUSPECTED", reason=reason, network_status="unavailable")
        if self._first_rate_limit is None:
            snapshot = self._snapshot_payload(event)
            self._first_rate_limit = snapshot
            stamp = datetime.now(LOG_TIMEZONE).strftime("%Y-%m-%d_%H%M%S")
            destination = self.diagnostics_dir / f"rate_limit_{stamp}.json"
            suffix = 1
            while destination.exists():
                destination = self.diagnostics_dir / f"rate_limit_{stamp}_{suffix}.json"
                suffix += 1
            destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._append_human("========== RATE LIMIT SUSPECTED ==========")
            self._append_human(json.dumps(snapshot, ensure_ascii=False, indent=2))
            self._append_human("===========================================")
            print("========== RATE LIMIT SUSPECTED ==========")
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            print("===========================================", flush=True)

    def _snapshot_payload(self, event: dict[str, object]) -> dict[str, object]:
        stats = self._stats()
        return {
            "timestamp": event.get("timestamp", _timestamp()),
            "elapsed_seconds": stats["elapsed_seconds"],
            "attempted_media": self.attempted_media,
            "success_media": self.success_count,
            "failed_media": self.failed_count,
            "current_media": self._current.get("media_id") or self._current.get("shortcode", ""),
            "current_shortcode": self._current.get("shortcode", ""),
            "current_url": self._current.get("current_url", ""),
            "username": self._current.get("username", ""),
            "current_stage": self._current_stage,
            "run_mode": self.run_mode,
            "recent_1min_media": stats["recent_1min_media"],
            "recent_5min_media": stats["recent_5min_media"],
            "retry_count_recent": stats["retry_count_recent"],
            "ui_status": dict(self._ui_status),
            "network_status": "unavailable",
        }

    def adb_command(self, arguments: tuple[str, ...] | list[str], *, duration: float, status: str, error: str = "") -> None:
        self.emit(
            "ADB_COMMAND",
            command=" ".join(str(argument) for argument in arguments),
            duration_ms=round(max(0.0, duration) * 1_000, 1),
            status=status,
            error=error,
        )

    def finish(self, status: str, *, error: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        stats = self._stats()
        self.emit(
            "COLLECTOR_FINISH",
            status=status,
            error=error,
            retry_count=self.retry_count,
            element_timeouts=self.element_timeouts,
            ui_render_failures=self.ui_render_failures,
            http_429_confirmed=self.http_429_confirmed,
            rate_limit_suspected_count=self.rate_limit_suspected_count,
            **stats,
        )
        first_rate_limit = self._first_rate_limit
        summary = [
            "========== COLLECTOR SUMMARY ==========",
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
        if first_rate_limit:
            summary.extend([
                "",
                "First rate limit suspected:",
                f"  elapsed: {_format_elapsed(float(first_rate_limit['elapsed_seconds']))}",
                f"  attempted media: {first_rate_limit['attempted_media']}",
                f"  successful media: {first_rate_limit['success_media']}",
                f"  stage: {first_rate_limit['current_stage']}",
            ])
        if self._last_successful:
            summary.extend([
                "",
                "Last successful media:",
                f"  shortcode: {self._last_successful.get('shortcode', '')}",
                f"  username: {self._last_successful.get('username', '')}",
            ])
        summary.extend(["", f"Status: {status}", "======================================="])
        summary_text = "\n".join(summary)
        self._append_human(summary_text)
        print(summary_text, flush=True)
