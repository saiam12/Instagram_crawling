import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import reel_analyzer
import reel_insights
from pool.sheets import SheetsPoolError


class CliStatusTests(unittest.TestCase):
    def setUp(self):
        self.pool = Mock(keys=[("test", "fake-key")], models=["gemini-3.6-flash"], combos=[object()])
        create = patch.object(reel_analyzer, "create_pool", return_value=self.pool)
        self.create = create.start()
        self.addCleanup(create.stop)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_configuration_failure_returns_nonzero(self):
        self.create.side_effect = RuntimeError("missing configuration")
        self.assertEqual(reel_analyzer.main([]), 1)

    def test_sync_failure_returns_nonzero(self):
        self.pool.sync_keys.side_effect = SheetsPoolError("offline")
        self.assertEqual(reel_analyzer.main(["--sync-key-pool"]), 1)

    def test_sync_in_local_mode_returns_nonzero(self):
        del self.pool.sync_keys
        self.assertEqual(reel_analyzer.main(["--sync-key-pool"]), 1)

    def test_missing_xlsx_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(reel_analyzer.main(["--xlsx", str(Path(directory) / "missing.xlsx")]), 1)

    def test_empty_xlsx_returns_nonzero(self):
        with patch.object(reel_analyzer, "read_reel_urls_from_xlsx", return_value=[]):
            self.assertEqual(reel_analyzer.main(["--xlsx", "empty.xlsx"]), 1)

    def test_partial_xlsx_failure_returns_nonzero(self):
        with patch.object(reel_analyzer, "read_reel_urls_from_xlsx", return_value=["AAA", "BBB"]), \
             patch.object(reel_analyzer, "process_group_safely", side_effect=[1, 0]):
            self.assertEqual(reel_analyzer.main(["--xlsx", "input.xlsx"]), 1)

    def test_successful_xlsx_returns_zero(self):
        with patch.object(reel_analyzer, "read_reel_urls_from_xlsx", return_value=["AAA"]), \
             patch.object(reel_analyzer, "process_group_safely", return_value=1):
            self.assertEqual(reel_analyzer.main(["--xlsx", "input.xlsx"]), 0)

    def test_interactive_failure_returns_nonzero_after_draining_queue(self):
        with patch("builtins.input", side_effect=["AAA", "BBB", "quit"]), \
             patch.object(reel_analyzer, "process_group_safely", side_effect=[0, 1]) as process:
            self.assertEqual(reel_analyzer.main([]), 1)
        self.assertEqual(process.call_count, 2)

    def test_successful_interactive_queue_returns_zero(self):
        with patch("builtins.input", side_effect=["AAA", "quit"]), \
             patch.object(reel_analyzer, "process_group_safely", return_value=1):
            self.assertEqual(reel_analyzer.main([]), 0)


class OutputPathTests(unittest.TestCase):
    def test_custom_output_creates_its_actual_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new" / "nested" / "result.json"
            with patch.object(reel_analyzer, "OUTPUT_FILE", str(output)), \
                 patch.object(reel_analyzer, "WRITE_READABLE_OUTPUT", False):
                reel_analyzer.save_result("AAA", {"summary": "test"})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))[0]["reel_id"], "AAA")


class InsightPoolTests(unittest.TestCase):
    def test_insights_respect_explicit_local_pool(self):
        observed_modes = []
        response = SimpleNamespace(text=json.dumps({
            "videos": [], "common_scene_patterns": [], "common_movement_patterns": [],
            "common_hook_patterns": [], "summary": "test",
        }))
        def create():
            observed_modes.append(os.environ["GEMINI_POOL_MODE"])
            return object()
        with patch.dict(os.environ, {"GEMINI_POOL_MODE": "local"}), \
             patch.object(reel_insights, "create_pool", side_effect=create), \
             patch.object(reel_insights, "_call_with_pool", return_value=response):
            result = reel_insights.analyze_batch([])
        self.assertEqual(result.summary, "test")
        self.assertEqual(observed_modes, ["local"])

    def test_insights_reject_disabled_model_before_pool_creation(self):
        response = SimpleNamespace(text=json.dumps({
            "videos": [], "common_scene_patterns": [], "common_movement_patterns": [],
            "common_hook_patterns": [], "summary": "test",
        }))
        with patch.object(reel_insights, "create_pool") as create, \
             patch.object(reel_insights, "_call_with_pool", return_value=response):
            with self.assertRaises(ValueError):
                reel_insights.analyze_batch([], "gemini-3.7-flash")
        create.assert_not_called()
