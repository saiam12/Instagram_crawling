from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from collectors.android_reel_metrics import detect_android_rate_limit_signal
from collectors.collection_diagnostics import CollectorDiagnostics
from collectors.instagram_reels_browser import InstagramRateLimitState, append_collection_log


class CollectionDiagnosticsTests(unittest.TestCase):
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
