import unittest
from unittest.mock import AsyncMock, Mock, patch

from scripts.instagram_reels_python import main as launcher_main

from reels.collector_modes import UNIFIED_ROOT, extract_collector_mode, run_android_collection, translate_android_arguments


class CollectorModeTests(unittest.TestCase):
    def test_extracts_mode_aliases_without_forwarding_them(self) -> None:
        self.assertEqual(
            extract_collector_mode(["--collector-mode", "web", "--background"]),
            ("web", ["--background"]),
        )
        self.assertEqual(
            extract_collector_mode(["--android-only", "--max-items", "3"]),
            ("android", ["--max-items", "3"]),
        )
        self.assertEqual(
            extract_collector_mode(["--no-android-metrics"]),
            ("web", []),
        )

    def test_rejects_conflicting_modes(self) -> None:
        with self.assertRaisesRegex(ValueError, "Conflicting collector modes"):
            extract_collector_mode(["--collector-mode", "web", "--android-only"])

    def test_translates_unified_android_options_and_uses_unified_data_root(self) -> None:
        translated = translate_android_arguments([
            "--android-adb-path",
            "C:/Android/adb.exe",
            "--android-device-id",
            "emulator-5554",
            "--android-ui-delay-seconds",
            "0.25",
        ])
        self.assertEqual(
            translated[:6],
            ["--adb-path", "C:/Android/adb.exe", "--device-id", "emulator-5554", "--interval-seconds", "0.25"],
        )
        self.assertIn("--data-dir", translated)
        self.assertEqual(translated[translated.index("--data-dir") + 1], str(UNIFIED_ROOT / "data_web"))

    def test_android_mode_uses_the_legacy_backend_through_one_entry_point(self) -> None:
        launcher = Mock(main=Mock(return_value=7))
        with patch("reels.collector_modes.importlib.import_module", return_value=launcher):
            result = run_android_collection("fashion", ["--background"])

        self.assertEqual(result, 7)
        launcher.main.assert_called_once_with([
            "fashion",
            "--background",
            "--data-dir",
            str(UNIFIED_ROOT / "data_web"),
        ])

    def test_web_mode_disables_android_for_direct_collection(self) -> None:
        with patch("scripts.instagram_reels_python.collector_main", return_value=0) as collector:
            result = launcher_main(["collect", "--collector-mode", "web"])

        self.assertEqual(result, 0)
        self.assertIn("--no-android-metrics", collector.call_args.args[0])

    def test_android_mode_dispatches_before_the_browser_scheduler(self) -> None:
        with patch("scripts.instagram_reels_python.run_android_collection", return_value=0) as run_android:
            result = launcher_main(["fashion", "--collector-mode", "android", "--background"])

        self.assertEqual(result, 0)
        run_android.assert_called_once_with("fashion", ["--background"])

    def test_web_mode_reaches_the_scheduler_without_android_metrics(self) -> None:
        with patch(
            "scripts.instagram_reels_python.run_fashion_beauty_collection",
            new=AsyncMock(return_value=0),
        ) as scheduler:
            result = launcher_main(["fashion", "--collector-mode", "web"])

        self.assertEqual(result, 0)
        self.assertEqual(scheduler.await_args.args[0].collector_mode, "web")


if __name__ == "__main__":
    unittest.main()
