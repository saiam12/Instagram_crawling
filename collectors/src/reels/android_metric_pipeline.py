"""Sequential Android handoff while browser collection continues."""
from __future__ import annotations
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence
from .android_metric_policy import (
    _is_android_device_connection_error,
    _is_android_ui_read_failure,
    compare_python_and_android_counts,
    format_android_mismatch,
)
from .android_metric_worker import read_collection_stop_request
from .android_reel_metrics import ANDROID_COLLECTION_FIELDS, AndroidMetricResult, AndroidReelMetricsEnricher
from .collection_log import append_collection_log
from .collection_settings import (
    ANDROID_DEVICE_RECONNECT_SECONDS,
    ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL,
    ANDROID_UI_MAX_RETRY_DELAY_SECONDS,
    ANDROID_UI_RETRY_DELAY_SECONDS,
    COLLECTION_FAILURE_BACKOFF_SECONDS,
    COLLECTION_MAX_CONSECUTIVE_FAILURES,
)
from .reel_history import LongReelStore
from .reel_store import write_json_atomic


@dataclass(frozen=True)
class AndroidMetricHandoff:
    """A browser snapshot waiting for the single Android UI worker."""

    record: dict[str, Any]
    missing_python_fields: tuple[str, ...] = ()
    delay_seconds: float = 0.0
    queued_at: float = field(default_factory=time.monotonic)
    checkpoint: Path | None = None


class AndroidMetricPipeline:
    """Enrich saved browser snapshots without blocking subsequent browser captures.

    Android UIAutomator can operate only one app surface at a time, so this
    pipeline deliberately has one worker.  Python can nevertheless continue
    discovering and saving the next Reel while that worker opens the prior
    Reel URL and writes its metrics back to the matching snapshot.
    """

    def __init__(
        self,
        *,
        enricher: AndroidReelMetricsEnricher,
        store: LongReelStore,
        on_result: Callable[[dict[str, Any], AndroidMetricResult], Awaitable[None]],
        metrics_required: bool = False,
        data_dir: Path | str | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.enricher = enricher
        self.store = store
        self.on_result = on_result
        self.metrics_required = metrics_required
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else None
        self.queue: asyncio.Queue[AndroidMetricHandoff | None] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None
        self.processing = 0
        self.queued = 0
        self.deferred = 0
        self._closed = False
        self.consecutive_failures = 0
        self.stop_event = stop_event or asyncio.Event()
        self.pending_dir = self.data_dir / '.collector' / 'android_pipeline_pending' if self.data_dir else None
        if self.pending_dir and self.pending_dir.exists():
            for path in sorted(self.pending_dir.glob('*.json')):
                payload = json.loads(path.read_text(encoding='utf-8'))
                self.queue.put_nowait(AndroidMetricHandoff(
                    record=payload['record'], missing_python_fields=tuple(payload.get('missing_python_fields', ())),
                    delay_seconds=max(0.0, float(payload.get('delay_seconds', 0))), checkpoint=path,
                ))
                self.queued += 1

    @property
    def backlog(self) -> int:
        return self.queue.qsize() + self.processing

    def start(self) -> None:
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())

    def enqueue(
        self,
        record: dict[str, Any],
        *,
        missing_python_fields: Sequence[str] = (),
        delay_seconds: float = 0.0,
    ) -> None:
        if self._closed:
            raise RuntimeError("Android metric pipeline is already closed.")
        if self.worker is not None and self.worker.done():
            self.worker.result()
            raise RuntimeError("Android metric worker has stopped.")
        self.start()
        normalized_delay = max(0.0, float(delay_seconds))
        self.queued += 1
        self.deferred += int(normalized_delay > 0)
        checkpoint = None
        if self.pending_dir is not None:
            checkpoint = self.pending_dir / f'{time.time_ns()}-{uuid.uuid4().hex}.json'
            write_json_atomic(checkpoint, dict(record=dict(record), missing_python_fields=list(missing_python_fields), delay_seconds=normalized_delay))
        self.queue.put_nowait(
            AndroidMetricHandoff(
                record=dict(record),
                missing_python_fields=tuple(missing_python_fields),
                delay_seconds=normalized_delay,
                checkpoint=checkpoint,
            )
        )

    async def _run(self) -> None:
        while True:
            handoff = await self.queue.get()
            try:
                if handoff is None:
                    return
                self.processing += 1
                if self.stop_event.is_set():
                    continue
                diagnostics = getattr(self.enricher, "diagnostics", None)
                if diagnostics is not None:
                    diagnostics.begin_media(
                        current_url=str(handoff.record.get("url", "")),
                        collection_number=handoff.record.get("_collection_number"),
                    )
                delay = max(0.0, handoff.delay_seconds - max(0.0, time.monotonic() - handoff.queued_at))
                if delay:
                    missing = ", ".join(handoff.missing_python_fields)
                    print(
                        "Python handoff incomplete "
                        f"({missing}); Android URL collection will start in {delay:g}s."
                    )
                    if await self._wait_or_stop(delay):
                        continue
                attempts = 0
                infrastructure_attempts = 0
                mismatch_details: list[str] = []
                result = AndroidMetricResult(status="unavailable", error="Android retry limit exhausted.")
                while attempts < ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL:
                    if self.stop_event.is_set():
                        break
                    attempts += 1
                    if attempts > 1 and diagnostics is not None:
                        diagnostics.record_retry(
                            stage=diagnostics.current_stage or "OPEN_REEL",
                            target="android_reel_metrics",
                            attempt=attempts,
                            total=ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL,
                            reason=result.error or "Android metrics unavailable",
                            previous_wait=0.1,
                        )
                    try:
                        result = await asyncio.to_thread(
                            self.enricher.enrich,
                            str(handoff.record.get("url", "")),
                            expected_username=str(handoff.record.get("username") or ""),
                        )
                    except Exception as error:
                        result = AndroidMetricResult(status="unavailable", error=str(error)[:500])
                    if _is_android_ui_read_failure(result.error) or _is_android_device_connection_error(result.error):
                        attempts -= 1
                        infrastructure_attempts += 1
                        self.queued += 1
                        if _is_android_ui_read_failure(result.error):
                            delay = min(ANDROID_UI_MAX_RETRY_DELAY_SECONDS, ANDROID_UI_RETRY_DELAY_SECONDS * 2 ** min(infrastructure_attempts - 1, 6))
                            if infrastructure_attempts % 3 == 0:
                                recover = getattr(self.enricher, "recover_app", None)
                                if callable(recover):
                                    await asyncio.to_thread(recover, str(handoff.record.get("url", "")))
                        else:
                            delay = ANDROID_DEVICE_RECONNECT_SECONDS
                            reset = getattr(self.enricher, "reset_connection", None)
                            if callable(reset):
                                await asyncio.to_thread(reset)
                        if await self._wait_or_stop(delay):
                            break
                        continue
                    if result.status == "collected":
                        mismatches = compare_python_and_android_counts(
                            handoff.record,
                            {field: value for field, value in result.metrics.items() if field not in ANDROID_COLLECTION_FIELDS},
                        )
                        if not mismatches:
                            break
                        mismatch_details.append(format_android_mismatch(attempts, mismatches))
                        result = AndroidMetricResult(
                            status="unavailable",
                            error=(
                                "Python/Android count mismatch: "
                                + ", ".join(sorted(mismatches))
                            ),
                        )
                    if attempts < ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL:
                        await asyncio.sleep(0.1)
                if self.stop_event.is_set():
                    continue
                try:
                    if diagnostics is not None:
                        diagnostics.stage_start("SAVE_RESULT")
                    enriched = dict(handoff.record)
                    if mismatch_details:
                        enriched["android_mismatch"] = " | ".join(mismatch_details)
                    snapshot_updated = await self.store.enrich_latest_snapshot(enriched, android_result=result)
                    if snapshot_updated and handoff.checkpoint is not None:
                        await self.store.flush()
                except Exception as error:
                    if diagnostics is not None:
                        diagnostics.stage_failed("SAVE_RESULT", reason=type(error).__name__, error=str(error))
                    snapshot_updated = False
                    result = AndroidMetricResult(status="unavailable", error=str(error)[:500])
                else:
                    if diagnostics is not None:
                        diagnostics.stage_success("SAVE_RESULT")
                if not snapshot_updated:
                    result = AndroidMetricResult(
                        status="unavailable",
                        error="The browser Reel snapshot disappeared before Android metric enrichment.",
                    )
                try:
                    await self.on_result(
                        {**handoff.record, "_android_attempt_count": attempts + infrastructure_attempts},
                        result,
                    )
                except Exception as error:
                    # A reporting failure must not strand the remaining
                    # Android queue items after Python has moved on.
                    print(f"Android metric status reporting failed: {error}", file=sys.stderr)
                if result.status == "collected":
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures += 1
                if snapshot_updated and handoff.checkpoint is not None:
                    handoff.checkpoint.unlink(missing_ok=True)
                if diagnostics is not None:
                    diagnostics.finish_media(
                        "SUCCESS" if result.status == "collected" else "FAILED",
                        success=result.status == "collected",
                        error=result.error,
                    )
                if self.consecutive_failures >= COLLECTION_MAX_CONSECUTIVE_FAILURES:
                    if self.data_dir is not None:
                        append_collection_log(
                            self.data_dir, "android", "pipeline_failure_backoff",
                            consecutive_failures=self.consecutive_failures,
                            retry_seconds=COLLECTION_FAILURE_BACKOFF_SECONDS,
                            url=str(handoff.record.get("url", "")),
                        )
                    await asyncio.sleep(COLLECTION_FAILURE_BACKOFF_SECONDS)
                    self.consecutive_failures = 0
            finally:
                if handoff is not None:
                    self.processing -= 1
                self.queue.task_done()

    async def _wait_or_stop(self, delay: float) -> bool:
        deadline = time.monotonic() + max(0.0, delay)
        while True:
            if self.data_dir is not None and read_collection_stop_request(self.data_dir) is not None:
                self.stop_event.set()
            if self.stop_event.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=min(0.25, remaining))
            except TimeoutError:
                pass

    async def close(self) -> None:
        if self._closed:
            return
        self.start()
        assert self.worker is not None
        joined = asyncio.create_task(self.queue.join())
        try:
            await asyncio.wait({joined, self.worker}, return_when=asyncio.FIRST_COMPLETED)
            if not self.worker.done():
                await joined
                self.queue.put_nowait(None)
            await self.worker
            if not joined.done():
                raise RuntimeError("Android metric worker stopped with queued jobs remaining.")
        except asyncio.CancelledError:
            # Threads cannot be cancelled. Keep the enricher/store alive until
            # the active operation finishes; queued checkpoints remain replayable.
            self.stop_event.set()
            await asyncio.wait({joined, self.worker}, return_when=asyncio.FIRST_COMPLETED)
            if not self.worker.done():
                self.queue.put_nowait(None)
                await self.worker
            raise
        finally:
            if not joined.done():
                joined.cancel()
            await asyncio.gather(joined, return_exceptions=True)
            self._closed = self.worker.done()

