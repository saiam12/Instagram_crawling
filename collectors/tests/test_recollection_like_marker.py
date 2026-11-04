import json
import tempfile
import unittest
from pathlib import Path

from src.reels.android_reel_metrics import AndroidMetricResult, merge_android_metrics
from src.reels.instagram_reels_browser import (
    _android_metric_queue_paths, _write_android_metric_completion,
    android_metric_terminal_line,
)


class RecollectionLikeMarkerTests(unittest.TestCase):
    def test_missing_recollection_like_is_preserved_without_privacy_evidence(self):
        for value in (None, "", "unavailable", 0, 3):
            with self.subTest(value=value):
                result = AndroidMetricResult(recollection=True)
                merged = merge_android_metrics({"like_count": value}, result)
                self.assertEqual(merged["like_count"], value)

    def test_completion_and_terminal_preserve_unavailable_recollection(self):
        result = AndroidMetricResult(status="unavailable", recollection=True)
        with tempfile.TemporaryDirectory() as directory:
            paths = _android_metric_queue_paths(directory)
            paths["working"].mkdir(parents=True)
            working = paths["working"] / "job.json"
            working.write_text("{}")
            _write_android_metric_completion(paths, working, {"recollection": True}, result)
            payload = json.loads((paths["completed"] / "job.json").read_text())
            self.assertNotIn("like_count", payload["metrics"])
        line = android_metric_terminal_line(result, 1, current=1, total=1, reel_url="url")
        self.assertIn("like_count=unavailable", line)

    def test_explicitly_private_recollection_uses_x_in_all_outputs(self):
        result = AndroidMetricResult(recollection=True, like_count_private=True)
        self.assertEqual(merge_android_metrics({"like_count": 0}, result)["like_count"], "X")
        with tempfile.TemporaryDirectory() as directory:
            paths = _android_metric_queue_paths(directory)
            paths["working"].mkdir(parents=True)
            working = paths["working"] / "job.json"
            working.write_text("{}")
            _write_android_metric_completion(paths, working, {"recollection": True}, result)
            payload = json.loads((paths["completed"] / "job.json").read_text())
            self.assertEqual(payload["metrics"]["like_count"], "X")
        self.assertIn("like_count=X", android_metric_terminal_line(result, 1, current=1, total=1, reel_url="url"))

    def test_initial_missing_like_behavior_is_preserved(self):
        self.assertEqual(merge_android_metrics({"like_count": ""}, AndroidMetricResult())["like_count"], "")
