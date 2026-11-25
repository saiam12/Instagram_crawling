import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.reels.android_reel_metrics import AndroidMetricResult
from src.reels.android_metric_worker import (
    _write_android_metric_completion,
    _android_metric_queue_paths,
)
from src.reels.caption_refresh import (
    enqueue_caption_refresh_job,
    list_pending_caption_refresh_jobs,
    complete_caption_refresh_job,
    apply_refreshed_captions_to_history,
    drain_caption_refresh_queue,
)
from src.reels.reel_history import (
    REEL_HISTORY_DIRECTORY,
    REEL_HISTORY_FILENAME,
    write_csv_records,
)


class CaptionRefreshTests(unittest.TestCase):
    def test_enqueue_list_and_complete_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            job_file = enqueue_caption_refresh_job(
                data_dir,
                url="https://www.instagram.com/reels/DfdwSufBOit/",
                shortcode="DfdwSufBOit",
                username="soli_piece",
            )
            self.assertTrue(job_file.exists())
            jobs = list_pending_caption_refresh_jobs(data_dir)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["shortcode"], "DfdwSufBOit")
            self.assertEqual(jobs[0]["username"], "soli_piece")

            complete_caption_refresh_job(data_dir, "DfdwSufBOit")
            self.assertFalse(job_file.exists())
            self.assertEqual(list_pending_caption_refresh_jobs(data_dir), [])

    def test_android_completion_includes_caption_modified(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _android_metric_queue_paths(temp_dir)
            working_path = paths["working"] / "test_job.json"
            working_path.parent.mkdir(parents=True, exist_ok=True)
            working_path.write_text("{}", encoding="utf-8")
            result = AndroidMetricResult(
                status="collected",
                identity_author_only=True,
                caption_modified=True,
                observed_username="soli_piece",
            )
            job = {"job_id": "test_job", "target": {"url": "https://www.instagram.com/reels/DfdwSufBOit/"}}
            _write_android_metric_completion(paths, working_path, job, result)
            saved = json.loads((paths["root"] / "hashtag_identity_completed" / "test_job.json").read_text(encoding="utf-8"))
            self.assertTrue(saved.get("caption_modified"))

    def test_apply_refreshed_captions_to_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            history_dir = data_dir / REEL_HISTORY_DIRECTORY
            history_dir.mkdir(parents=True)
            history_path = history_dir / REEL_HISTORY_FILENAME
            fields = ["url", "title", "hashtags", "username", "collected_at"]
            rows = [
                {
                    "url": "https://www.instagram.com/reels/DfdwSufBOit/",
                    "title": "구 캡션",
                    "hashtags": "#구태그",
                    "username": "soli_piece",
                    "collected_at": "2026-11-08T12:00:00Z",
                }
            ]
            write_csv_records(history_path, rows, fields)

            updates = {
                "DfdwSufBOit": {
                    "title": "신규 수정 캡션입니다",
                    "hashtags": "#신규태그 #패션",
                }
            }
            updated = apply_refreshed_captions_to_history(data_dir, updates)
            self.assertEqual(updated, 1)

            with open(history_path, "r", encoding="utf-8-sig") as f:
                import csv
                reader = list(csv.DictReader(f))
                self.assertEqual(reader[0]["title"], "신규 수정 캡션입니다")
                self.assertEqual(reader[0]["hashtags"], "#신규태그 #패션")

    def test_drain_caption_refresh_queue_separates_hashtags_and_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            history_dir = data_dir / REEL_HISTORY_DIRECTORY
            history_dir.mkdir(parents=True)
            history_path = history_dir / REEL_HISTORY_FILENAME
            fields = ["url", "title", "hashtags", "username", "collected_at"]
            rows = [
                {
                    "url": "https://www.instagram.com/reels/DfdwSufBOit/",
                    "title": "오래된 제목",
                    "hashtags": "#이전태그",
                    "username": "soli_piece",
                    "collected_at": "2026-11-08T12:00:00Z",
                }
            ]
            write_csv_records(history_path, rows, fields)

            enqueue_caption_refresh_job(
                data_dir,
                url="https://www.instagram.com/reels/DfdwSufBOit/",
                shortcode="DfdwSufBOit",
                username="soli_piece",
            )

            # Mock Web direct API response with hashtags mixed at start, middle, and end
            raw_caption = (
                "#데일리룩 코디정보는 프로필 아래 링크에 있어요!🍂🤎\n"
                "#가을코디 이번 2025 F/W 신상 모음 #OOTD"
            )
            mock_page = object()
            with patch(
                "src.reels.instagram_reels_browser.request_reel_info_metadata",
                new=AsyncMock(return_value={"caption": raw_caption}),
            ):
                drained = asyncio.run(drain_caption_refresh_queue(mock_page, data_dir))
                self.assertEqual(drained, 1)

            # Confirm pending queue is empty
            self.assertEqual(list_pending_caption_refresh_jobs(data_dir), [])

            # Confirm history updated with cleanly separated title and hashtags
            with open(history_path, "r", encoding="utf-8-sig") as f:
                import csv
                reader = list(csv.DictReader(f))
                self.assertEqual(
                    reader[0]["title"],
                    "코디정보는 프로필 아래 링크에 있어요!🍂🤎 이번 2025 F/W 신상 모음",
                )
                self.assertEqual(
                    reader[0]["hashtags"],
                    "#데일리룩 #가을코디 #OOTD",
                )

    def test_drain_queue_fallback_to_method_b_on_429(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            history_dir = data_dir / REEL_HISTORY_DIRECTORY
            history_dir.mkdir(parents=True)
            history_path = history_dir / REEL_HISTORY_FILENAME
            fields = ["url", "title", "hashtags", "username", "collected_at"]
            rows = [
                {
                    "url": "https://www.instagram.com/reels/fallback_test/",
                    "title": "구 캡션",
                    "hashtags": "#구태그",
                    "username": "tester",
                    "collected_at": "2026-11-08T12:00:00Z",
                }
            ]
            write_csv_records(history_path, rows, fields)
            enqueue_caption_refresh_job(
                data_dir,
                url="https://www.instagram.com/reels/fallback_test/",
                shortcode="fallback_test",
                username="tester",
            )

            # Method A returns 429
            async def mock_method_a(page, shortcode, diagnostic=None):
                if diagnostic is not None:
                    diagnostic.update({"status": "http_error", "error": "Direct Reel info returned HTTP 429."})
                return {}

            # Method B (page navigation) succeeds with updated caption
            async def mock_method_b(page, shortcode):
                return {"caption": "B방식으로 수집된 새 본문입니다! #폴백성공 #패션"}

            mock_page = object()
            with patch(
                "src.reels.instagram_reels_browser.request_reel_info_metadata",
                new=AsyncMock(side_effect=mock_method_a),
            ), patch(
                "src.reels.caption_refresh.fetch_caption_via_page_navigation",
                new=AsyncMock(side_effect=mock_method_b),
            ) as mocked_b:
                drained = asyncio.run(drain_caption_refresh_queue(mock_page, data_dir, cooldown_seconds=0))
                self.assertEqual(drained, 1)
                mocked_b.assert_awaited_once_with(mock_page, "fallback_test")

            # Queue should be empty and history updated
            self.assertEqual(list_pending_caption_refresh_jobs(data_dir), [])
            with open(history_path, "r", encoding="utf-8-sig") as f:
                import csv
                reader = list(csv.DictReader(f))
                self.assertEqual(reader[0]["title"], "B방식으로 수집된 새 본문입니다!")
                self.assertEqual(reader[0]["hashtags"], "#폴백성공 #패션")

    def test_drain_queue_rate_limit_halts_queue_and_preserves_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            enqueue_caption_refresh_job(data_dir, url="https://instagr.am/p/job1", shortcode="job1")
            enqueue_caption_refresh_job(data_dir, url="https://instagr.am/p/job2", shortcode="job2")

            async def mock_request(page, shortcode, diagnostic=None):
                if diagnostic is not None:
                    diagnostic.update({"status": "http_error", "error": "Direct Reel info returned HTTP 429."})
                return {}

            mock_page = object()
            with patch(
                "src.reels.instagram_reels_browser.request_reel_info_metadata",
                new=AsyncMock(side_effect=mock_request),
            ) as mocked_req, patch(
                "src.reels.caption_refresh.fetch_caption_via_page_navigation",
                new=AsyncMock(return_value={"rate_limited": True}),
            ):
                drained = asyncio.run(drain_caption_refresh_queue(mock_page, data_dir, cooldown_seconds=0))
                self.assertEqual(drained, 0)
                # Ensure it halted immediately after job1 and did NOT call job2 to protect session
                self.assertEqual(mocked_req.call_count, 1)

            # Both jobs should still be pending in queue
            jobs = list_pending_caption_refresh_jobs(data_dir)
            self.assertEqual(len(jobs), 2)

    def test_drain_queue_404_archives_to_failed_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            enqueue_caption_refresh_job(data_dir, url="https://instagr.am/p/deleted_reel", shortcode="deleted_reel")

            async def mock_request(page, shortcode, diagnostic=None):
                if diagnostic is not None:
                    diagnostic.update({"status": "http_error", "error": "Direct Reel info returned HTTP 404."})
                return {}

            mock_page = object()
            with patch(
                "src.reels.instagram_reels_browser.request_reel_info_metadata",
                new=AsyncMock(side_effect=mock_request),
            ):
                drained = asyncio.run(drain_caption_refresh_queue(mock_page, data_dir))
                self.assertEqual(drained, 0)

            # Pending queue should be empty, and failed directory should have the archived job
            self.assertEqual(list_pending_caption_refresh_jobs(data_dir), [])
            failed_dir = data_dir / ".collector" / "android_metric_queue" / "caption_refresh_failed"
            failed_file = failed_dir / "deleted_reel.json"
            self.assertTrue(failed_file.exists())
            saved = json.loads(failed_file.read_text(encoding="utf-8"))
            self.assertEqual(saved.get("failure_reason"), "not_found")


if __name__ == "__main__":
    unittest.main()
