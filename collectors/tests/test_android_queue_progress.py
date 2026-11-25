from src.reels import android_metric_worker as _remediation_android_metric_worker
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from src.reels import instagram_reels_browser as browser


class AndroidQueueProgressTests(unittest.TestCase):
    def enqueue(self, directory, index, run="batch"):
        return browser.enqueue_android_metric_job(
            directory,
            {"url": f"https://www.instagram.com/reels/item{index}/",
             "collected_at": "2026-10-27T00:00:00Z", "username": "author"},
            collection_run_id=run, python_index=index,
        )

    def test_requeued_tenth_job_becomes_twenty_eighth_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = browser._android_metric_queue_paths(directory)
            for index in range(1, 28):
                self.enqueue(directory, index)
            for index in range(1, 29):
                working, job = browser._claim_android_metric_job(paths)
                self.assertEqual(browser.android_reel_progress(paths, job, fallback_current=1),
                                 (index, 27 if index <= 10 else 28))
                if index == 10:
                    browser._requeue_android_metric_job(paths, working, job, "screen unreadable", to_tail=True)
                    self.assertEqual(browser.android_reel_progress(paths, job, fallback_current=1), (10, 28))
                else:
                    working.unlink()
            self.assertEqual(job["target"]["url"], "https://www.instagram.com/reels/item10/")

    def test_due_retry_in_middle_uses_next_attempt_number(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = browser._android_metric_queue_paths(directory)
            browser._write_android_metric_worker_status(paths, reel_attempts_started=9, reel_attempts_total=27)
            first = self.enqueue(directory, 10)
            self.enqueue(directory, 11)
            working, job = browser._claim_android_metric_job(paths)
            browser._requeue_android_metric_job(paths, working, job, "device offline")
            # Temporarily defer the retry so the next ready job can proceed.
            job["next_attempt_at"] = "2099-01-01T00:00:00Z"
            browser.write_json_atomic(paths["pending"] / f"{first}.json", job)
            working, next_job = browser._claim_android_metric_job(paths)
            self.assertEqual(browser.android_reel_progress(paths, next_job, fallback_current=1), (11, 28))
            working.unlink()
            job.pop("next_attempt_at")
            browser.write_json_atomic(paths["pending"] / f"{first}.json", job)
            working, retry = browser._claim_android_metric_job(paths)
            self.assertEqual(browser.android_reel_progress(paths, retry, fallback_current=1), (12, 28))

    def test_new_web_batch_and_worker_restart_do_not_reset_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = browser._android_metric_queue_paths(directory)
            self.enqueue(directory, 14, "first-batch")
            working, job = browser._claim_android_metric_job(paths)
            self.assertEqual(browser.android_reel_progress(paths, job, fallback_current=1), (1, 1))
            working.unlink()
            browser._write_android_metric_worker_status(paths, state="running", processed=0)
            self.enqueue(directory, 1, "second-batch")
            working, job = browser._claim_android_metric_job(paths)
            self.assertEqual(browser.android_reel_progress(paths, job, fallback_current=1), (2, 2))
            self.enqueue(directory, 2, "second-batch")
            self.assertEqual(browser.android_reel_progress(paths, job, fallback_current=1), (2, 3))

    def test_retry_cooldown_does_not_consume_extra_attempt_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = browser._android_metric_queue_paths(directory)
            self.enqueue(directory, 1)
            working, job = browser._claim_android_metric_job(paths)
            now = browser.utc_now()
            job["next_attempt_at"] = browser.isoformat_utc(now + timedelta(seconds=30))
            browser._requeue_android_metric_job(paths, working, job, "screen unreadable", to_tail=True)
            with patch.object(_remediation_android_metric_worker, "utc_now", return_value=now):
                self.assertIsNone(browser._claim_android_metric_job(paths))
                self.assertIsNone(browser._claim_android_metric_job(paths))
            with patch.object(_remediation_android_metric_worker, "utc_now", return_value=now + timedelta(seconds=31)):
                working, retry = browser._claim_android_metric_job(paths)
            self.assertEqual(browser.android_reel_progress(paths, retry, fallback_current=1), (2, 2))

    def test_auxiliary_jobs_are_not_counted_as_reel_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = browser._android_metric_queue_paths(directory)
            self.enqueue(directory, 1)
            browser.enqueue_android_hashtag_post_count_job(directory, ["fashion"])
            working, reel = browser._claim_android_metric_job(paths)
            self.assertEqual(browser.android_reel_progress(paths, reel, fallback_current=1), (1, 1))
            working.unlink()
            working, hashtag = browser._claim_android_metric_job(paths)
            self.assertEqual(hashtag["kind"], "related_hashtag_post_counts")
            self.assertNotIn("android_queue_index", hashtag)
            working.unlink()

    def test_worker_exposes_deferred_reel_in_terminal_log(self):
        with tempfile.TemporaryDirectory() as directory:
            self.enqueue(directory, 10)
            self.enqueue(directory, 11)
            calls = []

            class Enricher:
                def enrich(self, url, **kwargs):
                    calls.append(url)
                    if len(calls) == 1:
                        return browser.AndroidMetricResult(status="unavailable", error="UIAutomator returned an empty or unreadable screen.")
                    return browser.AndroidMetricResult(metrics={"share_count": 0})

            with (patch.object(_remediation_android_metric_worker, "AndroidReelMetricsEnricher", return_value=Enricher()),
                  patch.object(_remediation_android_metric_worker, "ANDROID_UI_RETRY_DELAY_SECONDS", 0)):
                self.assertEqual(browser.run_android_metric_worker(directory, attach_existing=False, idle_seconds=0.01), 0)
            lines = (Path(directory) / ".collector/android.log").read_text(encoding="utf-8").splitlines()
            terminal = [value for line in lines if (value := browser.collection_log_terminal_line("android", line))]
            self.assertEqual(len(terminal), 3)
            self.assertIn("[1/3]", terminal[0])
            self.assertIn("status=requeued", terminal[0])
            self.assertIn("[2/3]", terminal[1])
            self.assertIn("[3/3]", terminal[2])
            status = json.loads(browser._android_metric_queue_paths(directory)["status"].read_text(encoding="utf-8"))
            self.assertEqual(status["reel_attempts_started"], 3)
