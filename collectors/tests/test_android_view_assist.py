from src.reels import android_metric_worker as _remediation_android_metric_worker
import tempfile
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
from datetime import timedelta
from src.reels import android_reel_metrics as a
from src.reels import instagram_reels_browser as b


def xml(nodes):
    return "<hierarchy>" + nodes + "</hierarchy>"


def node(rid, text="", desc="", bounds="[0,0][10,10]"):
    return f'<node resource-id="com.instagram.android:id/{rid}" text="{text}" content-desc="{desc}" bounds="{bounds}" />'


def queue_view_for_test(data_dir, row, **kwargs):
    job = b.enqueue_android_metric_job(data_dir, row)
    b.enqueue_android_view_count_job(data_dir, row)
    return job


class Driver:
    def __init__(self, count="7,484", code="target"):
        self.surface = "detail"
        self.count, self.code = count, code
        self.card_taps = 0
    def ensure_ready(self): pass
    def open_instagram_url(self, url): self.surface = "detail"
    def dump_ui(self):
        if self.surface == "detail":
            return xml(node("clips_author_username", "author") + node("clips_pause_button", desc="Play") + node("like_count", "607"))
        return xml(node("title_text_view", "Likes and plays") + node("video_view_count_text", self.count))
    def tap_bounds(self, bounds):
        self.card_taps += 1
        self.surface = "panel"
        return True
    def tap_screen_center(self): pass
    def copy_current_reel_url(self): return f"https://www.instagram.com/reel/{self.code}/"
    def press_back(self): self.surface = "detail"
    def scroll_profile_grid(self, xml): raise AssertionError("Fallback must not scan the profile grid")


class AndroidViewAssistTests(unittest.TestCase):
    def test_collaboration_author_uses_same_identity_check_as_regular_metrics(self):
        for code in ("target", "other"):
            with self.subTest(code=code):
                driver = Driver(code=code)
                dump = driver.dump_ui
                driver.dump_ui = lambda: dump().replace('text="author"', 'text="author and co.author"')
                with patch.object(a.time, "sleep"):
                    result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                        "https://www.instagram.com/reel/target/", "author")
                self.assertEqual(result.metrics, {"view_count": 7484} if code == "target" else {})
                self.assertEqual(driver.card_taps, 1 if code == "target" else 0)

    def test_view_assist_verifies_permalink_before_opening_likes(self):
        for likes_text in (False, True):
            with self.subTest(likes_text=likes_text):
                driver = Driver()
                dump = driver.dump_ui
                def detail_with_share():
                    surface = dump()
                    if driver.surface == "detail":
                        surface = surface.replace('</hierarchy>', node("direct_share_button", bounds="[900,1500][1000,1600]") + '</hierarchy>')
                        if likes_text:
                            surface = surface.replace('resource-id="com.instagram.android:id/like_count" text="607"', 'resource-id="com.instagram.android:id/counter" text="Likes"')
                    return surface
                driver.dump_ui = detail_with_share
                with (
                    patch.object(a.time, "sleep"),
                    patch.object(driver, "copy_current_reel_url", wraps=driver.copy_current_reel_url) as copy,
                    patch.object(driver, "tap_bounds", wraps=driver.tap_bounds) as tap,
                ):
                    result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                        "https://www.instagram.com/reel/target/", "author")
                self.assertEqual(result.metrics, {"view_count": 7484})
                copy.assert_called_once_with()
                self.assertEqual(result.observed_reel_url, "https://www.instagram.com/reels/target/")
                self.assertEqual(result.observed_username, "author")
                tap.assert_called_once_with("[0,0][10,10]")
                self.assertEqual(driver.surface, "detail")

    def test_wrong_permalink_never_opens_likes_or_returns_views(self):
        driver = Driver(code="other")
        with patch.object(a.time, "sleep"):
            result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                "https://www.instagram.com/reel/target/", "author")
        self.assertEqual(result.error, "Reel permalink identity could not be verified")
        self.assertEqual(result.metrics, {})
        self.assertEqual(driver.card_taps, 0)

    def test_waits_for_share_menu_to_close_before_opening_likes(self):
        class ClosingMenuDriver(Driver):
            def copy_current_reel_url(self):
                self.surface = "menu_closing"
                return super().copy_current_reel_url()

            def dump_ui(self):
                if self.surface == "menu_closing":
                    self.surface = "detail"
                return super().dump_ui()

            def tap_bounds(self, bounds):
                if self.surface == "menu_closing":
                    return True  # The closing sheet consumes this tap.
                return super().tap_bounds(bounds)

        driver = ClosingMenuDriver()
        with patch.object(a.time, "sleep"):
            result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                "https://www.instagram.com/reel/target/", "author")
        self.assertEqual(result.metrics, {"view_count": 7484})
        self.assertEqual(driver.card_taps, 1)

    def test_changed_author_after_link_menu_never_opens_likes(self):
        driver = Driver()
        dump = driver.dump_ui

        def copy_url():
            driver.dump_ui = lambda: dump().replace('text="author"', 'text="other"')
            return "https://www.instagram.com/reel/target/"

        driver.copy_current_reel_url = copy_url
        with patch.object(a.time, "sleep"):
            result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                "https://www.instagram.com/reel/target/", "author")
        self.assertEqual(result.metrics, {})
        self.assertEqual(driver.card_taps, 0)

    def test_delayed_likes_panel_gets_one_more_bounded_read(self):
        driver = Driver()
        dump = driver.dump_ui
        reads = []
        def delayed_dump():
            if driver.card_taps and len(reads) < 1:
                reads.append(1)
                return "<hierarchy />"
            return dump()
        driver.dump_ui = delayed_dump
        with patch.object(a.time, "sleep"):
            result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                "https://www.instagram.com/reel/target/", "author")
        self.assertEqual(result.metrics, {"view_count": 7484})

    def test_delayed_retry_does_not_block_regular_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = b._android_metric_queue_paths(temp)
            b.write_json_atomic(paths["pending"] / "view.json", {
                "kind": "profile_view_count", "next_attempt_at": b.isoformat_utc(b.utc_now() + timedelta(seconds=10)),
            })
            self.assertIsNone(b._claim_android_metric_job(paths))
            b.write_json_atomic(paths["pending"] / "metrics.json", {"job_id": "metrics"})
            working, job = b._claim_android_metric_job(paths)
            self.assertEqual(job["job_id"], "metrics")
            working.unlink()
            self.assertTrue((paths["pending"] / "view.json").exists())

    def test_timeout_and_invalid_identity_are_distinct(self):
        driver = Driver()
        with patch.object(a.time, "monotonic", side_effect=[0, 61]):
            result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                "https://www.instagram.com/reel/target/", "author")
        self.assertIn("timed out", result.error)
        invalid = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count("not-a-reel", "author")
        self.assertEqual(invalid.error, "Invalid view target identity")

    def test_persistent_wrong_author_never_returns_a_view_count(self):
        driver = Driver()
        dump = driver.dump_ui
        driver.dump_ui = lambda: dump().replace('text="author"', 'text="other"')
        with patch.object(a.time, "sleep"):
            result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                "https://www.instagram.com/reel/target/", "author")
        self.assertEqual(result.error, "Reel author mismatch")
        self.assertEqual(result.metrics, {})
        self.assertEqual(driver.card_taps, 0)

    def test_worker_yield_does_not_consume_view_retry_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            job_id = queue_view_for_test(temp, {
                "url": "https://www.instagram.com/reel/target/", "collected_at": "new", "username": "author",
            }, assist_view_count=True)
            paths = b._android_metric_queue_paths(temp)
            (paths["pending"] / f"{job_id}.json").unlink()
            collect = Mock(side_effect=[
                a.AndroidMetricResult(status="unavailable", error="View assist yielded to other work"),
                a.AndroidMetricResult(metrics={"view_count": 7484}),
            ])
            with patch.object(_remediation_android_metric_worker, "AndroidReelMetricsEnricher", return_value=SimpleNamespace(collect_profile_view_count=collect)):
                b.run_android_metric_worker(temp, idle_seconds=0.01, attach_existing=False)
            completion = b._read_android_metric_job(next(paths["completed"].glob("*.json")))
            self.assertEqual(completion["attempts"], 1)



    def test_clipboard_restore_failure_preserves_verified_link(self):
        driver = a.AdbAndroidUiDriver.__new__(a.AdbAndroidUiDriver)
        share = xml(node("direct_share_button"))
        copy = xml(node("copy_link", "Copy link"))
        with patch.object(driver, "dump_ui", side_effect=[share, copy, share]), \
             patch.object(driver, "tap_bounds"), \
             patch.object(a, "_read_windows_clipboard_text", side_effect=["before", "https://www.instagram.com/reel/target/"]), \
             patch.object(a, "_set_windows_clipboard_text", side_effect=[None, OSError("busy"), OSError("busy"), OSError("busy")]), \
             patch.object(a.time, "sleep"), self.assertWarns(RuntimeWarning):
            self.assertEqual(driver.copy_current_reel_url(), "https://www.instagram.com/reel/target/")

    def test_worker_retries_view_failure_and_then_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            job_id = queue_view_for_test(temp, {
                "url": "https://www.instagram.com/reel/target/", "collected_at": "new", "username": "author",
            }, assist_view_count=True)
            paths = b._android_metric_queue_paths(temp)
            (paths["pending"] / f"{job_id}.json").unlink()
            collect = Mock(side_effect=[
                a.AndroidMetricResult(status="unavailable", error="Reel author mismatch"),
                a.AndroidMetricResult(metrics={"view_count": 7484}),
            ])
            with patch.object(_remediation_android_metric_worker, "AndroidReelMetricsEnricher", return_value=SimpleNamespace(collect_profile_view_count=collect)), \
                 patch.object(_remediation_android_metric_worker, "ANDROID_PROFILE_VIEW_RETRY_DELAY_SECONDS", 0, create=True):
                self.assertEqual(b.run_android_metric_worker(temp, idle_seconds=0.01, attach_existing=False), 0)
            self.assertEqual(collect.call_count, 2)
            completion = b._read_android_metric_job(next(paths["completed"].glob("*.json")))
            self.assertEqual(completion["metrics"], {"view_count": 7484})
            self.assertEqual(completion["attempts"], 2)

    def test_worker_stops_after_three_view_failures_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            job_id = queue_view_for_test(temp, {
                "url": "https://www.instagram.com/reel/target/", "collected_at": "new", "username": "author",
            }, assist_view_count=True)
            paths = b._android_metric_queue_paths(temp)
            (paths["pending"] / f"{job_id}.json").unlink()
            collect = Mock(return_value=a.AndroidMetricResult(status="unavailable", error="ADB command timed out after 5s: exec-out sh -c"))
            with patch.object(_remediation_android_metric_worker, "AndroidReelMetricsEnricher", return_value=SimpleNamespace(collect_profile_view_count=collect)), \
                 patch.object(_remediation_android_metric_worker, "ANDROID_PROFILE_VIEW_RETRY_DELAY_SECONDS", 0, create=True):
                self.assertEqual(b.run_android_metric_worker(temp, idle_seconds=0.01, attach_existing=False), 0)
            self.assertEqual(collect.call_count, 3)
            failures = list((paths["completed"].parent / "view_failed").glob("*.json"))
            self.assertEqual(len(failures), 1)
            self.assertEqual(b._read_android_metric_job(failures[0])["attempts"], 3)
            self.assertFalse(list(paths["pending"].glob("*.json")))

    def test_prune_removes_only_collected_same_snapshot_view_helpers(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            paths = b._android_metric_queue_paths(data_dir)
            for stamp in ("old", "new"):
                queue_view_for_test(data_dir, {
                    "url": "u", "collected_at": stamp, "username": "author",
                }, assist_view_count=True)
            history = data_dir / b.REEL_HISTORY_DIRECTORY / b.REEL_HISTORY_FILENAME
            history.touch()
            with patch.object(_remediation_android_metric_worker, "read_csv_objects", return_value=([], [
                {"url": "u", "collected_at": "new", "view_count": "0"},
                {"url": "u", "collected_at": "old", "view_count": "1.2K"},
            ])):
                self.assertEqual(b.prune_collected_android_view_jobs(data_dir), 1)
            jobs = [b._read_android_metric_job(p) for p in paths["pending"].glob("*.json")]
            self.assertEqual(len(jobs), 3)
            self.assertEqual([j["target"]["collected_at"] for j in jobs if j.get("kind") == "profile_view_count"], ["old"])

    def test_worker_discards_active_helper_when_web_finishes_during_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            job = queue_view_for_test(data_dir, {
                "url": "https://www.instagram.com/reel/target/", "collected_at": "new", "username": "author",
            }, assist_view_count=True)
            paths = b._android_metric_queue_paths(data_dir)
            (paths["pending"] / f"{job}.json").unlink()
            def collect(*args, should_yield, **kwargs):
                self.assertTrue(should_yield())
                return a.AndroidMetricResult(status="unavailable")
            enricher = SimpleNamespace(collect_profile_view_count=collect)
            with patch.object(_remediation_android_metric_worker, "AndroidReelMetricsEnricher", return_value=enricher), \
                 patch.object(_remediation_android_metric_worker, "_android_view_target_is_collected", side_effect=[False, True, True]):
                self.assertEqual(b.run_android_metric_worker(data_dir, idle_seconds=0.01, attach_existing=False), 0)
            self.assertFalse(list(paths["pending"].glob("*.json")))
            self.assertFalse(list(paths["working"].glob("*.json")))
            self.assertFalse(list(paths["completed"].glob("*.json")))

    def test_direct_target_collects_only_exact_view_counts(self):
        for count, expected in [("7,484", {"view_count": 7484}), ("142K", {}),
                                ("78.2K", {}), ("1.7M", {})]:
            with self.subTest(count=count), patch.object(a.time, "sleep"):
                driver = Driver(count)
                open_url = Mock(wraps=driver.open_instagram_url)
                driver.open_instagram_url = open_url
                result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                    "https://www.instagram.com/reel/target/", "author")
                self.assertEqual(result.metrics, expected)
                open_url.assert_called_once_with("https://www.instagram.com/reel/target/")
                if count in {"142K", "78.2K", "1.7M"}: self.assertEqual(driver.card_taps, 1)

    def test_new_metric_work_preempts_view_scan(self):
        driver = Driver()
        result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
            "https://www.instagram.com/reel/target/", "author", should_yield=lambda: True)
        self.assertEqual(result.metrics, {})

    def test_empty_detail_does_not_open_likes_or_scroll(self):
        driver = Driver()
        driver.dump_ui = lambda: "<hierarchy />"
        driver.tap_bounds = lambda bounds: self.fail("Unknown screen must not open Likes")
        driver.scroll_profile_grid = lambda xml: self.fail("Unknown screen must not scroll")
        with patch.object(a.time, "sleep"):
            result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count(
                "https://www.instagram.com/reel/target/", "author")
        self.assertEqual(result.status, "unavailable")

    def test_profile_gesture_uses_grid_bounds_and_reverses_before_downward_scroll(self):
        driver = a.AdbAndroidUiDriver.__new__(a.AdbAndroidUiDriver)
        screen = xml(node("clips_grid_recyclerview", bounds="[0,1200][1080,2300]"))
        with patch.object(driver, "_run") as run:
            driver.scroll_profile_grid(screen)
        commands = [c.args for c in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertLess(int(commands[0][4]), int(commands[0][6]))
        self.assertGreater(int(commands[1][4]), int(commands[1][6]))
        for command in commands:
            self.assertTrue(1200 < int(command[4]) < 2300)
            self.assertTrue(1200 < int(command[6]) < 2300)
        with patch.object(driver, "_run") as run:
            with self.assertRaises(a.AndroidMetricsError):
                driver.scroll_profile_grid(xml(node("clips_author_username", "author")))
            run.assert_not_called()

    def test_profile_labels_are_not_detail_surface(self):
        grid = xml(node("clips_grid_recyclerview") + node("preview_clip_thumbnail", desc="Reel by author. View Count 142K.") + node("profile_tab_icon_view", desc="Reels"))
        self.assertFalse(a._is_reel_detail_surface(grid))
        self.assertTrue(a._is_reel_detail_surface(xml(node("clips_author_username", "author"))))


    def test_queue_prioritizes_metrics_over_view_assist(self):
        with tempfile.TemporaryDirectory() as temp:
            row = {"url": "https://www.instagram.com/reel/target/", "collected_at": "2026-10-01T01:00:00Z", "username": "author"}
            job = queue_view_for_test(temp, row, assist_view_count=True)
            paths = b._android_metric_queue_paths(temp)
            self.assertEqual(len(list(paths["pending"].glob("*.json"))), 2)
            claimed, payload = b._claim_android_metric_job(paths)
            self.assertEqual(payload["job_id"], job)
            claimed.unlink()
            _, payload = b._claim_android_metric_job(paths)
            self.assertEqual(payload["kind"], "profile_view_count")

    def test_external_view_only_fills_missing_same_snapshot(self):
        old, new = "2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z"
        rows = [{"url": "u", "collected_at": new, "view_count": ""},
                {"url": "u", "collected_at": old, "view_count": ""},
                {"url": "v", "collected_at": new, "view_count": 100}]
        disk = [{"url": "u", "collected_at": new, "view_count": 7484},
                {"url": "v", "collected_at": new, "view_count": 200}]
        with patch.object(b, "enrich_reel_collection_rows", side_effect=lambda rows: rows):
            merged, _ = b._merge_external_android_metrics(rows, disk)
        self.assertEqual([r["view_count"] for r in merged], [7484, "", 100])

    def test_stored_web_integer_text_is_preserved_and_does_not_queue_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            row = {"url": "u", "collected_at": "new", "username": "author", "view_count": "0"}
            queue_view_for_test(temp, row, assist_view_count=True)
            paths = b._android_metric_queue_paths(temp)
            self.assertEqual(len(list(paths["pending"].glob("*.json"))), 1)
            with patch.object(b, "enrich_reel_collection_rows", side_effect=lambda rows: rows):
                merged, _ = b._merge_external_android_metrics([row], [
                    {"url": "u", "collected_at": "new", "view_count": "7484"},
                ])
            self.assertEqual(merged[0]["view_count"], "0")

    def test_external_view_rejects_late_observation_of_superseded_snapshot(self):
        old, new = "2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z"
        rows = [{"url": "u", "collected_at": old, "view_count": ""},
                {"url": "u", "collected_at": new, "view_count": ""}]
        disk = [{"url": "u", "collected_at": old, "view_count": 7484,
                 "view_count_observed_at": new, "view_count_source": "instagram_android_likes_panel"}]
        with patch.object(b, "enrich_reel_collection_rows", side_effect=lambda rows: rows):
            merged, changed = b._merge_external_android_metrics(rows, disk)
        self.assertEqual([row["view_count"] for row in merged], ["", ""])
        self.assertEqual(changed, 0)

    def test_late_android_view_completion_preserves_value_and_rejection_reason(self):
        for old_value in ("", 100):
            with self.subTest(old_value=old_value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = b._android_metric_queue_paths(root)
                history = root / b.REEL_HISTORY_DIRECTORY / b.REEL_HISTORY_FILENAME
                history.parent.mkdir(parents=True, exist_ok=True)
                history.write_text("placeholder", encoding="utf-8")
                url = "https://www.instagram.com/reels/target/"
                rows = [
                    {"url": url, "username": "author", "collected_at": "2026-10-29T06:15:18Z", "view_count": old_value},
                    {"url": url, "username": "author", "collected_at": "2026-10-29T06:20:40Z", "view_count": ""},
                ]
                completion = paths["completed"] / "late.json"
                b.write_json_atomic(completion, {
                    "kind": "profile_view_count", "status": "collected", "metrics": {"view_count": 236768},
                    "target": {"url": url, "collected_at": rows[0]["collected_at"]},
                    "completed_at": "2026-10-29T06:24:05Z", "observed_reel_url": url, "observed_username": "author",
                })
                with patch.object(_remediation_android_metric_worker, "read_csv_objects", return_value=(list(rows[0]), rows)), patch.object(_remediation_android_metric_worker, "write_csv_records"), patch.object(_remediation_android_metric_worker, "write_count_history_xlsx"):
                    b.apply_completed_android_metric_jobs(root, export_outputs=False)
                self.assertEqual([r["view_count"] for r in rows], [old_value, ""])
                rejected = b._read_android_metric_job(paths["root"] / "view_failed" / "late.json")
                if old_value == "":
                    self.assertTrue(rejected["needs_review"])
                    self.assertEqual(rejected["reason"], "observation_outside_snapshot_window")
                    self.assertEqual(rejected["metrics"], {"view_count": 236768})
                else:
                    self.assertIsNone(rejected)


class AndroidViewFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_web_timeout_preserves_partial_counts_and_defers_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            rows = [{"url": f"https://www.instagram.com/reel/{code}/", "collected_at": "2026-10-25T00:00:00Z", "username": "author", "view_count": ""} for code in ("found", "missing")]
            store = SimpleNamespace(rows=rows, lock=asyncio.Lock(), dirty=False, flush=AsyncMock(), csv_path=Path(temp)/"reels"/"history.csv")
            enricher = b.ProfileReelViewEnricher(store, android_fallback=True)
            for row in rows:
                enricher.track(row)
            async def timeout_lookup(*args, observed_counts, **kwargs):
                observed_counts["found"] = 0
                await asyncio.sleep(1)
            profile = {"status": "success", "accountCountry": "South Korea"}
            with patch.object(b, "_read_profile_reel_view_counts", timeout_lookup), patch.object(b, "PROFILE_REEL_VIEW_INITIAL_TIMEOUT_SECONDS", 0.001), patch.object(b, "start_android_metric_worker") as start:
                await enricher(None, {"username": "author"}, profile)
            jobs = list(b._android_metric_queue_paths(temp)["pending"].glob("*.json"))
            self.assertEqual(jobs, [])
            self.assertNotIn("profileReelViewFallbackQueued", profile)
            self.assertEqual(rows[0]["view_count"], 0)
            self.assertEqual(rows[1]["view_count"], "")
            self.assertIn((rows[1]["url"], rows[1]["collected_at"]), enricher.pending)
            start.assert_not_called()

    def test_likes_text_opens_exact_view_panel_without_profile_navigation(self):
        driver = Driver("2,474,419")
        dump = driver.dump_ui
        driver.dump_ui = lambda: dump().replace('resource-id="com.instagram.android:id/like_count" text="607"', 'resource-id="com.instagram.android:id/counter" text="Likes"')
        with patch.object(a.time, "sleep"):
            result = a.AndroidReelMetricsEnricher(driver=driver).collect_profile_view_count("https://www.instagram.com/reel/target/", "author")
        self.assertEqual(result.metrics, {"view_count": 2474419})
        self.assertEqual(driver.surface, "detail")

    async def test_fallback_waits_until_web_tail_retry_is_exhausted(self):
        with tempfile.TemporaryDirectory() as temp:
            row = {"url": "https://www.instagram.com/reel/target/", "collected_at": "new", "username": "author", "view_count": ""}
            store = SimpleNamespace(rows=[row], lock=asyncio.Lock(), dirty=False,
                                    flush=AsyncMock(), csv_path=Path(temp)/"reels"/"history.csv")
            enricher = b.ProfileReelViewEnricher(store, android_fallback=True)
            enricher.track(row)
            with patch.object(b, "read_profile_reel_view_counts", AsyncMock(return_value={})), patch.object(b, "start_android_metric_worker") as start:
                await enricher(None, {"username": "author"}, {"status": "success", "accountCountry": "South Korea"})
                paths = b._android_metric_queue_paths(temp)
                self.assertFalse(list(paths["pending"].glob("*.json")))
                start.assert_not_called()
                await enricher(None, {"username": "author", "viewRetry": "1"}, {"status": "success", "accountCountry": "South Korea"})
                jobs = list(paths["pending"].glob("*.json"))
                self.assertEqual(len(jobs), 1)
                self.assertEqual(b._read_android_metric_job(jobs[0])["reason"], "web_view_unavailable")
                start.assert_called_once()

    async def test_successful_web_retry_does_not_queue_android_fallback(self):
        row = {"url": "https://www.instagram.com/reel/target/", "collected_at": "2026-10-25T00:00:00Z", "username": "author", "view_count": ""}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SimpleNamespace(rows=[row], lock=asyncio.Lock(), dirty=False, flush=AsyncMock(), csv_path=Path(temporary.name) / "reels" / "history.csv")
        enricher = b.ProfileReelViewEnricher(store, android_fallback=True)
        enricher.track(row)
        with patch.object(b, "read_profile_reel_view_counts", AsyncMock(return_value={"target": 0})), patch.object(b, "enqueue_android_view_count_job") as enqueue:
            await enricher(None, {"username": "author", "viewRetry": "1"}, {"status": "success", "accountCountry": "South Korea"})
        enqueue.assert_not_called()
        self.assertEqual(row["view_count"], 0)

    def test_normal_metric_job_never_queues_speculative_view_assist(self):
        with tempfile.TemporaryDirectory() as temp:
            b.enqueue_android_metric_job(temp, {"url": "u", "collected_at": "new", "username": "author", "view_count": ""})
            jobs = list(b._android_metric_queue_paths(temp)["pending"].glob("*.json"))
            self.assertEqual(len(jobs), 1)
            self.assertNotEqual(b._read_android_metric_job(jobs[0]).get("kind"), "profile_view_count")
