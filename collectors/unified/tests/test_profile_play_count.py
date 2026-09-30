import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from reels import instagram_reels_browser as browser
from reels.instagram_follower_enricher import FollowerEnricher


class ProfilePlayCountTests(unittest.IsolatedAsyncioTestCase):
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
        return {"url": f"https://www.instagram.com/reel/{code}/", "collected_at": at,
                "user_id": "1", "username": "author", "view_count": "", **fields}

    def store(self, rows):
        return SimpleNamespace(rows=rows, lock=asyncio.Lock(), flush=AsyncMock(), dirty=False)

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
        with patch.object(browser, "read_profile_reel_view_counts", lookup):
            await enrich(page, {"username": "author", "userId": "1"}, {"status": "success", "userId": "1"})
        self.assertEqual(lookup.await_args.args, (page, "author", {"target", "second"}))
        self.assertEqual([r["view_count"] for r in store.rows], ["", 45293, 0, ""])
        self.assertEqual(first["like_count"], 17)
        store.flush.assert_awaited_once()
        self.assertEqual(len(enrich.pending), 1)

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

        async def lookup(_page, _username, _targets, *, observed_counts):
            observed_counts.update({"target": 45293, "later": 1234})
            return {"target": 45293}

        with patch.object(browser, "read_profile_reel_view_counts", side_effect=lookup) as read:
            await enrich(object(), {"username": "author"}, {"status": "success"})
            self.assertEqual(enrich.cached_count(later), 1234)
            enrich.track(later)
            counts = await enrich(object(), {"username": "author"}, {"status": "success"})

        self.assertEqual(read.await_count, 1)
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
