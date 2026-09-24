from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from android_collector.diagnostics import CollectorDiagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_writes_jsonl_human_log_and_first_rate_limit_snapshot_without_query_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = CollectorDiagnostics(Path(directory), run_mode="background")
            diagnostics.begin_media()
            diagnostics.update_media(
                current_url="https://www.instagram.com/reel/ABC123/?igsh=secret-value",
                username="creator",
            )
            diagnostics.stage_start("OPEN_REEL")
            diagnostics.stage_success("OPEN_REEL")
            diagnostics.stage_start("READ_LIKE_COUNT")
            diagnostics.record_retry(
                stage="READ_LIKE_COUNT",
                target="likes_and_plays_panel",
                attempt=1,
                total=3,
                reason="ELEMENT_TIMEOUT",
                previous_wait=0.2,
            )
            diagnostics.stage_timeout("READ_LIKE_COUNT", target="likes_and_plays_panel")
            diagnostics.rate_limit_suspected("429 visible in UI")
            diagnostics.finish("failed", error="safe failure")

            events_path = next((Path(directory) / "logs").glob("instagram_events_*.jsonl"))
            log_path = next((Path(directory) / "logs").glob("instagram_collector_*.log"))
            snapshot_path = next((Path(directory) / "diagnostics").glob("rate_limit_*.json"))
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            event_names = {event["event"] for event in events}

            self.assertTrue({"MEDIA_START", "STAGE_START", "STAGE_SUCCESS", "RETRY", "ELEMENT_TIMEOUT", "RATE_LIMIT_SUSPECTED", "COLLECTOR_FINISH"} <= event_names)
            self.assertTrue(all(event["run_mode"] == "background" for event in events))
            self.assertEqual(snapshot["current_shortcode"], "ABC123")
            self.assertEqual(snapshot["network_status"], "unavailable")
            self.assertEqual(snapshot["attempted_media"], 1)
            self.assertNotIn("secret-value", events_path.read_text(encoding="utf-8"))
            self.assertNotIn("secret-value", log_path.read_text(encoding="utf-8"))

    def test_http_429_is_not_claimed_when_only_the_android_ui_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = CollectorDiagnostics(directory, run_mode="foreground")
            diagnostics.rate_limit_suspected("try again later")
            diagnostics.finish("failed")

            events_path = next((Path(directory) / "logs").glob("instagram_events_*.jsonl"))
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            finish = next(event for event in events if event["event"] == "COLLECTOR_FINISH")

            self.assertEqual(finish["http_429_confirmed"], 0)
            self.assertEqual(finish["rate_limit_suspected_count"], 1)


if __name__ == "__main__":
    unittest.main()
