import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts.instagram_reels_python import main as launcher_main
from src.exporters.instagram_collector import write_xlsx_workbook

from src.reels.collector_modes import COLLECTOR_MODES, extract_collector_mode


class CollectorModeTests(unittest.TestCase):
    def test_refresh_public_data_directory_reuses_domain_history_and_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            internal = root / ".datasets" / "fashion"
            internal.mkdir(parents=True)
            workbook = root / "reels.xlsx"
            write_xlsx_workbook(workbook, [("reels", [["url"], ["https://www.instagram.com/reels/ONE/"]])])
            (internal / "reels.xlsx").write_bytes(workbook.read_bytes())
            with patch("scripts.instagram_reels_python.collector_main", return_value=0) as collector, patch("scripts.instagram_reels_python.publish_dataset_outputs") as publish:
                self.assertEqual(launcher_main(["refresh", "--data-dir", directory]), 0)
            arguments = collector.call_args.args[0]
            self.assertEqual(arguments[arguments.index("--data-dir") + 1], str(internal))
            self.assertEqual(publish.call_args.args[0].data_root, internal)

    def test_refresh_refuses_a_running_scheduler_in_same_data_directory(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".datasets").mkdir()
            (root / ".datasets" / "fashion_beauty_scheduler.lock.json").write_text(json.dumps({"pid": 123}), encoding="utf-8")
            with patch("scripts.instagram_reels_python.process_is_alive", return_value=True), patch("scripts.instagram_reels_python.collector_main") as collector:
                self.assertEqual(launcher_main(["refresh", "--data-dir", directory]), 1)
            collector.assert_not_called()

    def test_refresh_selects_only_requested_analysis_workbook_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "analysis.xlsx"
            write_xlsx_workbook(workbook, [("reels", [
                ["url"],
                ["https://www.instagram.com/reels/first/"],
                ["https://www.instagram.com/reels/second/"],
                ["https://www.instagram.com/reels/third/"],
            ])])
            selected_urls: list[str] = []

            def inspect_urls(arguments: list[str]) -> int:
                selected_urls.extend(Path(arguments[arguments.index("--urls-file") + 1]).read_text(encoding="utf-8").splitlines())
                return 0

            with patch("scripts.instagram_reels_python.collector_main", side_effect=inspect_urls) as collector:
                result = launcher_main([
                    "refresh", "--input-xlsx", str(workbook), "--start-row", "3", "--end-row", "4",
                    "--data-dir", directory, "--background",
                ])

            self.assertEqual(result, 0)
            self.assertEqual(selected_urls, [
                "https://www.instagram.com/reels/second/",
                "https://www.instagram.com/reels/third/",
            ])
            arguments = collector.call_args.args[0]
            self.assertIn("--urls-file", arguments)
            self.assertNotIn("--input-xlsx", arguments)
            self.assertNotIn("--start-row", arguments)
            self.assertNotIn("--end-row", arguments)
            self.assertIn("--background", arguments)
            self.assertIn("--disable-recollect-cooldown", arguments)
            self.assertEqual(arguments[arguments.index("--max-items") + 1], "2")

    def test_refresh_processes_all_selected_urls_unless_explicitly_limited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "analysis.xlsx"
            write_xlsx_workbook(workbook, [("reels", [
                ["url"],
                *[[f"https://www.instagram.com/reels/target{index}/"] for index in range(60)],
            ])])
            for mode in ("hybrid", "web"):
                for limit in ([], ["--max-items", "3"], ["--max-items=3"]):
                    with self.subTest(mode=mode, limit=limit), patch(
                        "scripts.instagram_reels_python.collector_main", return_value=0,
                    ) as collector:
                        result = launcher_main([
                            "refresh", "--input-xlsx", str(workbook), "--data-dir", directory,
                            "--collector-mode", mode, *limit,
                        ])
                    self.assertEqual(result, 0)
                    arguments = collector.call_args.args[0]
                    self.assertIn("--disable-recollect-cooldown", arguments)
                    if limit:
                        self.assertEqual([arg for arg in arguments if arg.startswith("--max-items")], [limit[0]])
                        for argument in limit:
                            self.assertIn(argument, arguments)
                    else:
                        self.assertEqual(arguments[arguments.index("--max-items") + 1], "60")
                    self.assertEqual("--no-android-metrics" in arguments, mode == "web")
                    self.assertFalse(Path(arguments[arguments.index("--urls-file") + 1]).exists())

    def test_extracts_mode_aliases_without_forwarding_them(self) -> None:
        self.assertEqual(
            extract_collector_mode(["--collector-mode", "web", "--background"]),
            ("web", ["--background"]),
        )
        self.assertEqual(extract_collector_mode(["--android-metrics"]), ("hybrid", []))
        self.assertEqual(extract_collector_mode([]), ("hybrid", []))
        self.assertEqual(
            extract_collector_mode(["--no-android-metrics"]),
            ("web", []),
        )

    def test_rejects_conflicting_modes(self) -> None:
        with self.assertRaisesRegex(ValueError, "Conflicting collector modes"):
            extract_collector_mode(["--collector-mode", "web", "--android-metrics"])

    def test_only_web_and_hybrid_modes_are_supported(self) -> None:
        self.assertEqual(COLLECTOR_MODES, ("hybrid", "web"))
        for arguments in (["--collector-mode", "android"], ["--collector-mode=android"], ["--android-only"]):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "web.*hybrid|hybrid.*web"):
                extract_collector_mode(arguments)

    def test_rejects_missing_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires one of: hybrid, web"):
            extract_collector_mode(["--collector-mode"])

    def test_web_mode_disables_android_for_direct_collection(self) -> None:
        with patch("scripts.instagram_reels_python.collector_main", return_value=0) as collector:
            result = launcher_main(["collect", "--collector-mode", "web"])

        self.assertEqual(result, 0)
        self.assertIn("--no-android-metrics", collector.call_args.args[0])

    def test_android_only_is_rejected_before_collection_or_scheduling(self) -> None:
        for command in ("collect", "fashion"):
            for selector in (["--collector-mode", "android"], ["--collector-mode=android"], ["--android-only"]):
                with self.subTest(command=command, selector=selector), \
                     patch("scripts.instagram_reels_python.collector_main") as collector, \
                     patch("scripts.instagram_reels_python.run_fashion_beauty_collection") as scheduler:
                    self.assertEqual(launcher_main([command, *selector]), 2)
                    collector.assert_not_called()
                    scheduler.assert_not_called()

    def test_default_hybrid_keeps_android_metric_options(self) -> None:
        with patch("scripts.instagram_reels_python.collector_main", return_value=0) as collector:
            result = launcher_main(["collect", "--android-device-id", "emulator-5554", "--android-metrics-required"])
        self.assertEqual(result, 0)
        arguments = collector.call_args.args[0]
        self.assertNotIn("--no-android-metrics", arguments)
        self.assertIn("--android-metrics-required", arguments)
        self.assertEqual(arguments[arguments.index("--android-device-id") + 1], "emulator-5554")

    def test_hybrid_mode_reaches_the_scheduler(self) -> None:
        with patch("scripts.instagram_reels_python.run_fashion_beauty_collection", new=AsyncMock(return_value=0)) as scheduler:
            result = launcher_main(["fashion", "--collector-mode=hybrid"])
        self.assertEqual(result, 0)
        self.assertEqual(scheduler.await_args.args[0].collector_mode, "hybrid")

    def test_web_mode_rejects_android_worker_commands(self) -> None:
        for command in ("android-worker", "hashtag-posts"):
            with self.subTest(command=command), \
                 patch("scripts.instagram_reels_python.run_android_metric_worker") as worker, \
                 patch("scripts.instagram_reels_python.collect_hashtag_count_report") as tags:
                self.assertEqual(launcher_main([command, "--collector-mode", "web"]), 2)
                worker.assert_not_called()
                tags.assert_not_called()

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
