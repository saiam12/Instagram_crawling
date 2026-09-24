import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from openpyxl import Workbook

import reel_analyzer
from input_sources import normalize_reel_url, read_reel_urls_from_xlsx


class InputSourceTests(unittest.TestCase):
    def test_normalizes_shortcode_and_supports_single_dash_url_option(self):
        self.assertEqual(
            normalize_reel_url("DcSDbXNCtfd"),
            "https://www.instagram.com/reels/DcSDbXNCtfd/",
        )
        self.assertEqual(reel_analyzer.parse_args(["-url", "DcSDbXNCtfd"]).url, "DcSDbXNCtfd")
        self.assertEqual(normalize_reel_url("not valid!"), "")

    def test_group_size_defaults_to_one_and_accepts_two(self):
        self.assertEqual(reel_analyzer.parse_args([]).group_size, 1)
        self.assertEqual(reel_analyzer.parse_args(["--group-size", "2"]).group_size, 2)

    def test_seed_is_optional_and_accepts_an_integer(self):
        self.assertIsNone(reel_analyzer.parse_args([]).seed)
        self.assertEqual(reel_analyzer.parse_args(["--seed", "42"]).seed, 42)

    def test_reads_url_columns_from_all_sheets_and_removes_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reels.xlsx"
            workbook = Workbook()
            first = workbook.active
            first.append(["caption", "url"])
            first.append(["one", "https://www.instagram.com/reel/AAA/"])
            first.append(["duplicate", "https://www.instagram.com/reel/AAA/"])
            second = workbook.create_sheet("second")
            second.append(["reel_url"])
            second.append(["https://www.instagram.com/reels/BBB/"])
            second.append(["CCC"])
            second.append(["https://example.com/not-a-reel"])
            workbook.save(path)

            self.assertEqual(read_reel_urls_from_xlsx(path), [
                "https://www.instagram.com/reel/AAA/",
                "https://www.instagram.com/reels/BBB/",
                "https://www.instagram.com/reels/CCC/",
            ])

    def test_process_safely_converts_shortcode_before_processing(self):
        with patch.object(reel_analyzer, "process_one") as process:
            self.assertTrue(reel_analyzer.process_safely(object(), "DcSDbXNCtfd"))
        process.assert_called_once_with(
            ANY,
            "https://www.instagram.com/reels/DcSDbXNCtfd/",
        )

    def test_model_option_is_applied_before_pool_creation(self):
        pool = Mock(keys=[("a", "secret")], models=["gemini-3.7-flash"], combos=[object()])
        with patch.object(reel_analyzer, "create_pool", return_value=pool) as create, \
             patch.object(reel_analyzer, "ensure_api_keys"), \
             patch.object(reel_analyzer, "process_safely", return_value=True), \
             patch.dict("os.environ", {}, clear=False):
            reel_analyzer.main([
                "--url", "https://www.instagram.com/reel/AAA/",
                "--model", "gemini-3.7-flash",
            ])
            self.assertEqual(reel_analyzer.os.environ["GEMINI_MODELS"], "gemini-3.7-flash")
            create.assert_called_once_with()

    def test_sync_does_not_require_a_local_api_key(self):
        pool = Mock(keys=[])
        pool.sync_keys.return_value = {
            'vault_added': 0, 'vault_updated': 0, 'owner_renamed': 0,
            'local_added': 2, 'local_updated': 0,
            'added': 6, 'updated': 0, 'log_updated': 0, 'enabled': 0, 'stopped': 0,
        }
        with patch.object(reel_analyzer, 'create_pool', return_value=pool), \
             patch.object(reel_analyzer, 'ensure_api_keys') as ensure, \
             patch('builtins.print'):
            reel_analyzer.main(['--sync-key-pool'])
        ensure.assert_not_called()
        pool.sync_keys.assert_called_once_with()

    def test_interactive_input_continues_while_workers_analyze(self):
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        processed = []
        inputs = iter([
            "https://www.instagram.com/reel/AAA/",
            "https://www.instagram.com/reel/BBB/",
            "quit",
        ])

        def process(pool, url):
            processed.append((pool, url))
            if url.endswith("/AAA/"):
                first_started.set()
                release_first.wait(2)
            else:
                second_started.set()
            return True

        def read_input(_prompt):
            value = next(inputs)
            if value.endswith("/BBB/"):
                self.assertTrue(first_started.wait(2))
            elif value == "quit":
                self.assertTrue(second_started.wait(2))
                release_first.set()
            return value

        pools = [object(), object()]
        with patch("builtins.input", side_effect=read_input), \
             patch.object(reel_analyzer, "process_safely", side_effect=process):
            reel_analyzer.run_interactive(Mock(), pools)

        self.assertEqual({url for _, url in processed}, {
            "https://www.instagram.com/reel/AAA/",
            "https://www.instagram.com/reel/BBB/",
        })
        self.assertEqual({pool for pool, _ in processed}, set(pools))

    def test_interactive_groups_two_urls_into_one_analysis_call(self):
        urls = [
            "https://www.instagram.com/reel/AAA/",
            "https://www.instagram.com/reel/BBB/",
        ]
        inputs = iter([*urls, "quit"])

        with patch("builtins.input", side_effect=lambda _prompt: next(inputs)), \
             patch.object(reel_analyzer, "process_group_safely", return_value=2) as process:
            reel_analyzer.run_interactive(Mock(), [object()], group_size=2)

        process.assert_called_once_with(ANY, urls)


if __name__ == "__main__":
    unittest.main()
