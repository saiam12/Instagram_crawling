from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import reel_analyzer as cli
from instagram.reel_resolver import ReelAccessError, parse_reel_url
from video.frame_sampler import ExtractedFrame, FrameCandidate, build_candidates, frames_for_analysis, limit_candidates
from vl.ollama_client import build_window_ranges, response_text
from vl.parser import QwenResponseError, parse_json_response


class ReelUrlTests(unittest.TestCase):
    def test_parses_supported_reel_urls(self) -> None:
        for url in (
            "https://www.instagram.com/reel/DAbCdEf1234/",
            "https://www.instagram.com/reels/DAbCdEf1234/?utm_source=test",
        ):
            with self.subTest(url=url):
                reference = parse_reel_url(url)
                self.assertEqual(reference.shortcode, "DAbCdEf1234")
                self.assertEqual(reference.url, "https://www.instagram.com/reel/DAbCdEf1234/")

    def test_rejects_non_reel_url(self) -> None:
        with self.assertRaisesRegex(ReelAccessError, "Unsupported URL"):
            parse_reel_url("https://www.instagram.com/p/DAbCdEf1234/")


class SamplingTests(unittest.TestCase):
    def test_minimum_fps_keeps_three_base_frames_per_second(self) -> None:
        candidates = build_candidates(30, min_fps=3)
        base_frames = [item for item in candidates if "minimum_sampling" in item.reasons]
        self.assertEqual(len(base_frames), 90)

    def test_opening_and_scene_reasons_are_merged(self) -> None:
        candidates = build_candidates(10, [1.5, 8.4], min_fps=3)
        by_timestamp = {item.timestamp: item.reasons for item in candidates}
        self.assertIn("opening", by_timestamp[1.5])
        self.assertIn("scene_change", by_timestamp[1.5])
        self.assertIn("scene_change", by_timestamp[8.4])

    def test_frame_limit_uses_priority_then_returns_time_order(self) -> None:
        candidates = [
            FrameCandidate(10.0, {"adaptive_sampling"}),
            FrameCandidate(8.0, {"scene_change"}),
            FrameCandidate(2.0, {"opening"}),
            FrameCandidate(30.0, {"video_end"}),
        ]
        selected = limit_candidates(candidates, max_frames=2)
        self.assertEqual([item.timestamp for item in selected], [2.0, 8.0])

    def test_similar_frames_are_not_sent_to_qwen(self) -> None:
        frames = [
            ExtractedFrame(0.0, ["video_start"], Path("first.jpg")),
            ExtractedFrame(0.333, ["minimum_sampling"], Path("similar.jpg"), similar_to_previous=True, similar_to_timestamp=0.0),
            ExtractedFrame(0.667, ["minimum_sampling"], Path("changed.jpg")),
        ]
        self.assertEqual([frame.timestamp for frame in frames_for_analysis(frames)], [0.0, 0.667])


class ResponseParserTests(unittest.TestCase):
    def test_extracts_json_from_surrounding_text(self) -> None:
        self.assertEqual(parse_json_response("Here it is: {\"content\": {\"summary\": \"demo\"}}"), {"content": {"summary": "demo"}})

    def test_rejects_non_json_response(self) -> None:
        with self.assertRaises(QwenResponseError):
            parse_json_response("No JSON was returned")


class WindowingTests(unittest.TestCase):
    def test_windows_share_the_requested_overlap(self) -> None:
        self.assertEqual(build_window_ranges(10, 4, 1), [(0, 4), (3, 7), (6, 10)])

    def test_invalid_overlap_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_window_ranges(10, 4, 4)

    def test_qwen_thinking_field_is_used_when_content_is_empty(self) -> None:
        self.assertEqual(response_text({"content": "", "thinking": '{"ok":true}'}), '{"ok":true}')


class ResultStorageTests(unittest.TestCase):
    def test_results_are_combined_and_shortcodes_are_updated(self) -> None:
        original_dir, original_file = cli.RESULTS_DIR, cli.RESULTS_FILE
        with TemporaryDirectory() as directory:
            cli.RESULTS_DIR = Path(directory)
            cli.RESULTS_FILE = cli.RESULTS_DIR / "reel_analyses.json"
            try:
                cli.save_result({"shortcode": "first", "content": {"summary": "old"}})
                cli.save_result({"shortcode": "second"})
                path = cli.save_result({"shortcode": "first", "content": {"summary": "new"}})
                records = json.loads(path.read_text(encoding="utf-8"))
            finally:
                cli.RESULTS_DIR, cli.RESULTS_FILE = original_dir, original_file
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]["content"]["summary"], "new")


if __name__ == "__main__":
    unittest.main()
