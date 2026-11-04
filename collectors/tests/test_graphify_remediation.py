from src.reels import android_metric_pipeline as _remediation_android_metric_pipeline
import asyncio
import unittest
import tempfile
import json
from pathlib import Path
from src.reels.instagram_follower_enricher import read_csv_objects
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.reels import instagram_reels_browser as browser
from src.reels.android_reel_metrics import AndroidMetricResult, merge_android_metrics
from src.reels import android_metric_worker, process_lock


class MetricMergeTests(unittest.TestCase):
    def test_lock_ownership_is_shared_across_modules(self):
        self.assertIs(browser.AtomicProcessLock, process_lock.AtomicProcessLock)
        self.assertIs(android_metric_worker.AtomicProcessLock, process_lock.AtomicProcessLock)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'history.lock.json'
            owner = browser.AtomicProcessLock(path)
            competing = android_metric_worker.AtomicProcessLock(path)
            self.assertTrue(owner.acquire())
            try:
                self.assertFalse(competing.acquire())
                self.assertTrue(path.exists())
            finally:
                owner.release()
            self.assertTrue(competing.acquire())
            competing.release()

    def test_disk_completion_and_memory_use_same_metric_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            row = dict(url='https://www.instagram.com/reel/probe/', collected_at='2026-10-31T00:00:00Z',
                       username='author', like_count=14, comment_count=5, share_count=1, repost_count=20,
                       saved_count='', audio_name='Web audio', ad='true')
            result = AndroidMetricResult(metrics={'like_count': 0, 'share_count': 40, 'repost_count': 100, 'saved_count': 0},
                compact_fields=('repost_count',), like_count_private=True, audio_name='Stale app audio',
                observed_reel_url=row['url'], observed_username='author')
            expected = merge_android_metrics(row, result)
            history = Path(directory)/'.collector'/'reels_history_active.csv'
            browser.write_csv_records(history, [row])
            browser.enqueue_android_metric_job(directory, row)
            paths = browser._android_metric_queue_paths(directory)
            working, job = browser._claim_android_metric_job(paths)
            browser._write_android_metric_completion(paths, working, job, result)
            self.assertEqual(browser.apply_completed_android_metric_jobs(directory)['applied'], 1)
            _, rows = read_csv_objects(history)
            for field in ('like_count', 'comment_count', 'share_count', 'repost_count', 'saved_count', 'audio_name'):
                self.assertEqual(rows[0][field], str(expected[field]), field)

    def test_android_never_fills_web_audio(self):
        row = {"audio_name": "", "saved_count": "", "ad": "true"}
        merged = merge_android_metrics(row, AndroidMetricResult(audio_name="stale app audio"))
        self.assertEqual(merged["audio_name"], "")

    def test_missing_ad_saved_count_stays_missing(self):
        merged = merge_android_metrics({"saved_count": "", "ad": "true"}, AndroidMetricResult())
        self.assertEqual(merged["saved_count"], "")


class PipelineRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_close_waits_for_active_save(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def save(*args, **kwargs):
            started.set()
            await release.wait()
            return True
        pipeline = browser.AndroidMetricPipeline(
            enricher=SimpleNamespace(enrich=lambda *a, **k: AndroidMetricResult(metrics={'share_count': 1})),
            store=SimpleNamespace(enrich_latest_snapshot=save), on_result=AsyncMock(),
        )
        pipeline.enqueue({'url': 'https://www.instagram.com/reel/probe/'})
        closer = asyncio.create_task(pipeline.close())
        try:
            await started.wait()
            closer.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(closer.done(), 'cleanup must wait for active save')
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await closer
            self.assertTrue(pipeline.worker.done())
        finally:
            release.set()
            if not pipeline.worker.done():
                pipeline.worker.cancel()
            await asyncio.gather(pipeline.worker, return_exceptions=True)

    async def test_shared_stop_interrupts_device_reconnect_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = browser.AndroidMetricPipeline(
                enricher=SimpleNamespace(enrich=lambda *a, **k: AndroidMetricResult(status='unavailable', error='device offline')),
                store=SimpleNamespace(enrich_latest_snapshot=AsyncMock()), on_result=AsyncMock(), data_dir=directory,
            )
            pipeline.enqueue({'url': 'https://www.instagram.com/reel/probe/', 'collected_at': '2026-10-31T00:00:00Z'})
            await asyncio.sleep(0.05)
            stop_path = Path(directory)/'.collector'/'collection_stop.json'
            stop_path.write_text(json.dumps({'stop_requested': True}), encoding='utf-8')
            await asyncio.wait_for(pipeline.close(), 2)
            self.assertTrue(list((stop_path.parent/'android_pipeline_pending').glob('*.json')))

    async def test_checkpoint_is_removed_only_after_metrics_reach_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'.collector'/'reels_history_active.csv'
            store = await browser.LongReelStore.create(path, 100, 'rows', False)
            row = {'url': 'https://www.instagram.com/reel/probe/', 'collected_at': '2026-10-31T00:00:00Z', 'share_count': ''}
            await store.append(row)
            await store.flush()
            pipeline = browser.AndroidMetricPipeline(
                enricher=SimpleNamespace(enrich=lambda *a, **k: AndroidMetricResult(metrics={'share_count': 7})),
                store=store, on_result=AsyncMock(), data_dir=directory,
            )
            pipeline.enqueue(row)
            await asyncio.wait_for(pipeline.close(), 2)
            _, rows = read_csv_objects(path)
            self.assertEqual(rows[0]['share_count'], '7')

    async def test_stopped_work_is_recovered_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = asyncio.Event()
            stop.set()
            reports = []
            async def report(record, result):
                reports.append(result.status)
            options = dict(
                enricher=SimpleNamespace(enrich=lambda *a, **k: AndroidMetricResult(metrics={"share_count": 7})),
                store=SimpleNamespace(enrich_latest_snapshot=AsyncMock(return_value=True), flush=AsyncMock()),
                on_result=report, data_dir=directory,
            )
            pipeline = browser.AndroidMetricPipeline(**options, stop_event=stop)
            pipeline.enqueue({"url": "https://www.instagram.com/reel/probe/", "collected_at": "2026-10-31T00:00:00Z"})
            await asyncio.wait_for(pipeline.close(), 2)
            self.assertEqual(reports, [])
            self.assertTrue(list(Path(directory).rglob("*.json")))
            restarted = browser.AndroidMetricPipeline(**options)
            await asyncio.wait_for(restarted.close(), 2)
            self.assertEqual(reports, ["collected"])
            self.assertEqual(list((Path(directory)/'.collector'/'android_pipeline_pending').glob('*.json')), [])

    async def test_device_failures_do_not_consume_reel_attempts(self):
        calls = 0
        reports = []
        stored = []

        def enrich(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 3:
                return AndroidMetricResult(status="unavailable", error="adb.exe: device offline")
            return AndroidMetricResult(metrics={"share_count": 42})

        async def save(record, *, android_result):
            stored.append(android_result.metrics)
            return True

        async def report(record, result):
            reports.append(result.status)

        pipeline = browser.AndroidMetricPipeline(
            enricher=SimpleNamespace(enrich=enrich, reset_connection=lambda: None),
            store=SimpleNamespace(enrich_latest_snapshot=save), on_result=report,
        )
        with patch.object(_remediation_android_metric_pipeline, "ANDROID_DEVICE_RECONNECT_SECONDS", 0):
            pipeline.enqueue({"url": "https://www.instagram.com/reel/probe/"})
            await asyncio.wait_for(pipeline.close(), timeout=5)
        self.assertEqual(reports, ["collected"])
        self.assertEqual(stored, [{"share_count": 42}])

    async def test_normal_failure_still_has_three_attempt_limit(self):
        reports = []
        async def report(record, result):
            reports.append((record["_android_attempt_count"], result.status))
        pipeline = browser.AndroidMetricPipeline(
            enricher=SimpleNamespace(enrich=lambda *a, **k: AndroidMetricResult(status="unavailable", error="missing count")),
            store=SimpleNamespace(enrich_latest_snapshot=AsyncMock(return_value=True)), on_result=report,
        )
        pipeline.enqueue({"url": "https://www.instagram.com/reel/probe/"})
        await asyncio.wait_for(pipeline.close(), timeout=5)
        self.assertEqual(reports, [(3, "unavailable")])
