from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from src.reels import fashion_analyzer, instagram_follower_enricher as followers
from src.reels import instagram_reels_browser as browser
from src.reels import instagram_hashtag_search as hashtag_search
from scripts import instagram_reels_python as launcher


class FollowerPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_write_keeps_changes_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            enricher = followers.FollowerEnricher(data_dir=directory, lookup_impl=AsyncMock())
            enricher.dirty_changes = 3
            with patch.object(followers, "atomic_write_csv", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    await enricher._flush_users()
            self.assertEqual(enricher.dirty_changes, 3)
            await enricher._flush_users()
            self.assertEqual(enricher.dirty_changes, 0)
            self.assertTrue(enricher.users_path.is_file())

    async def test_changes_during_write_are_saved_in_a_second_flush(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            enricher = followers.FollowerEnricher(data_dir=directory, lookup_impl=AsyncMock())
            enricher.dirty_changes = 1
            first_started, release = threading.Event(), threading.Event()
            calls = []

            def write(*args):
                calls.append(args)
                if len(calls) == 1:
                    first_started.set()
                    if not release.wait(2):
                        raise RuntimeError("write was not released")

            with patch.object(followers, "atomic_write_csv", side_effect=write):
                flushing = asyncio.create_task(enricher._flush_users())
                try:
                    self.assertTrue(await asyncio.to_thread(first_started.wait, 2))
                    enricher.dirty_changes += 1
                finally:
                    release.set()
                    await flushing
            self.assertEqual(len(calls), 2)
            self.assertEqual(enricher.dirty_changes, 0)

    async def test_cancelled_flush_retains_lock_until_thread_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            enricher = followers.FollowerEnricher(data_dir=directory, lookup_impl=AsyncMock())
            enricher.dirty_changes = 1
            started, release = threading.Event(), threading.Event()

            def write(*_args):
                started.set()
                if not release.wait(2):
                    raise RuntimeError("write was not released")

            with patch.object(followers, "atomic_write_csv", side_effect=write):
                flushing = asyncio.create_task(enricher._flush_users())
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    flushing.cancel()
                    await asyncio.sleep(0)
                    self.assertTrue(enricher.write_lock.locked())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await flushing
            self.assertFalse(enricher.write_lock.locked())
            self.assertEqual(enricher.dirty_changes, 1)

    async def test_cancelled_lookup_can_be_queued_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = asyncio.Event()

            async def lookup(_payload):
                started.set()
                await asyncio.Event().wait()

            enricher = followers.FollowerEnricher(data_dir=directory, lookup_impl=lookup)
            with patch.object(enricher, "_mark_dirty"):
                row = await enricher.track_user(username="example")
                await started.wait()
                task = next(iter(enricher.active_tasks))
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertNotIn(row["_key"], enricher.queued)
                self.assertFalse(enricher.active_tasks)
                self.assertTrue(enricher._enqueue(enricher.users[row["_key"]]))
                for task in tuple(enricher.active_tasks):
                    task.cancel()
                await asyncio.gather(*tuple(enricher.active_tasks), return_exceptions=True)

    def test_follower_csv_retries_a_transient_windows_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("src.reels.reel_store.os.replace", side_effect=[PermissionError("scanner"), None]) as replace, \
                 patch("src.reels.reel_store.time.sleep") as sleep:
                followers.atomic_write_csv(Path(directory) / "users.csv", [], ["username"])
            self.assertEqual(replace.call_count, 2)
            sleep.assert_called_once_with(0.1)

    async def test_close_saves_collected_users_and_cancels_pending_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = asyncio.Event()

            async def lookup(_payload):
                started.set()
                await asyncio.Event().wait()

            enricher = followers.FollowerEnricher(data_dir=directory, lookup_impl=lookup)
            await enricher.record_profile_result("123", "saved", {
                "status": "success", "followerCount": 100,
                "sourceField": "profile_header_text",
            })
            await enricher.track_user(username="pending")
            await started.wait()
            await asyncio.wait_for(enricher.close(), 1)
            self.assertFalse(enricher.active_tasks)
            self.assertFalse(enricher.queued)
            self.assertEqual(enricher.dirty_changes, 0)
            _fields, rows = followers.read_csv_objects(enricher.users_path)
            self.assertEqual(next(row for row in rows if row["username"] == "saved")["follower_count"], "100")

    async def test_drain_observes_an_already_failed_flush_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            enricher = followers.FollowerEnricher(data_dir=directory, lookup_impl=AsyncMock())
            await enricher.ready()

            async def failed_flush():
                raise PermissionError("locked")

            task = asyncio.create_task(failed_flush())
            await asyncio.wait({task})
            enricher.flush_task = task
            with self.assertRaises(PermissionError):
                await enricher.drain()
            self.assertIsNone(enricher.flush_task)


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_follower_spacing_counts_time_spent_between_calls(self) -> None:
        for gap, expected_wait in [(10.0, 0.0), (0.1, 0.2)]:
            with self.subTest(gap=gap):
                page = Mock()
                page.is_closed.return_value = False
                lookup = browser.SequentialWebFollowerLookup(page)
                current = [0.0]
                result = {"status": "success", "followerCount": 100}
                with patch.object(browser.time, "monotonic", side_effect=lambda: current[0]), \
                     patch.object(browser, "request_web_follower_count", new=AsyncMock(return_value=result)), \
                     patch.object(browser.asyncio, "sleep", new=AsyncMock()) as sleep:
                    await lookup({"username": "one"})
                    current[0] = gap
                    await lookup({"username": "two"})
                if expected_wait:
                    self.assertEqual(sleep.await_count, 1)
                    self.assertAlmostEqual(sleep.await_args.args[0], expected_wait)
                else:
                    sleep.assert_not_awaited()

    async def test_background_browser_closes_on_initialization_failure_or_cancel(self) -> None:
        for error in (RuntimeError("context failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                launched = Mock(new_context=AsyncMock(side_effect=error), close=AsyncMock())
                chromium = Mock(launch=AsyncMock(return_value=launched))
                source = Mock(storage_state=AsyncMock(return_value={}))
                with self.assertRaises(type(error)):
                    await browser.create_background_follower_runtime(chromium, source, "chrome")
                launched.close.assert_awaited_once()

    async def test_pipeline_close_reports_dead_worker_without_hanging(self) -> None:
        pipeline = browser.AndroidMetricPipeline(enricher=Mock(), store=Mock(), on_result=AsyncMock())
        started, fail = asyncio.Event(), asyncio.Event()

        async def worker():
            started.set()
            await fail.wait()
            raise RuntimeError("worker failed")

        pipeline.worker = asyncio.create_task(worker())
        pipeline.queue.put_nowait(browser.AndroidMetricHandoff(record={}))
        await started.wait()
        closing = asyncio.create_task(pipeline.close())
        await asyncio.sleep(0)
        fail.set()
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            await asyncio.wait_for(closing, 1)
        self.assertTrue(pipeline._closed)

    async def test_pipeline_rejects_new_jobs_after_worker_stops(self) -> None:
        pipeline = browser.AndroidMetricPipeline(enricher=Mock(), store=Mock(), on_result=AsyncMock())
        pipeline.worker = asyncio.create_task(asyncio.sleep(0))
        await pipeline.worker
        with self.assertRaisesRegex(RuntimeError, "worker has stopped"):
            pipeline.enqueue({})
        self.assertEqual(pipeline.queue.qsize(), 0)


class AnalyzerPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def run_analysis(self, root: Path, payload):
        async def fake_analyzer(*, url, output_file, **_kwargs):
            output_file.write_text(json.dumps(payload), encoding="utf-8")
            return url, output_file, 0, ""

        script = root / "analyzers" / "reel_analyzer.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.touch()
        with patch.object(fashion_analyzer, "_read_rows", return_value=[{"url": "https://www.instagram.com/reels/one/", "reaction_rate": "90"}]), \
             patch.object(fashion_analyzer, "_run_analyzer", new=fake_analyzer), \
             patch.object(fashion_analyzer, "_run_reel_insights", new=AsyncMock()):
            return await fashion_analyzer.analyze_qualifying_fashion_reels(root, repo_root=root)

    async def test_output_write_failure_does_not_mark_analysis_succeeded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(fashion_analyzer, "_write_json_atomic", side_effect=PermissionError("output locked")):
                with self.assertRaises(PermissionError):
                    await self.run_analysis(root, [{"analysis": {"summary": "ok"}}])
            self.assertFalse(fashion_analyzer._state_path(root).exists())
            summary = await self.run_analysis(root, [{"analysis": {"summary": "ok"}}])
            self.assertEqual(summary["succeeded"], 1)
            self.assertTrue(fashion_analyzer._analysis_output_path(root).exists())

    async def test_non_record_analyzer_output_is_not_a_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for payload in (["invalid"], [{}]):
                with self.subTest(payload=payload):
                    summary = await self.run_analysis(root, payload)
                    self.assertEqual(summary["failed"], 1)
                    self.assertEqual(summary["succeeded"], 0)
                    self.assertFalse(fashion_analyzer._analysis_output_path(root).exists())


class HashtagLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        trigger = Mock(wait_for=AsyncMock(), click=AsyncMock())
        search_input = Mock(wait_for=AsyncMock(), fill=AsyncMock())
        page = Mock(goto=AsyncMock(), wait_for_timeout=AsyncMock())
        page.locator.side_effect = [Mock(first=trigger), Mock(first=search_input)]
        context = Mock(pages=[page], close=AsyncMock())
        runtime = Mock(chromium=Mock(launch_persistent_context=AsyncMock(return_value=context)))
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=runtime)
        manager.__aexit__ = AsyncMock(return_value=False)
        return manager, context, page, trigger, search_input

    async def test_ready_search_does_not_use_fixed_startup_sleeps(self) -> None:
        manager, context, page, trigger, search_input = self.runtime()
        queue = asyncio.Queue()
        queue.put_nowait(None)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(hashtag_search, "load_playwright", return_value=lambda: manager), \
             patch.object(hashtag_search, "locate_browser_executable", return_value="chrome"):
            result = await hashtag_search.collect_python_exact_hashtag_count_stream(queue, profile_dir=Path(directory))
        self.assertEqual(result, {})
        trigger.wait_for.assert_awaited_once_with(state="visible", timeout=10_000)
        search_input.wait_for.assert_awaited_once_with(state="visible", timeout=10_000)
        page.wait_for_timeout.assert_not_awaited()
        context.close.assert_awaited_once()

    async def test_search_closes_context_on_error_or_cancel(self) -> None:
        for error in (RuntimeError("search unavailable"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                manager, context, _page, trigger, _input = self.runtime()
                trigger.wait_for.side_effect = error
                with tempfile.TemporaryDirectory() as directory, \
                     patch.object(hashtag_search, "load_playwright", return_value=lambda: manager), \
                     patch.object(hashtag_search, "locate_browser_executable", return_value="chrome"):
                    if isinstance(error, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            await hashtag_search.collect_python_exact_hashtag_count_stream(asyncio.Queue(), profile_dir=Path(directory))
                    else:
                        await hashtag_search.collect_python_exact_hashtag_count_stream(asyncio.Queue(), profile_dir=Path(directory))
                context.close.assert_awaited_once()

    async def test_search_cancels_an_unfinished_response_reader(self) -> None:
        manager, context, page, _trigger, _input = self.runtime()
        cancelled = asyncio.Event()

        async def response_body():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def navigate(*_args, **_kwargs):
            response = Mock(url="https://www.instagram.com/api/graphql", status=200, json=AsyncMock(side_effect=response_body))
            page.on.call_args.args[1](response)
            await asyncio.sleep(0)

        page.goto.side_effect = navigate
        queue = asyncio.Queue()
        queue.put_nowait(None)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(hashtag_search, "load_playwright", return_value=lambda: manager), \
             patch.object(hashtag_search, "locate_browser_executable", return_value="chrome"):
            await asyncio.wait_for(hashtag_search.collect_python_exact_hashtag_count_stream(queue, profile_dir=Path(directory)), 1)
        self.assertTrue(cancelled.is_set())
        context.close.assert_awaited_once()

    async def test_android_error_cancels_the_browser_stream(self) -> None:
        started = threading.Event()
        cancelled = asyncio.Event()

        async def stream(*_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        def android_failure(*_args, **_kwargs):
            if not started.wait(2):
                raise RuntimeError("browser stream did not start")
            raise RuntimeError("ADB failed")

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(hashtag_search, "collect_python_exact_hashtag_count_stream", new=stream), \
             patch.object(hashtag_search, "collect_android_related_hashtag_rows_and_counts", side_effect=android_failure):
            with self.assertRaisesRegex(RuntimeError, "ADB failed"):
                await hashtag_search.collect_hashtag_count_report(Path(directory), ["fashion"], profile_dir=Path(directory))
        self.assertTrue(cancelled.is_set())


class LauncherOptionTests(unittest.TestCase):
    def test_equals_data_directory_is_forwarded_without_a_default_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(launcher, "collector_main", return_value=0) as collect:
            self.assertEqual(launcher.main(["collect", f"--data-dir={directory}", "--collector-mode", "web"]), 0)
        arguments = collect.call_args.args[0]
        self.assertIn(f"--data-dir={directory}", arguments)
        self.assertNotIn("--data-dir", arguments)

    def test_option_value_matches_argparse_last_option_wins(self) -> None:
        self.assertEqual(launcher.option_value(["--data-dir=first", "--data-dir", "second"], "--data-dir", "default"), "second")
        self.assertEqual(launcher.option_value(["--data-dir", "first", "--data-dir=second"], "--data-dir", "default"), "second")
