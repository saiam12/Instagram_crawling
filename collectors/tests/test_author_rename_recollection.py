import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.reels.instagram_follower_enricher import FollowerEnricher
from src.reels.instagram_reels_browser import (
    ProfileReelViewEnricher,
    resolve_pending_reels_via_links,
)


class AuthorRenameTests(unittest.IsolatedAsyncioTestCase):
    def test_find_or_create_allows_account_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lambda p: {})
            enricher.users["id:75854493651"] = {
                "_key": "id:75854493651",
                "user_id": "75854493651",
                "username": "csell.fit",
            }
            enricher.user_id_index["75854493651"] = enricher.users["id:75854493651"]
            enricher.username_index["csell.fit"] = enricher.users["id:75854493651"]

            # User renames from csell.fit to censell.co.kr
            row = enricher._find_or_create("75854493651", "censell.co.kr")
            self.assertEqual(row["username"], "censell.co.kr")
            self.assertEqual(row["user_id"], "75854493651")
            self.assertNotIn("csell.fit", enricher.username_index)
            self.assertIs(enricher.username_index["censell.co.kr"], row)

    async def test_resolve_pending_reels_via_links_detects_rename_and_requeues_android_when_metrics_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            mock_store = MagicMock()
            mock_store.lock = asyncio.Lock()
            mock_store.dirty = False
            saved_row = {
                "url": "https://www.instagram.com/reels/DffA_1ePAgx/",
                "collected_at": "2026-11-09T01:09:38.466Z",
                "user_id": "75854493651",
                "username": "csell.fit",
                "view_count": "",
                "like_count": "",  # Android has not collected yet
                "comment_count": "",
                "saved_count": "",
            }
            mock_store.rows = [saved_row]

            profile_views = ProfileReelViewEnricher(mock_store, android_fallback=True)
            profile_views.track(saved_row)

            mock_page = AsyncMock()
            mock_page.is_closed = MagicMock(return_value=False)

            with patch("src.reels.instagram_reels_browser.navigate_with_retries", new_callable=AsyncMock) as mock_nav, \
                 patch("src.reels.instagram_reels_browser.collect_embedded_reel_metadata", new_callable=AsyncMock) as mock_meta, \
                 patch("src.reels.instagram_reels_browser.enqueue_android_metric_job") as mock_enqueue_metric, \
                 patch("src.reels.instagram_reels_browser.enqueue_android_view_count_job") as mock_enqueue_view, \
                 patch("src.reels.instagram_reels_browser.start_android_metric_worker"):

                async def fake_collect(page, dest, **kwargs):
                    dest["DffA_1ePAgx"] = {
                        "username": "censell.co.kr",
                        "userId": "75854493651",
                        "viewCount": 2500,
                    }
                mock_meta.side_effect = fake_collect

                await resolve_pending_reels_via_links(
                    mock_page,
                    directory,
                    mock_store,
                    profile_views,
                    follower_enricher=None,
                    detached_android_metrics=True,
                    recollection=True,
                )

                # Verified rename: row username updated to censell.co.kr
                self.assertEqual(saved_row["username"], "censell.co.kr")
                # View count updated from reel page
                self.assertEqual(saved_row["view_count"], 2500)
                # Removed from pending
                self.assertNotIn(("https://www.instagram.com/reels/DffA_1ePAgx/", "2026-11-09T01:09:38.466Z"), profile_views.pending)
                # Android metrics were missing, so re-queued to Android with updated username!
                mock_enqueue_metric.assert_called_once()
                self.assertEqual(mock_enqueue_metric.call_args[0][1]["username"], "censell.co.kr")

    async def test_resolve_pending_reels_skips_requeue_when_android_metrics_already_present(self):
        with tempfile.TemporaryDirectory() as directory:
            mock_store = MagicMock()
            mock_store.lock = asyncio.Lock()
            mock_store.dirty = False
            saved_row = {
                "url": "https://www.instagram.com/reels/DffA_1ePAgx/",
                "collected_at": "2026-11-09T01:09:38.466Z",
                "user_id": "75854493651",
                "username": "csell.fit",
                "view_count": "",
                "like_count": 12,  # Android already collected!
                "comment_count": 0,
                "saved_count": 3,
            }
            mock_store.rows = [saved_row]

            profile_views = ProfileReelViewEnricher(mock_store, android_fallback=True)
            profile_views.track(saved_row)

            mock_page = AsyncMock()
            mock_page.is_closed = MagicMock(return_value=False)

            with patch("src.reels.instagram_reels_browser.navigate_with_retries", new_callable=AsyncMock) as mock_nav, \
                 patch("src.reels.instagram_reels_browser.collect_embedded_reel_metadata", new_callable=AsyncMock) as mock_meta, \
                 patch("src.reels.instagram_reels_browser.enqueue_android_metric_job") as mock_enqueue_metric, \
                 patch("src.reels.instagram_reels_browser.enqueue_android_view_count_job") as mock_enqueue_view, \
                 patch("src.reels.instagram_reels_browser.start_android_metric_worker"):

                async def fake_collect(page, dest, **kwargs):
                    dest["DffA_1ePAgx"] = {
                        "username": "censell.co.kr",
                        "userId": "75854493651",
                        "viewCount": 2500,
                    }
                mock_meta.side_effect = fake_collect

                await resolve_pending_reels_via_links(
                    mock_page,
                    directory,
                    mock_store,
                    profile_views,
                    follower_enricher=None,
                    detached_android_metrics=True,
                    recollection=True,
                )

                # Verified rename: row username updated to censell.co.kr
                self.assertEqual(saved_row["username"], "censell.co.kr")
                self.assertEqual(saved_row["view_count"], 2500)
                # Android metrics were already present, so NOT re-queued!
                mock_enqueue_metric.assert_not_called()


if __name__ == "__main__":
    unittest.main()
