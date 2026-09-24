from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collect_android_reels import FASHION_KEYWORDS, BEAUTY_KEYWORDS, collector_options, main, parse_args


class CliTests(unittest.TestCase):
    @patch("collect_android_reels.run_hashtag", return_value=2)
    @patch("collect_android_reels.create_driver", return_value=object())
    def test_hashtag_command_splits_or_query_and_dispatches(self, _create_driver, run_hashtag) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main([
                "hashtag", "--hashtag", "패션 OR ootd", "--max-items", "2", "--data-dir", directory,
            ])

        self.assertEqual(result, 0)
        options = run_hashtag.call_args.args[0]
        self.assertEqual(options.hashtags, ("패션", "ootd"))
        self.assertEqual(options.max_items, 2)

    @patch("collect_android_reels.run_feed", return_value=1)
    @patch("collect_android_reels.create_driver", return_value=object())
    def test_options_without_command_default_to_new_feed_collection(self, _create_driver, run_feed) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(["--max-items", "1", "--data-dir", directory])

        self.assertEqual(result, 0)
        self.assertEqual(run_feed.call_args.args[0].max_items, 1)

    def test_builtin_domain_keyword_options_need_no_manual_hashtag_query(self) -> None:
        options = parse_args(["collect", "--fashion", "--beauty", "--keywords-per-run", "3"])

        collection = collector_options(options)

        self.assertEqual(collection.hashtags[:3], FASHION_KEYWORDS[:3])
        self.assertIn(BEAUTY_KEYWORDS[0], collection.hashtags)
        self.assertEqual(len(collection.hashtags), 6)

    def test_verbose_progress_option_is_passed_to_collection(self) -> None:
        options = parse_args(["collect", "--verbose-progress"])

        self.assertTrue(collector_options(options).verbose_progress)

    def test_fast_option_uses_xml_evidence_and_same_run_profile_cache(self) -> None:
        options = parse_args(["collect", "--fast"])

        collection = collector_options(options)

        self.assertFalse(collection.capture_screenshots)
        self.assertTrue(collection.reuse_profiles_within_run)

    @patch("collect_android_reels.run_refresh", return_value=2)
    @patch("collect_android_reels.create_driver", return_value=object())
    def test_refresh_dispatches_url_backed_android_recollection(self, _create_driver, run_refresh) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "source.xlsx"
            workbook.touch()
            result = main([
                "refresh", "--max-items", "2", "--data-dir", directory,
                "--input-xlsx", str(workbook), "--start-row", "10", "--end-row", "20",
            ])

        self.assertEqual(result, 0)
        self.assertEqual(run_refresh.call_args.args[0].max_items, 2)
        self.assertEqual(run_refresh.call_args.args[0].hashtags, ())
        self.assertEqual(run_refresh.call_args.kwargs["input_xlsx"], workbook)
        self.assertEqual(run_refresh.call_args.kwargs["start_row"], 10)
        self.assertEqual(run_refresh.call_args.kwargs["end_row"], 20)
        self.assertFalse(hasattr(parse_args(["refresh"]), "new_only"))

    def test_refresh_row_range_requires_both_bounds_in_order(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["refresh", "--start-row", "10"])
        with self.assertRaises(SystemExit):
            parse_args(["refresh", "--start-row", "20", "--end-row", "10"])

    def test_fashion_six_hour_preset_is_new_only(self) -> None:
        options = parse_args(["fashion-beauty", "--six-hour-new-only"])

        self.assertEqual(options.duration_hours, 6.0)
        self.assertEqual(options.new_items_per_window, 250)
        self.assertTrue(options.base_output)

    @patch("collect_android_reels.run_new_only_schedule", return_value=3)
    @patch("collect_android_reels.create_driver", return_value=object())
    def test_fashion_command_dispatches_new_only_schedule(self, _create_driver, run_schedule) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(["fashion", "--duration-hours", "1", "--data-dir", directory])

        self.assertEqual(result, 0)
        self.assertEqual(run_schedule.call_args.args[3], ("fashion",))

    def test_xlsx_command_combines_current_reel_and_user_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            for name, row in (("reels", ["url", "https://www.instagram.com/reel/ABC/"]), ("users", ["username", "creator"])):
                with (data_dir / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow([row[0]])
                    writer.writerow([row[1]])

            result = main(["xlsx", "--data-dir", directory])

            self.assertEqual(result, 0)
            self.assertTrue((data_dir / "instagram_data.xlsx").exists())


if __name__ == "__main__":
    unittest.main()
