from __future__ import annotations
from src.reels import reel_history as _remediation_reel_history

import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.reels import fashion_beauty_collection as collection
from src.reels.fashion_beauty_scheduler import DatasetConfig, FASHION_KEYWORDS, RunConfig
from src.reels import fashion_analyzer as analyzer
from src.reels import instagram_reels_browser as browser


def record(timestamp: str) -> dict:
    return {"url": "https://www.instagram.com/reels/example/", "collected_at": timestamp, "view_count": 100}


class JournalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_preserves_snapshots_saved_with_cooldown_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".collector" / browser.REEL_HISTORY_FILENAME
            store = await browser.LongReelStore.create(path, disable_recollect_cooldown=True)
            await store.append(record("2026-10-01T00:00:00Z"))
            await store.append(record("2026-10-01T00:01:00Z"))
            recovered = await browser.LongReelStore.create(path)
            self.assertEqual(len(recovered.rows), 2)
            self.assertFalse(recovered.journal_path.exists())

    async def test_recovery_does_not_duplicate_an_older_checkpointed_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".collector" / browser.REEL_HISTORY_FILENAME
            store = await browser.LongReelStore.create(path, disable_recollect_cooldown=True)
            first = record("2026-09-01T00:00:00Z")
            await store.append(first)
            await store.append(record("2026-10-01T00:00:00Z"))
            await store.flush()
            store.journal_path.write_text(json.dumps(first) + "\n", encoding="utf-8")
            recovered = await browser.LongReelStore.create(path)
            self.assertEqual(len(recovered.rows), 2)

    async def test_corrupt_non_object_line_is_archived_without_losing_valid_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".collector" / browser.REEL_HISTORY_FILENAME
            store = await browser.LongReelStore.create(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            store.journal_path.write_text('[]\n' + json.dumps(record("2026-10-01T00:00:00Z")) + '\n', encoding="utf-8")
            recovered = await browser.LongReelStore.create(path)
            self.assertEqual(len(recovered.rows), 1)
            archives = list(path.parent.glob("*.corrupt_*"))
            self.assertEqual(len(archives), 1)
            self.assertIn("[]", archives[0].read_text(encoding="utf-8"))

    async def test_cancelled_checkpoint_keeps_lock_until_writer_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".collector" / browser.REEL_HISTORY_FILENAME
            store = await browser.LongReelStore.create(path)
            await store.append(record("2026-10-01T00:00:00Z"))
            started, release = threading.Event(), threading.Event()

            def writing(_path, rows, *_args):
                started.set()
                if not release.wait(2):
                    raise RuntimeError("writer not released")
                return rows

            with patch.object(_remediation_reel_history, "_write_history_preserving_external_android_metrics", side_effect=writing):
                flushing = asyncio.create_task(store.flush())
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    flushing.cancel()
                    await asyncio.wait({flushing}, timeout=0.02)
                    self.assertTrue(store.lock.locked())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await flushing
            self.assertTrue(store.dirty)
            self.assertTrue(store.journal_path.exists())


class AnalyzerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_analysis_is_saved_before_the_batch_is_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "analyzers" / "reel_analyzer.py"
            script.parent.mkdir(parents=True)
            script.touch()
            persisted = asyncio.Event()
            original_write = analyzer._write_json_atomic

            def write(path, payload):
                original_write(path, payload)
                if path == analyzer._state_path(root):
                    persisted.set()

            async def run(*, url, output_file, **_kwargs):
                if url.endswith("/one/"):
                    output_file.write_text('[{"analysis": "ok"}]', encoding="utf-8")
                    return url, output_file, 0, ""
                await asyncio.Event().wait()

            rows = [{"url": f"https://www.instagram.com/reels/{name}/", "reaction_rate": "90"} for name in ("one", "two")]
            with patch.object(analyzer, "_read_rows", return_value=rows), \
                 patch.object(analyzer, "_run_analyzer", new=run), \
                 patch.object(analyzer, "_write_json_atomic", side_effect=write), \
                 patch.object(analyzer, "_run_reel_insights", new=AsyncMock()):
                task = asyncio.create_task(analyzer.analyze_qualifying_fashion_reels(root, repo_root=root))
                try:
                    await asyncio.wait_for(persisted.wait(), 0.3)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            state = json.loads(analyzer._state_path(root).read_text(encoding="utf-8"))
            self.assertEqual(state["records"][rows[0]["url"]]["status"], "succeeded")
            results = json.loads(analyzer._analysis_output_path(root).read_text(encoding="utf-8"))
            self.assertEqual(results[0]["reel_url"], rows[0]["url"])

    async def test_success_manifest_without_output_is_requeued(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "analyzers" / "reel_analyzer.py"
            script.parent.mkdir(parents=True)
            script.touch()
            url = "https://www.instagram.com/reels/one/"
            state_path = analyzer._state_path(root)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"records": {url: {"status": "succeeded"}}}), encoding="utf-8")

            async def run(*, url, output_file, **_kwargs):
                output_file.write_text('[{"analysis": "recovered"}]', encoding="utf-8")
                return url, output_file, 0, ""

            with patch.object(analyzer, "_read_rows", return_value=[{"url": url, "reaction_rate": "90"}]), \
                 patch.object(analyzer, "_run_analyzer", new=run), \
                 patch.object(analyzer, "_run_reel_insights", new=AsyncMock()):
                summary = await analyzer.analyze_qualifying_fashion_reels(root, repo_root=root)
            self.assertEqual(summary["succeeded"], 1)
            self.assertTrue(analyzer._analysis_output_path(root).is_file())

    async def test_malformed_manifest_entry_does_not_stop_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "analyzers" / "reel_analyzer.py"
            script.parent.mkdir(parents=True)
            script.touch()
            url = "https://www.instagram.com/reels/one/"
            state_path = analyzer._state_path(root)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"records": {url: "invalid"}}), encoding="utf-8")

            async def run(*, url, output_file, **_kwargs):
                return url, output_file, 1, "test failure"

            with patch.object(analyzer, "_read_rows", return_value=[{"url": url, "reaction_rate": "90"}]), \
                 patch.object(analyzer, "_run_analyzer", new=run), \
                 patch.object(analyzer, "_run_reel_insights", new=AsyncMock()):
                summary = await analyzer.analyze_qualifying_fashion_reels(root, repo_root=root)
            self.assertEqual(summary["failed"], 1)


class HandoffDelayTests(unittest.TestCase):
    def test_queue_wait_counts_toward_handoff_delay(self):
        now = datetime(2026, 10, 1, 0, 0, 10, tzinfo=timezone.utc)
        self.assertEqual(browser.android_metric_job_delay_seconds(
            {"delay_seconds": 3, "queued_at": "2026-10-01T00:00:00Z"}, now=now), 0)
        self.assertEqual(browser.android_metric_job_delay_seconds(
            {"delay_seconds": 3, "queued_at": "2026-10-01T00:00:09Z"}, now=now), 2)

    def test_missing_or_future_queue_timestamp_retains_grace_period(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        for queued_at in (None, "invalid", "2026-10-02T00:00:00Z"):
            with self.subTest(queued_at=queued_at):
                self.assertEqual(browser.android_metric_job_delay_seconds(
                    {"delay_seconds": 3, "queued_at": queued_at}, now=now), 3)


class PublicationSignatureTests(unittest.TestCase):
    def test_web_and_detached_android_updates_invalidate_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = DatasetConfig("fashion", root, FASHION_KEYWORDS)
            initial = collection.dataset_publication_signature(dataset)
            self.assertEqual(initial, collection.dataset_publication_signature(dataset))
            users = root / "users.csv"
            users.write_text("username\none\n", encoding="utf-8")
            web_updated = collection.dataset_publication_signature(dataset)
            self.assertNotEqual(initial, web_updated)
            completed = root / ".collector" / "android_metric_queue" / "completed" / "one.json"
            completed.parent.mkdir(parents=True)
            completed.write_text('{"metrics": {"view_count": 100}}', encoding="utf-8")
            android_updated = collection.dataset_publication_signature(dataset)
            self.assertNotEqual(web_updated, android_updated)
            completed.write_text('{"metrics": {"view_count": 10000}}', encoding="utf-8")
            self.assertNotEqual(android_updated, collection.dataset_publication_signature(dataset))
            completed.unlink()
            self.assertEqual(web_updated, collection.dataset_publication_signature(dataset))


class PublicationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_loop_skips_exports_but_restores_deleted_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
            output = root / "beauty_reels.csv"
            calls = []
            invocations = []
            config = RunConfig(data_root=root, domains=("beauty",), collector_mode="web",
                               duration_hours=0.05, discovery_hours=0.05, new_only=True)

            async def invoke(**_kwargs):
                history = root / ".datasets" / "beauty" / ".collector" / browser.REEL_HISTORY_FILENAME
                history.parent.mkdir(parents=True, exist_ok=True)
                if not history.exists():
                    history.write_text("url,collected_at,collection_number\n"
                                   f"https://www.instagram.com/reels/example/,{current[0].isoformat()},1\n",
                                   encoding="utf-8")
                invocations.append(current[0])
                if len(invocations) == 2:
                    output.unlink()
                current[0] += timedelta(minutes=1)
                return 0

            def publish(dataset):
                calls.append(dataset.name)
                output.write_text("url\n", encoding="utf-8")
                return {"reels_csv": output}

            with patch.object(collection, "publish_dataset_outputs", side_effect=publish):
                await collection.run_fashion_beauty_collection(config, invoke=invoke, clock=lambda: current[0])
            self.assertEqual(len(invocations), 3)
            self.assertEqual(calls, ["beauty", "beauty"])
            self.assertTrue(output.exists())
