import tempfile
import unittest

from src.reels.instagram_follower_enricher import FollowerEnricher


class FollowerTailRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_android_fallback_queued_skips_web_view_tail_retry(self):
        calls = []
        async def lookup(payload):
            calls.append(payload)
            return {"status": "success", "followerCount": 123, "sourceField": "profile_header_text",
                    "profileReelViewCounts": [None], "profileReelViewFallbackQueued": True}
        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup)
            await enricher.track_user(username="author")
            await enricher.drain()
        self.assertEqual(len(calls), 1)

    async def test_successful_follower_with_missing_views_retries_at_tail(self):
        calls = []
        retry_flags = []

        async def lookup(payload):
            calls.append(payload["username"])
            retry_flags.append(payload.get("viewRetry"))
            return {"status": "success", "followerCount": 123,
                    "sourceField": "profile_header_text",
                    "profileReelViewCounts": [None, 0] if payload["username"] == "first" else [456]}

        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup)
            await enricher.track_user(username="first")
            await enricher.track_user(username="second")
            stats = await enricher.drain()
        self.assertEqual(calls, ["first", "second", "first"])
        self.assertEqual(retry_flags, [None, None, "1"])
        self.assertEqual(stats["queued"], stats["completed"])

    async def run_queue(self, recover=False, status="web_unavailable"):
        calls = []

        async def lookup(payload):
            username = payload["username"]
            calls.append(username)
            if username == "first" and (not recover or calls.count(username) == 1):
                return {"status": status, "error": "Missing count"}
            return {"status": "success", "followerCount": 123,
                    "sourceField": "profile_header_text"}

        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup)
            await enricher.track_user(username="first")
            await enricher.track_user(username="second")
            stats = await enricher.drain()
            row = enricher.username_index["first"]
            return calls, stats, row

    async def test_unavailable_retries_at_tail_and_recovers(self):
        calls, stats, row = await self.run_queue(recover=True)
        self.assertEqual(calls, ["first", "second", "first"])
        self.assertEqual(stats["completed"], stats["queued"])
        self.assertEqual(row["lookup_status"], "success")

    async def test_persistent_unavailable_retries_only_once(self):
        calls, stats, row = await self.run_queue()
        self.assertEqual(calls, ["first", "second", "first"])
        self.assertEqual(stats["completed"], 3)
        self.assertEqual(row["lookup_status"], "web_unavailable")
        self.assertEqual(row["follower_count"], "")

    async def test_login_failure_stops_without_tail_retry(self):
        calls, stats, row = await self.run_queue(status="login_required")
        self.assertEqual(calls, ["first"])
        self.assertTrue(stats["stopped"])
