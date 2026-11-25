import asyncio
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.reels import instagram_reels_browser as browser


class IndependentRecollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_android_jobs_exist_before_any_profile_lookup(self):
        for android_first in (True, False):
            with self.subTest(android_first=android_first), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                csv_path = data_dir / browser.REEL_HISTORY_DIRECTORY / browser.REEL_HISTORY_FILENAME
                store = await browser.LongReelStore.create(csv_path, 1, "long", True)
                urls = [f"https://www.instagram.com/reels/{code}/" for code in ("target", "second")]
                for url in urls:
                    await store.append({"url": url, "collected_at": "2026-09-01T00:00:00Z",
                                        "username": "author", "user_id": "1", "follower_count": 10})
                await store.flush()
                urls_file = data_dir / "urls.txt"
                urls_file.write_text("\n".join(urls), encoding="utf-8")
                options = browser.parse_args(["--data-dir", str(data_dir), "--urls-file", str(urls_file),
                                              "--background", "--disable-recollect-cooldown"])
                options.relay_detached_android_logs = False
                paths = browser._android_metric_queue_paths(data_dir)
                launched = []
                def worker(*args, **kwargs):
                    jobs = list(paths["pending"].glob("*.json"))
                    self.assertEqual(len(jobs), 2)
                    for job_path in jobs:
                        job = browser._read_android_metric_job(job_path)
                        self.assertEqual(browser.android_reel_progress(paths, job, fallback_current=1)[1], 2)
                    launched.extend(jobs)
                    if android_first:
                        complete()
                def complete():
                    while claimed := browser._claim_android_metric_job(paths):
                        working, job = claimed
                        browser._write_android_metric_completion(paths, working, job,
                            browser.AndroidMetricResult(metrics={"like_count": 7}, recollection=True,
                                observed_reel_url=job["target"]["url"], observed_username="author"))
                async def profile_lookup(*args, **kwargs):
                    self.assertEqual(len(launched), 2)
                    if not android_first and list(paths["pending"].glob("*.json")):
                        self.assertEqual(len(list(paths["pending"].glob("*.json"))), 2)
                        complete()
                    # Missing views must not prevent Android from processing both Reels.
                    profile = {"status": "success", "userId": "1", "username": "author",
                               "followerCount": 961, "sourceField": "profile_header_text",
                               "accountCountry": "South Korea",
                               "_reel_metadata": {code: {"videoDurationSeconds": 10} for code in ("target", "second")}}
                    if on_profile := kwargs.get("on_profile"):
                        await on_profile(profile)
                    if on_metadata_complete := kwargs.get("on_metadata_complete"):
                        await on_metadata_complete(profile)
                    return profile
                page = SimpleNamespace(url="https://www.instagram.com/", close=AsyncMock(),
                                       context=MagicMock(), is_closed=lambda: False)
                runtime = SimpleNamespace(page=page, browser=SimpleNamespace(close=AsyncMock()))
                context = MagicMock()
                # View-helper retries have their own suite; isolate metric-job ordering here.
                with patch.object(browser, "create_collection_page", AsyncMock(return_value=page)) as create_page, \
                     patch.object(browser, "create_background_follower_runtime", AsyncMock(return_value=runtime)), \
                     patch.object(browser, "request_web_follower_count", AsyncMock(side_effect=profile_lookup)), \
                     patch.object(browser, "read_profile_reel_view_counts", AsyncMock(return_value={})), \
                     patch.object(browser, "enqueue_android_view_count_job", return_value=None), \
                     patch.object(browser, "start_android_metric_worker", side_effect=worker) as launch, \
                     patch.object(browser, "locate_browser_executable", return_value=None), \
                     patch.object(browser, "navigate_with_retries", AsyncMock()) as detail, \
                     redirect_stdout(StringIO()):
                    result = await asyncio.wait_for(browser._run_collector_once(
                        options, register_signal_handler=False, shared_context=context,
                        shared_playwright_runtime=SimpleNamespace(chromium=object()), enable_stop_input=False), 15)
                self.assertEqual(result, 0)
                create_page.assert_not_awaited()
                launch.assert_called_once()
                detail.assert_not_awaited()
                _, rows = browser.read_csv_objects(csv_path)
                current = [row for row in rows if row["collected_at"] != "2026-09-01T00:00:00Z"]
                self.assertEqual(len(current), 2)
                self.assertTrue(all(str(row["like_count"]) == "7" for row in current))
                self.assertTrue(all(str(row["follower_count"]) == "961" for row in current))
