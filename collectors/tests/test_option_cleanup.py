import contextlib
import io
import unittest
from unittest.mock import patch

from src.reels.instagram_reels_browser import build_parser, parse_args, main as browser_main
from scripts.instagram_reels_python import parse_scheduled_command, main as launcher_main


class OptionCleanupTests(unittest.TestCase):
    def test_scheduled_discovery_count_options_are_removed(self):
        config = parse_scheduled_command("fashion", [])
        self.assertFalse(hasattr(config, "new_items_per_window"))
        self.assertFalse(hasattr(config, "max_new_items_per_window"))
        for flag in ("--new-items-per-window", "--max-new-items-per-window"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_scheduled_command("fashion", [flag, "100"])

    def test_hidden_aliases_still_parse_but_help_uses_official_names(self):
        config = parse_scheduled_command("fashion", ["--maxdays", "14", "--shared-data"])
        self.assertEqual(config.max_upload_age_days, 14)
        self.assertTrue(config.base_output)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            parse_scheduled_command("fashion", ["--help"])
        self.assertIn("--collector-mode", output.getvalue())
        for flag in ("--maxdays", "--shared-data", "--direct-reel-info-wait-seconds"):
            self.assertNotIn(flag, output.getvalue())

    def test_browser_mode_and_compatibility_aliases_agree(self):
        for args in (["--collector-mode", "web"], ["--no-android-metrics"]):
            self.assertFalse(parse_args(args).android_metrics)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--collector-mode", "web", "--android-metrics"])
        help_text = build_parser().format_help()
        self.assertIn("--collector-mode", help_text)
        for flag in ("--no-android-metrics", "--android-metrics ", "--direct-concurrency"):
            self.assertNotIn(flag, help_text)

    def test_removed_options_are_rejected(self):
        for args in (["--no-login"], ["--followers-only"], ["--followres-only"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(args)
        self.assertEqual(parse_args([]).follower_interval_seconds, 8)
        self.assertFalse(parse_args([]).no_login)

    def test_follower_interval_remains_configurable(self):
        self.assertEqual(parse_args(["--follower-interval-seconds", "3"]).follower_interval_seconds, 3)
        for value in ("0", "nan", "inf"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(["--follower-interval-seconds", value])

    def test_followers_command_uses_internal_dispatch(self):
        with patch("scripts.instagram_reels_python.collector_main", return_value=0) as collect:
            self.assertEqual(launcher_main(["followers"]), 0)
        self.assertTrue(collect.call_args.kwargs["followers_only"])
        self.assertNotIn("--followers-only", collect.call_args.args[0])
        self.assertIn("--background", collect.call_args.args[0])

    def test_internal_dispatch_sets_follower_mode(self):
        async def run(options):
            self.assertTrue(options.followers_only)
            self.assertEqual(options.follower_interval_seconds, 8)
            return 0
        with patch("src.reels.instagram_reels_browser.run_collector", new=run):
            self.assertEqual(browser_main([], followers_only=True), 0)
