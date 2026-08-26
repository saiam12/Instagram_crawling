from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collect_public_reels import (
    CANONICAL_DATA_DIR,
    data_dir_from_arguments,
    prepare_arguments,
    read_initial_reel_urls,
)


class PublicLauncherTests(unittest.TestCase):
    def test_refresh_defaults_to_canonical_history_and_anonymous_runtime(self) -> None:
        arguments, limit = prepare_arguments(["refresh", "--limit", "25", "--background"])

        self.assertEqual(limit, 25)
        self.assertIn("--no-login", arguments)
        self.assertEqual(arguments[arguments.index("--data-dir") + 1], str(CANONICAL_DATA_DIR))

    def test_direct_and_discovery_inputs_are_rejected(self) -> None:
        for arguments in [
            ["--url", "https://www.instagram.com/reel/sampleCode/"],
            ["--urls-file", "urls.txt"],
            ["--hashtag-query", "패션"],
            ["--start-url", "https://www.instagram.com/reels/"],
            ["--output-stem", "another_history"],
            ["--followers-only"],
            ["--followers-after-reels"],
        ]:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, "only recollects URLs"):
                    prepare_arguments(arguments)

    def test_initial_urls_are_read_from_standard_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "reels.csv").write_text(
                "url\n"
                "https://www.instagram.com/reels/first/\n"
                "https://www.instagram.com/reels/second/\n",
                encoding="utf-8",
            )

            self.assertEqual(
                read_initial_reel_urls(data_dir),
                [
                    "https://www.instagram.com/reels/first/",
                    "https://www.instagram.com/reels/second/",
                ],
            )

    def test_reels_workbook_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "reels.xlsx").write_bytes(b"placeholder")
            with mock.patch(
                "collect_public_reels.read_reel_urls_from_xlsx",
                return_value=["https://www.instagram.com/reels/new/"],
            ):
                self.assertEqual(read_initial_reel_urls(data_dir), ["https://www.instagram.com/reels/new/"])

    def test_missing_initial_history_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Run the logged-in Python collector first"):
                read_initial_reel_urls(Path(directory))

    def test_explicit_data_directory_is_used_for_source_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments, _limit = prepare_arguments(["--data-dir", directory])
            self.assertEqual(data_dir_from_arguments(arguments), Path(directory).resolve())


if __name__ == "__main__":
    unittest.main()
