from src.reels import android_metric_worker as _remediation_android_metric_worker
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.reels import instagram_reels_browser as browser
from src.reels.instagram_follower_enricher import FollowerEnricher


class ProfilePlayCountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Country UI has its own suite; these fixtures intentionally provide
        # only the page methods used by view-count collection.
        country = patch.object(browser, "read_profile_account_country", AsyncMock(return_value=""))
        country.start()
        self.addCleanup(country.stop)

    async def test_initial_lookup_uses_thirty_seconds_and_tail_retry_uses_sixty(self):
        real_wait_for = asyncio.wait_for
        for retry, expected in ((False, 30.0), (True, 60.0)):
            with self.subTest(retry=retry), patch.object(browser, "_read_profile_reel_view_counts", AsyncMock(return_value={"target": 7111})), patch.object(browser.asyncio, "wait_for", wraps=real_wait_for) as wait:
                self.assertEqual(await browser.read_profile_reel_view_counts(object(), "author", {"target"}, retry=retry), {"target": 7111})
                self.assertEqual(wait.call_args.kwargs["timeout"], expected)

    async def test_initial_timeout_preserves_counts_already_observed(self):
        observed = {"first": 7484}
        with patch.object(browser, "PROFILE_REEL_VIEW_INITIAL_TIMEOUT_SECONDS", 0.001), patch.object(browser, "_read_profile_reel_view_counts", AsyncMock(side_effect=lambda *args, **kwargs: None)):
            async def pending_lookup(*args, **kwargs):
                await asyncio.sleep(10)
            with patch.object(browser, "_read_profile_reel_view_counts", pending_lookup):
                result = await browser.read_profile_reel_view_counts(object(), "author", {"first", "missing"}, observed_counts=observed)
        self.assertEqual(result, {"first": 7484})
    async def test_profile_grid_dom_can_finish_without_raw_play_response(self):
        page = SimpleNamespace(url="https://www.instagram.com/author/reels/", on=Mock(), remove_listener=Mock(),
                               goto=AsyncMock(return_value=None), mouse=SimpleNamespace(wheel=AsyncMock()))
        with patch.object(browser, "collect_embedded_reel_metadata", AsyncMock()), patch.object(
            browser, "read_exact_profile_grid_counts", AsyncMock(return_value={"target": 7111})
        ):
            self.assertEqual(await browser.read_profile_reel_view_counts(page, "author", {"target"}), {"target": 7111})
        page.mouse.wheel.assert_not_awaited()

    async def test_initial_passive_count_avoids_second_profile_navigation(self):
        page = SimpleNamespace(url="https://www.instagram.com/author/reels/", on=Mock(), remove_listener=Mock(), goto=AsyncMock())
        seed = {"target": {"username": "author", "viewCount": 23649, "viewSourceField": "play_count"}}
        with patch.object(browser, "collect_embedded_reel_metadata", AsyncMock()):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target"}, initial_metadata=seed)
        self.assertEqual(counts, {"target": 23649})
        page.goto.assert_not_awaited()

    async def test_profile_grid_only_accepts_full_integer_labels(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value=[
            {"shortcode": "small", "text": "7,111"}, {"shortcode": "zero", "text": "0"},
            {"shortcode": "compact", "text": "23.6K"}, {"shortcode": "hover", "text": "112\n13"},
        ]))
        self.assertEqual(await browser.read_exact_profile_grid_counts(page, "author"), {"small": 7111, "zero": 0})

    async def test_reels_route_follower_lookup_accepts_author_and_preserves_views(self):
        page = SimpleNamespace(url="https://www.instagram.com/author/reels/", on=Mock(), remove_listener=Mock(),
                               goto=AsyncMock(return_value=None), evaluate=AsyncMock(return_value={
                                   "profileText": "100 followers", "profileTexts": ["100 followers"],
                               }))
        result = await browser.request_web_follower_count(page, "author", reels=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["followerCount"], 100)
        self.assertIn("_reel_metadata", result)
        self.assertEqual(page.goto.await_args.args[0], "https://www.instagram.com/author/reels/")

    async def test_reels_route_follower_lookup_rejects_other_author(self):
        page = SimpleNamespace(url="https://www.instagram.com/other/reels/", on=Mock(), remove_listener=Mock(),
                               goto=AsyncMock(return_value=None))
        result = await browser.request_web_follower_count(page, "author", reels=True)
        self.assertEqual(result["status"], "identity_mismatch")

    def test_recollection_uses_only_fresh_exact_reel_counts(self):
        counts = browser.recollection_detail_counts(
            {"userId": "1", "username": "author", "commentCount": 7,
             "likeCount": 22, "repostCount": 0, "viewCount": 123,
             "viewSourceField": "play_count"},
            user_id="1", username="author", shortcode="target",
        )
        self.assertEqual(counts, {"comment_count": 7, "like_count": 22,
                                  "repost_count": 0, "view_count": 123})
        with self.assertRaises(browser.CrawlerAccessError) as missing:
            browser.recollection_detail_counts(
                {"likeCount": 22}, user_id="1", username="author", shortcode="target",
            )
        self.assertEqual(missing.exception.code, "metric_unavailable")
        with self.assertRaises(browser.CrawlerAccessError) as mismatch:
            browser.recollection_detail_counts(
                {"userId": "2", "commentCount": 7},
                user_id="1", username="author", shortcode="target",
            )
        self.assertEqual(mismatch.exception.code, "identity_mismatch")

    async def test_detail_recollection_stops_when_exact_comment_arrives_without_like(self):
        page = SimpleNamespace(
            on=Mock(), remove_listener=Mock(), goto=AsyncMock(return_value=None),
            wait_for_timeout=AsyncMock(), url="https://www.instagram.com/",
        )

        async def embedded(_page, metadata, **_kwargs):
            metadata["target"] = {"commentCount": 7, "viewCount": 123, "viewSourceField": "play_count"}

        with patch.object(browser, "collect_embedded_reel_metadata", side_effect=embedded):
            detail = await browser.read_reel_detail_metadata(
                page, "target", username="author", required_fields=("commentCount",),
            )

        self.assertEqual(detail["commentCount"], 7)
        self.assertEqual(detail["viewCount"], 123)
        page.goto.assert_awaited_once()

    async def test_detail_navigation_uses_post_route(self):
        for metadata_author, fallback_author, expected in (
            ("author", "fallback", "author/p/target/"),
            ("", "fallback", "fallback/p/target/"),
            ("", "", "p/target/"),
        ):
            with self.subTest(expected=expected):
                page = SimpleNamespace(
                    on=Mock(), remove_listener=Mock(), goto=AsyncMock(return_value=None),
                    wait_for_timeout=AsyncMock(), url="https://www.instagram.com/",
                )
                with patch.object(browser, "collect_embedded_reel_metadata", AsyncMock()):
                    await browser.read_reel_detail_metadata(
                        page, "target", {"username": metadata_author, "likeCount": 1, "commentCount": 2},
                        username=fallback_author,
                    )
                page.goto.assert_awaited_once_with(
                    "https://www.instagram.com/" + expected,
                    wait_until="domcontentloaded", timeout=30_000,
                )

    def row(self, code="target", at="new", **fields):
        at = {"old":"2026-10-22T00:00:00Z", "new":"2026-10-22T04:00:00Z", "later":"2026-10-22T08:00:00Z"}.get(at, at)
        return {"url": f"https://www.instagram.com/reel/{code}/", "collected_at": at,
                "user_id": "1", "username": "author", "view_count": "", **fields}

    def store(self, rows):
        return SimpleNamespace(rows=rows, lock=asyncio.Lock(), flush=AsyncMock(), dirty=False)

    async def test_retry_can_exceed_120_scrolls_without_fixed_half_second_wait(self):
        state = {"scrolls": 0}
        async def scroll(*_args):
            state["scrolls"] += 1
        async def embedded(_page, metadata, **_kwargs):
            if state["scrolls"] >= 125:
                metadata["target"] = {"viewCount": 17, "viewSourceField": "play_count"}
        page = SimpleNamespace(url="https://www.instagram.com/author/reels/", on=Mock(),
            remove_listener=Mock(), goto=AsyncMock(), evaluate=AsyncMock(), wait_for_timeout=AsyncMock(),
            mouse=SimpleNamespace(wheel=AsyncMock(side_effect=scroll)))
        with patch.object(browser, "collect_embedded_reel_metadata", side_effect=embedded), patch.object(
            browser, "profile_reel_scroll_state", AsyncMock(side_effect=lambda _page: [state["scrolls"], 0, 1000, []])
        ), patch.object(browser, "read_exact_profile_grid_counts", AsyncMock(return_value={})):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target"}, retry=True)
        self.assertEqual(counts, {"target": 17})
        self.assertEqual(state["scrolls"], 125)
        page.goto.assert_not_awaited()
        self.assertIn("25%", page.evaluate.await_args_list[0].args[0])
        self.assertIn("style.zoom = ''", page.evaluate.await_args_list[-1].args[0])
        self.assertTrue(all(call.args[0] != 500 for call in page.wait_for_timeout.await_args_list))

    async def test_retry_waits_for_late_card_after_eight_unchanged_scrolls(self):
        state = {"scrolls": 0}
        async def scroll(*_args):
            state["scrolls"] += 1
        async def cards(*_args):
            return {"DccARklyRRG": 134} if state["scrolls"] >= 12 else {}
        page = SimpleNamespace(url="https://www.instagram.com/5fl.kr/reels/", on=Mock(),
            remove_listener=Mock(), goto=AsyncMock(), evaluate=AsyncMock(), wait_for_timeout=AsyncMock(),
            mouse=SimpleNamespace(wheel=AsyncMock(side_effect=scroll)))
        with patch.object(browser, "collect_embedded_reel_metadata", AsyncMock()), patch.object(
            browser, "profile_reel_scroll_state", AsyncMock(return_value=[0, 0, 1000, []])
        ), patch.object(browser, "read_exact_profile_grid_counts", side_effect=cards):
            counts = await browser.read_profile_reel_view_counts(page, "5fl.kr", {"DccARklyRRG"}, retry=True)
        self.assertEqual(counts, {"DccARklyRRG": 134})
        self.assertEqual(state["scrolls"], 12)

    async def test_retry_stops_after_twenty_consecutive_unchanged_scrolls(self):
        page = SimpleNamespace(url="https://www.instagram.com/author/reels/", on=Mock(),
            remove_listener=Mock(), goto=AsyncMock(), evaluate=AsyncMock(), wait_for_timeout=AsyncMock(),
            mouse=SimpleNamespace(wheel=AsyncMock()))
        with patch.object(browser, "collect_embedded_reel_metadata", AsyncMock()), patch.object(
            browser, "profile_reel_scroll_state", AsyncMock(return_value=[0, 0, 1000, []])
        ), patch.object(browser, "read_exact_profile_grid_counts", AsyncMock(return_value={})):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target"}, retry=True)
        self.assertEqual(counts, {})
        self.assertEqual(page.mouse.wheel.await_count, 20)

    async def test_retry_timeout_preserves_partial_results_and_restores_zoom(self):
        async def wait(_ms):
            await asyncio.sleep(10)
        page = SimpleNamespace(url="https://www.instagram.com/author/reels/", on=Mock(),
            remove_listener=Mock(), goto=AsyncMock(), evaluate=AsyncMock(), wait_for_timeout=wait,
            mouse=SimpleNamespace(wheel=AsyncMock()))
        async def embedded(_page, metadata, **_kwargs):
            metadata["first"] = {"viewCount": 17, "viewSourceField": "play_count"}
        observed = {}
        with patch.object(browser, "PROFILE_REEL_VIEW_RETRY_TIMEOUT_SECONDS", 0.02), patch.object(
            browser, "collect_embedded_reel_metadata", side_effect=embedded
        ), patch.object(browser, "read_exact_profile_grid_counts", AsyncMock(return_value={})):
            counts = await browser.read_profile_reel_view_counts(
                page, "author", {"first", "missing"}, retry=True, observed_counts=observed)
        self.assertEqual(counts, {"first": 17})
        self.assertEqual(observed, {"first": 17})
        page.remove_listener.assert_called_once()
        self.assertIn("style.zoom = ''", page.evaluate.await_args_list[-1].args[0])

    async def test_tail_retry_flag_is_forwarded_to_profile_view_lookup(self):
        row = self.row()
        enrich = browser.ProfileReelViewEnricher(self.store([row]))
        enrich.track(row)
        with patch.object(browser, "read_profile_reel_view_counts", AsyncMock(return_value={})) as lookup:
            await enrich(object(), {"username": "author", "viewRetry": "1"}, {"status": "success"})
        self.assertTrue(lookup.await_args.kwargs["retry"])

    async def test_likes_fallback_only_after_web_tail_retry_failure(self):
        for retry, found in ((False, {}), (True, {}), (True, {"target": 42})):
            with self.subTest(retry=retry, found=found):
                row = self.row()
                store = self.store([row])
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                store.csv_path = Path(temporary.name) / ".collector" / "reels_history_active.csv"
                enrich = browser.ProfileReelViewEnricher(store, android_fallback=True)
                enrich.track(row)
                async def lookup(*args, **kwargs):
                    if not found:
                        kwargs["on_timeout"]()
                    return found
                payload = {"username": "author", "userId": "1"}
                if retry:
                    payload["viewRetry"] = "1"
                with patch.object(browser, "read_profile_reel_view_counts", side_effect=lookup), patch.object(
                    browser, "enqueue_android_view_count_job", return_value="job"
                ) as enqueue, patch.object(browser, "start_android_metric_worker") as start, patch.object(
                    enrich, "classify_profile_rows", AsyncMock()
                ):
                    await enrich(object(), payload, {"status": "success"})
                self.assertEqual(enqueue.call_count, int(retry and not found))
                self.assertEqual(start.call_count, int(retry and not found))

    def test_likes_view_fallback_is_enabled(self):
        self.assertTrue(browser.ANDROID_PROFILE_VIEW_FALLBACK_ENABLED)

    async def test_profile_duration_only_fills_matching_tracked_reel(self):
        row, other = self.row(), self.row('other')
        store = self.store([row, other])
        enrich = browser.ProfileReelViewEnricher(store)
        enrich.track(row)
        enrich.track(other)
        await enrich.enrich_profile_durations('author', '1', {'_reel_metadata': {
            'target': {'userId': '1', 'username': 'author', 'videoDurationSeconds': 12.3456},
            'other': {'userId': '2', 'username': 'different', 'videoDurationSeconds': 99},
        }})
        self.assertEqual(row['video_duration_seconds'], 12.3456)
        self.assertNotIn('video_duration_seconds', other)
        store.flush.assert_awaited_once()

    async def test_profile_reports_already_collected_views_only_for_tracked_reels(self):
        old = self.row(at="old", view_count=999)
        first = self.row(view_count=16951, video_duration_seconds=12.5)
        second = self.row("second", view_count=17212, video_duration_seconds=15.0)
        store = self.store([old, first, second])
        enrich = browser.ProfileReelViewEnricher(store)
        enrich.track(first)
        enrich.track(second)
        profile = {"status": "success"}
        with patch.object(browser, "read_profile_reel_view_counts", AsyncMock()) as lookup:
            await enrich(object(), {"username": "author", "userId": "1"}, profile)
        lookup.assert_not_awaited()
        self.assertEqual(profile["profileReelViewCounts"], [16951, 17212])

    def test_public_profile_serverjs_envelope_is_not_truncated(self):
        payload = {"data": {"xig_user_by_username": {
            "polaris_clips_connection": {"edges": [{"node": {"code": "target", "play_count": 45293}}]}
        }}}
        payload = {"__bbox": {"result": payload}}
        payload = {"require": [["RelayPrefetchedStreamCache", [], ["key", payload]]]}
        payload = {"require": [["ScheduledServerJS", [], [{"__bbox": payload}]]]}
        found = {}
        browser.collect_reel_metadata(payload, found)
        self.assertEqual(found["target"]["viewCount"], 45293)

    async def test_batches_author_reels_and_only_fills_current_snapshots(self):
        old, first, second, other = self.row(at="old"), self.row(like_count=17), self.row("second"), self.row("other", username="other")
        store = self.store([old, first, second, other])
        enrich = browser.ProfileReelViewEnricher(store)
        for row in (first, second, other):
            enrich.track(row)
        page = object()
        lookup = AsyncMock(return_value={"target": 45293, "second": 0, "other": 999})
        profile = {"status": "success", "userId": "1"}
        with patch.object(browser, "read_profile_reel_view_counts", lookup):
            await enrich(page, {"username": "author", "userId": "1"}, profile)
        self.assertEqual(lookup.await_args.args, (page, "author", {"target", "second"}))
        self.assertEqual([r["view_count"] for r in store.rows], ["", 45293, 0, ""])
        self.assertEqual(first["like_count"], 17)
        store.flush.assert_awaited_once()
        self.assertEqual(len(enrich.pending), 1)
        self.assertEqual(profile["profileReelsUpdated"], 2)
        self.assertEqual(profile["profileReelViewCounts"], [45293, 0])

    async def test_followers_update_only_current_batch_even_when_views_missing(self):
        old = self.row(at="old", follower_count=10)
        first, second = self.row(), self.row("second")
        other = self.row("other", username="other")
        store = self.store([old, first, second, other])
        enrich = browser.ProfileReelViewEnricher(store)
        for row in (first, second, other):
            enrich.track(row)
        with patch.object(browser, "read_profile_reel_view_counts", AsyncMock(return_value={})):
            await enrich(object(), {"username": "author", "userId": "1"}, {
                "status": "success", "userId": "1", "followerCount": 961,
            })
        self.assertEqual([row.get("follower_count") for row in store.rows], [10, 961, 961, None])
        self.assertEqual(len(enrich.pending), 3)

    async def test_concurrent_android_fields_and_exact_count_are_preserved(self):
        row = self.row()
        store = self.store([row])
        enrich = browser.ProfileReelViewEnricher(store)
        enrich.track(row)
        async def lookup(*args, **kwargs):
            row.update(view_count=700, share_count=123)
            return {"target": 600}
        with patch.object(browser, "read_profile_reel_view_counts", side_effect=lookup):
            await enrich(object(), {"username": "author", "userId": "1"}, {"status": "success"})
        self.assertEqual(row["view_count"], 700)
        self.assertEqual(row["share_count"], 123)

    async def test_missing_follower_count_does_not_block_profile_play_count(self):
        row = self.row()
        store = self.store([row])
        enrich = browser.ProfileReelViewEnricher(store)
        enrich.track(row)
        with patch.object(browser, "read_profile_reel_view_counts", AsyncMock(return_value={"target": 45293})):
            counts = await enrich(object(), {"username": "author", "userId": "1"}, {
                "status": "web_unavailable", "userId": "1",
            })
        self.assertEqual(counts, {"target": 45293})
        self.assertEqual(row["view_count"], 45293)

    async def test_profile_visit_caches_other_reels_for_later_snapshots(self):
        first = self.row()
        later = self.row("later", at="later")
        store = self.store([first, later])
        enrich = browser.ProfileReelViewEnricher(store)
        enrich.track(first)

        async def lookup(_page, _username, _targets, *, observed_counts, **_kwargs):
            observed_counts.update({"target": 45293, "later": 1234})
            return {"target": 45293}

        with patch.object(browser, "read_profile_reel_view_counts", side_effect=lookup) as read:
            await enrich(object(), {"username": "author"}, {"status": "success"})
            self.assertIsNone(enrich.cached_count(later))
            enrich.track(later)
            counts = await enrich(object(), {"username": "author"}, {"status": "success"})

        self.assertEqual(read.await_count, 2)
        self.assertEqual(counts, {"later": 1234})
        self.assertEqual(later["view_count"], 1234)

    async def test_unchanged_embedded_scripts_are_parsed_once(self):
        payload = '{"code":"target","play_count":45293}'
        page = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(all_text_contents=AsyncMock(return_value=[payload]))))
        metadata = {}
        seen = set()
        with patch.object(browser, "collect_reel_metadata", wraps=browser.collect_reel_metadata) as parse:
            await browser.collect_embedded_reel_metadata(page, metadata, seen_scripts=seen)
            await browser.collect_embedded_reel_metadata(page, metadata, seen_scripts=seen)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(metadata["target"]["viewCount"], 45293)

    async def test_new_response_ends_profile_settle_without_full_wait(self):
        response = SimpleNamespace(
            url="https://www.instagram.com/graphql/query",
            headers={"content-type": "application/json"},
            text=AsyncMock(return_value=json.dumps({"code": "target", "play_count": 45293})),
        )
        page = SimpleNamespace(url="", on=Mock(), remove_listener=Mock(), goto=AsyncMock(return_value=None),
                               mouse=SimpleNamespace(wheel=AsyncMock()))
        async def wait(milliseconds):
            if page.wait_for_timeout.await_count == 1:
                page.on.call_args.args[1](response)
            await asyncio.sleep(0)
        page.wait_for_timeout = AsyncMock(side_effect=wait)
        with patch.object(browser, "collect_embedded_reel_metadata", AsyncMock()):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target"})
        self.assertEqual(counts, {"target": 45293})
        self.assertLess(page.wait_for_timeout.await_count, 7)
        page.mouse.wheel.assert_not_awaited()

    async def test_access_failure_and_identity_mismatch_do_not_navigate(self):
        for profile in ({"status": "rate_limited"}, {"status": "success", "userId": "2"}):
            row = self.row()
            enrich = browser.ProfileReelViewEnricher(self.store([row]))
            enrich.track(row)
            with patch.object(browser, "read_profile_reel_view_counts", new_callable=AsyncMock) as lookup:
                await enrich(object(), {"username": "author", "userId": "1"}, profile)
                lookup.assert_not_awaited()

    async def test_missing_and_compact_counts_remain_pending(self):
        row = self.row()
        store = self.store([row])
        enrich = browser.ProfileReelViewEnricher(store)
        enrich.track(row)
        for counts in ({}, {"target": "45.2K"}):
            with patch.object(browser, "read_profile_reel_view_counts", AsyncMock(return_value=counts)):
                await enrich(object(), {"username": "author"}, {"status": "success"})
        self.assertEqual(row["view_count"], "")
        self.assertEqual(len(enrich.pending), 1)
        store.flush.assert_not_awaited()

    async def test_existing_profile_payload_skips_reels_navigation(self):
        page = SimpleNamespace(
            url="https://www.instagram.com/author/",
            on=Mock(), remove_listener=Mock(), goto=AsyncMock(),
        )

        async def embedded(_page, metadata, **_kwargs):
            metadata["target"] = {"viewCount": 45293, "viewSourceField": "play_count"}
            metadata["later"] = {"viewCount": 1234, "viewSourceField": "play_count"}
            metadata["foreign"] = {"username": "other", "viewCount": 999, "viewSourceField": "play_count"}

        observed = {}
        with patch.object(browser, "collect_embedded_reel_metadata", side_effect=embedded):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target"}, observed_counts=observed)

        self.assertEqual(counts, {"target": 45293})
        self.assertEqual(observed, {"target": 45293, "later": 1234})
        page.goto.assert_not_awaited()

    async def test_unrelated_loaded_reels_skip_duplicate_full_scroll(self):
        page = SimpleNamespace(
            url="https://www.instagram.com/author/",
            on=Mock(), remove_listener=Mock(), goto=AsyncMock(return_value=None),
            wait_for_timeout=AsyncMock(), mouse=SimpleNamespace(wheel=AsyncMock()),
            evaluate=AsyncMock(return_value=[0, 0, 1000, []]),
        )

        calls = 0

        async def embedded(_page, metadata, **_kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                metadata["other"] = {"viewCount": 100, "viewSourceField": "play_count"}

        with (
            patch.object(browser, "collect_embedded_reel_metadata", side_effect=embedded),
            patch.object(browser, "PROFILE_REEL_VIEW_SCROLL_ATTEMPTS", 2),
        ):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target"})

        self.assertEqual(counts, {})
        page.goto.assert_awaited_once()
        page.mouse.wheel.assert_awaited_once()

    async def test_profile_scroll_stops_after_repeated_bottom_without_new_reels(self):
        page = SimpleNamespace(
            url="https://www.instagram.com/author/",
            on=Mock(), remove_listener=Mock(), goto=AsyncMock(return_value=None),
            wait_for_timeout=AsyncMock(), mouse=SimpleNamespace(wheel=AsyncMock()),
            evaluate=AsyncMock(return_value=[0, 0, 1000, ["other"]]),
        )

        async def embedded(_page, metadata, **_kwargs):
            metadata["other"] = {"viewCount": 100, "viewSourceField": "play_count"}

        with (
            patch.object(browser, "collect_embedded_reel_metadata", side_effect=embedded),
            patch.object(browser, "PROFILE_REEL_VIEW_PAGE_ATTEMPTS", 1),
        ):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target"})

        self.assertEqual(counts, {})
        self.assertEqual(page.mouse.wheel.await_count, browser.PROFILE_REEL_VIEW_MAX_UNCHANGED_SCROLLS)
        page.goto.assert_awaited_once()

    async def test_new_reels_after_stall_allow_profile_scroll_to_continue(self):
        state = {"scrolls": 0}

        async def scroll(*_args):
            state["scrolls"] += 1

        async def scroll_state(*_args):
            codes = ["other", "later"] if state["scrolls"] >= 7 else ["other"]
            return [0, 0, 1000, codes]

        async def embedded(_page, metadata, **_kwargs):
            metadata["other"] = {"viewCount": 100, "viewSourceField": "play_count"}
            if state["scrolls"] >= 15:
                metadata["target"] = {"viewCount": 45293, "viewSourceField": "play_count"}

        page = SimpleNamespace(
            url="https://www.instagram.com/author/",
            on=Mock(), remove_listener=Mock(), goto=AsyncMock(return_value=None),
            wait_for_timeout=AsyncMock(), mouse=SimpleNamespace(wheel=AsyncMock(side_effect=scroll)),
            evaluate=AsyncMock(side_effect=scroll_state),
        )
        with patch.object(browser, "collect_embedded_reel_metadata", side_effect=embedded):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target"})

        self.assertEqual(counts, {"target": 45293})
        self.assertEqual(state["scrolls"], 15)
        page.goto.assert_awaited_once()

    async def test_older_reel_can_be_found_after_thirty_scrolls(self):
        state = {"scrolls": 0}

        async def navigate(*args, **kwargs):
            state["scrolls"] = 0

        async def scroll(*args):
            state["scrolls"] += 1

        async def embedded(_page, metadata, **_kwargs):
            metadata["other"] = {"viewCount": 100, "viewSourceField": "play_count"}
            if state["scrolls"] >= 35:
                metadata["target"] = {"viewCount": 45293, "viewSourceField": "play_count"}

        page = SimpleNamespace(
            url="https://www.instagram.com/author/",
            on=Mock(), remove_listener=Mock(), goto=AsyncMock(side_effect=navigate),
            wait_for_timeout=AsyncMock(), mouse=SimpleNamespace(wheel=AsyncMock(side_effect=scroll)),
            evaluate=AsyncMock(side_effect=lambda *_args: [
                state["scrolls"] * 1500, state["scrolls"] * 1500, 100_000, ["other"],
            ]),
        )
        with patch.object(browser, "collect_embedded_reel_metadata", side_effect=embedded):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target"})

        self.assertEqual(counts, {"target": 45293})
        page.goto.assert_awaited_once()

    async def test_existing_profile_count_survives_reels_navigation_failure(self):
        page = SimpleNamespace(
            url="https://www.instagram.com/author/",
            on=Mock(), remove_listener=Mock(), goto=AsyncMock(side_effect=RuntimeError("offline")),
            wait_for_timeout=AsyncMock(),
        )

        async def embedded(_page, metadata, **_kwargs):
            metadata["target"] = {"viewCount": 45293, "viewSourceField": "play_count"}

        with (
            patch.object(browser, "collect_embedded_reel_metadata", side_effect=embedded),
            patch.object(browser, "PROFILE_REEL_VIEW_PAGE_ATTEMPTS", 1),
        ):
            counts = await browser.read_profile_reel_view_counts(page, "author", {"target", "missing"})

        self.assertEqual(counts, {"target": 45293})

    async def test_follower_callback_uses_same_page_after_follower_lookup(self):
        page = SimpleNamespace(context=object(), is_closed=lambda: False)
        order = []
        async def followers(p, username):
            order.append("followers")
            return {"status": "success", "followerCount": 123}
        async def views(p, payload, result):
            self.assertIs(p, page)
            self.assertEqual(result["followerCount"], 123)
            order.append("views")
        lookup = browser.SequentialWebFollowerLookup(page, on_profile=views)
        with patch.object(browser, "request_web_follower_count", side_effect=followers):
            await lookup({"username": "author"})
        self.assertEqual(order, ["followers", "views"])

    async def test_fresh_follower_can_be_requeued_for_new_reel_views(self):
        with tempfile.TemporaryDirectory() as directory:
            lookup = AsyncMock(return_value={"status": "success", "followerCount": 100, "sourceField": "follower_count"})
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup)
            await enricher.track_user(user_id="1", username="author")
            await enricher.drain()
            await enricher.track_user(user_id="1", username="author", enqueue=False)
            self.assertEqual(await enricher.enqueue_tracked(force=True), 1)
            await enricher.drain()
            self.assertEqual(lookup.await_count, 2)

    async def test_follower_progress_includes_updated_reel_count(self):
        with tempfile.TemporaryDirectory() as directory:
            progress = []
            lookup = AsyncMock(return_value={
                "status": "success", "followerCount": 42_411,
                "sourceField": "edge_followed_by.count", "profileReelsUpdated": 1,
            })
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup, on_progress=progress.append)
            await enricher.track_user(user_id="1", username="author")
            await enricher.drain()

        self.assertEqual(progress[0]["profileReelsUpdated"], 1)

    async def test_deferred_metrics_do_not_visit_profile_during_reel_capture(self):
        with (
            patch.object(browser, "collect_embedded_reel_metadata", AsyncMock()),
            patch.object(browser, "wait_for_reel_metadata", AsyncMock(return_value={"username": "author"})),
            patch.object(browser, "open_reel_from_creator_profile", AsyncMock()) as open_profile,
        ):
            await browser.resolve_page_first_reel_metadata(object(), {"shortcode": "target"}, {}, defer_profile_metrics=True)
            open_profile.assert_not_awaited()

    def test_web_snapshot_can_wait_for_profile_metrics_without_fake_zero(self):
        row = self.row(uploaded_at="2026-09-27T00:00:00Z", ad="false", like_count=1, comment_count=0, follower_count="")
        self.assertFalse(browser.has_complete_reel_core_data(row))
        self.assertTrue(browser.has_complete_reel_core_data(row, allow_pending_profile_metrics=True))
        self.assertEqual(row["view_count"], "")
