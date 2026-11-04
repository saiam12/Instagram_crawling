from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from src.reels.android_reel_metrics import detect_android_rate_limit_signal
from src.reels.collection_diagnostics import CollectorDiagnostics, cumulative_collector_summaries
from src.reels.instagram_reels_browser import InstagramRateLimitState, append_collection_log, collection_skip_reason


class CollectionDiagnosticsTests(unittest.TestCase):
    def test_summary_shows_collection_rounds_for_mixed_reel_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()) as output:
            diagnostics = CollectorDiagnostics(directory, run_mode="background", component="python")
            diagnostics.begin_media(current_url="https://www.instagram.com/reel/ONE/", collection_number=1)
            diagnostics.finish_media("SUCCESS", success=True)
            diagnostics.begin_media(current_url="https://www.instagram.com/reel/TWO/", collection_number=2)
            diagnostics.finish_media("FAILED", success=False)
            diagnostics.begin_media(current_url="https://www.instagram.com/reel/THREE/")
            diagnostics.update_media(collection_number=3)
            diagnostics.finish_media("SUCCESS", success=True)
            diagnostics.finish("completed")

            self.assertIn("Collection rounds (successful/attempted): #1: 1/1, #2: 0/1, #3: 1/1", output.getvalue())

    def test_supervisor_summary_accumulates_batches_and_separates_rounds(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as directory, patch("src.reels.collection_diagnostics.time.monotonic", side_effect=lambda: now[0]), redirect_stdout(StringIO()) as output:
            with cumulative_collector_summaries():
                for rounds in ([1] * 50, [2] * 12, [2] * 10, [2] * 28 + [3] * 12):
                    diag = CollectorDiagnostics(directory, run_mode="foreground", component="python")
                    for number in rounds:
                        diag.begin_media(current_url="https://www.instagram.com/reel/target/", collection_number=number)
                        diag.finish_media("SUCCESS", success=True)
                    now[0] += 60
                    diag.finish("completed")
                    diag.finish("completed")  # Repeated finish must not add the batch twice.
                self.assertEqual(diag.attempted_media, 112)
                self.assertEqual(diag.success_count, 112)
                self.assertEqual(dict(diag.collection_rounds), {1: 50, 2: 50, 3: 12})
                self.assertEqual(diag._stats()["media_per_min"], 28.0)
                self.assertEqual(diag._stats()["elapsed_seconds"], 240.0)
                third = CollectorDiagnostics(directory, run_mode="foreground", component="python")
                for _ in range(38):
                    third.begin_media(current_url="https://www.instagram.com/reel/third/", collection_number=3)
                    third.finish_media("SUCCESS", success=True)
                now[0] += 60
                third.finish("completed")
                last_summary = output.getvalue().split("========== COLLECTOR SUMMARY ==========")[-1]
                self.assertIn("Collection rounds (successful/attempted): #1: 50/50, #2: 50/50, #3: 50/50", last_summary)
                self.assertIn("Attempted media: 50", last_summary)
                self.assertIn("Round runtime: 00:02:00", last_summary)
                self.assertIn("Program runtime: 00:05:00", last_summary)
                self.assertNotIn("Last successful media:", output.getvalue())
                other = CollectorDiagnostics(Path(directory) / "beauty", run_mode="foreground", component="python")
                other.finish("completed")
                self.assertEqual(other.attempted_media, 0)
            with cumulative_collector_summaries():
                fresh = CollectorDiagnostics(directory, run_mode="foreground", component="python")
                fresh.finish("completed")
                self.assertEqual(fresh.attempted_media, 0)
            self.assertIn("Attempted media: 62", output.getvalue())
            self.assertIn("Attempted media: 22", output.getvalue())
            self.assertIn("Collection rounds (successful/attempted): #1: 50/50, #2: 50/50, #3: 12/12", output.getvalue())

    def test_skip_conditions_are_accumulated_only_for_current_rounds(self):
        reasons = {
            "duplicateInRun": "실행 중 중복",
            "uploadAgeFilteredOut": "maxdays 초과",
            "uploadDateUnavailable": "업로드 날짜 미확인",
            "cooldownSkipped": "재수집 간격 미경과",
        }
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()) as output:
            with cumulative_collector_summaries():
                for _ in range(2):
                    diag = CollectorDiagnostics(directory, run_mode="foreground", component="python")
                    diag.begin_media(collection_number=1)
                    diag.finish_media("SUCCESS", success=True)
                    for key, expected in reasons.items():
                        reason = collection_skip_reason({key: True})
                        self.assertEqual(reason, expected)
                        diag.begin_media(collection_number=1)
                        diag.finish_media("SKIPPED", success=False, count_as_failure=False, skip_reason=reason)
                    diag.begin_media(collection_number=1)
                    diag.finish_media("FAILED", success=False)
                    diag.finish("completed")
                    diag.finish("completed")
                summary = output.getvalue().split("========== COLLECTOR SUMMARY ==========")[-1]
                self.assertIn("#1: 2/12", summary)
                self.assertIn("Successful: 2", summary)
                self.assertIn("Failed: 2", summary)
                self.assertIn("Skipped: 8", summary)
                for reason in reasons.values():
                    self.assertIn(f"  {reason}: 2", summary)
                events = [json.loads(line) for line in diag.events_path.read_text(encoding="utf-8").splitlines()]
                skipped = [event for event in events if event.get("result") == "SKIPPED"]
                self.assertEqual({event["skip_reason"] for event in skipped}, set(reasons.values()))
                next_round = CollectorDiagnostics(directory, run_mode="foreground", component="python")
                next_round.begin_media(collection_number=2)
                next_round.finish_media("SKIPPED", success=False, count_as_failure=False)
                next_round.finish("completed")
                summary = output.getvalue().split("========== COLLECTOR SUMMARY ==========")[-1]
                self.assertIn("#2: 0/1", summary)
                self.assertIn("Skipped: 1", summary)
                self.assertIn("기타(사유 미확인): 1", summary)
                self.assertNotIn("maxdays 초과", summary)
        self.assertEqual(collection_skip_reason(None), "")
        self.assertEqual(collection_skip_reason({"exactMetricUnavailable": True}), "")

    def test_program_runtime_includes_initial_wait_and_other_datasets_and_resets(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as directory, patch("src.reels.collection_diagnostics.time.monotonic", side_effect=lambda: now[0]), redirect_stdout(StringIO()) as output:
            with cumulative_collector_summaries():
                now[0] = 60
                first = CollectorDiagnostics(directory, run_mode="foreground", component="python")
                first.begin_media(collection_number=1)
                first.finish_media("SUCCESS", success=True)
                now[0] = 120
                first.finish("completed")
                now[0] = 180
                other = CollectorDiagnostics(Path(directory) / "beauty", run_mode="foreground", component="python")
                other.begin_media(collection_number=2)
                other.finish_media("SUCCESS", success=True)
                now[0] = 240
                other.finish("completed")
                summary = output.getvalue().split("========== COLLECTOR SUMMARY ==========")[-1]
                self.assertIn("Round runtime: 00:01:00", summary)
                self.assertIn("Program runtime: 00:04:00", summary)
            with cumulative_collector_summaries():
                fresh = CollectorDiagnostics(directory, run_mode="foreground", component="python")
                now[0] = 270
                fresh.finish("completed")
                summary = output.getvalue().split("========== COLLECTOR SUMMARY ==========")[-1]
                self.assertIn("Program runtime: 00:00:30", summary)

    def test_standalone_summary_shows_both_runtimes_without_last_successful_media(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as directory, patch("src.reels.collection_diagnostics.time.monotonic", side_effect=lambda: now[0]), redirect_stdout(StringIO()) as output:
            diag = CollectorDiagnostics(directory, run_mode="foreground", component="android")
            diag.begin_media(collection_number=1)
            diag.finish_media("SUCCESS", success=True)
            now[0] = 90
            diag.finish("completed")
            self.assertIn("Round runtime: 00:01:30", output.getvalue())
            self.assertIn("Program runtime: 00:01:30", output.getvalue())
            self.assertNotIn("Last successful media:", output.getvalue())

    def test_browser_429_writes_confirmed_event_snapshot_and_sanitized_network_history(self) -> None:
        class Request:
            resource_type = "xhr"
            timing = {"startTime": 1_000, "responseEnd": 384}

        class Response:
            url = "https://www.instagram.com/api/graphql?token=secret-value"
            status = 429
            headers = {"retry-after": "120"}
            request = Request()

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            diagnostics = CollectorDiagnostics(directory, run_mode="background", component="python")
            diagnostics.begin_media(current_url="https://www.instagram.com/reel/ABC123/?igsh=private")
            diagnostics.stage_start("READ_LIKE_COUNT")
            state = InstagramRateLimitState(diagnostics=[diagnostics])
            state.observe(Response())
            diagnostics.finish("failed", error="rate limited")

            event_path = next((Path(directory) / "logs").glob("instagram_events_*.jsonl"))
            snapshot_path = next((Path(directory) / "diagnostics").glob("rate_limit_*.json"))
            events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
            confirmed = next(event for event in events if event["event"] == "HTTP_429_CONFIRMED")
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

            self.assertTrue(state.limited)
            self.assertEqual(confirmed["shortcode"], "ABC123")
            self.assertEqual(confirmed["duration_ms"], 384)
            self.assertEqual(snapshot["event"], "HTTP_429_CONFIRMED")
            self.assertEqual(snapshot["network_status"], "available")
            self.assertEqual(snapshot["recent_network"][-1]["path"], "/api/graphql")
            self.assertNotIn("secret-value", event_path.read_text(encoding="utf-8"))
            self.assertNotIn("private", snapshot_path.read_text(encoding="utf-8"))

    def test_android_ui_signal_is_suspected_without_claiming_http_429(self) -> None:
        xml = '<hierarchy><node text="Please wait a few minutes before you try again" /></hierarchy>'
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            diagnostics = CollectorDiagnostics(directory, run_mode="foreground", component="android")
            diagnostics.begin_media(current_url="https://www.instagram.com/reel/UI123/")
            diagnostics.stage_start("WAIT_FOR_RENDER")
            signal = detect_android_rate_limit_signal(xml)
            diagnostics.rate_limit_suspected(signal)
            diagnostics.finish("failed")

            event_path = next((Path(directory) / "logs").glob("instagram_events_*.jsonl"))
            events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
            finish = next(event for event in events if event["event"] == "COLLECTOR_FINISH")

            self.assertIn("Please wait a few minutes", signal)
            self.assertEqual(finish["http_429_confirmed"], 0)
            self.assertEqual(finish["rate_limit_suspected_count"], 1)

    def test_legacy_log_removes_url_query_and_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = append_collection_log(
                directory,
                "python",
                "request_failed",
                url="https://www.instagram.com/reel/SAFE/?igsh=private",
                authorization="Bearer private-token",
                error="request to https://www.instagram.com/api/test?token=private failed",
            )
            log_text = path.read_text(encoding="utf-8")

            self.assertIn("https://www.instagram.com/reel/SAFE/", log_text)
            self.assertIn("https://www.instagram.com/api/test", log_text)
            self.assertNotIn("private", log_text)


if __name__ == "__main__":
    unittest.main()
