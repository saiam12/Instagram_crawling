import unittest
import tempfile
import json
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import instagram_reels_python as launcher
from src.reels.fashion_beauty_scheduler import DatasetConfig


class RefreshScheduleTests(unittest.TestCase):
    def test_explicit_duration_stops_idle_watch_but_zero_keeps_watching(self):
        from types import SimpleNamespace
        for hours in (1 / 3600, 0):
            current = [datetime(2026, 10, 30, tzinfo=timezone.utc)]
            class Clock:
                @staticmethod
                def now(tz):
                    return current[0]
            sleeps = []
            def sleep(seconds):
                sleeps.append(seconds)
                current[0] += timedelta(seconds=seconds)
                if len(sleeps) > 1:
                    raise KeyboardInterrupt
            inbox = SimpleNamespace(drain=lambda: [], cancelled_urls=lambda: set(), update_workload=lambda *args: None)
            with tempfile.TemporaryDirectory() as directory, patch.object(launcher, 'datetime', Clock), patch.object(launcher, 'read_history', return_value=[]), patch.object(launcher.time, 'sleep', side_effect=sleep), patch.object(launcher, 'collector_main') as collect:
                call = lambda: launcher.refresh_until_collection_count(
                    DatasetConfig('fashion', Path(directory), (), base_output=True), [], [],
                    Path(directory) / 'urls.txt', None, 4, False, inbox=inbox, duration_hours=hours)
                if hours:
                    self.assertEqual(call(), 0)
                    self.assertEqual(len(sleeps), 1)
                else:
                    with self.assertRaises(KeyboardInterrupt):
                        call()
                collect.assert_not_called()

    def test_runtime_records_real_settings_and_terminal_time(self):
        with tempfile.TemporaryDirectory() as directory:
            options = SimpleNamespace(interval_hours=4, age_basis='first-collected',
                                      schedule_basis='last-collected', test=False)
            path = Path(directory) / '.collector/refresh_status.json'
            with launcher.refresh_runtime_record(directory, options, (1, 3, 6, 12)):
                running = json.loads(path.read_text())
                self.assertEqual(running['state'], 'running')
                self.assertIsNone(running['finished_at'])
                self.assertEqual(running['age_intervals'], [1, 3, 6, 12])
                self.assertEqual(running['duration_hours'], 0)
                self.assertIsNone(running['ends_at'])
            ended = json.loads(path.read_text())
            self.assertEqual(ended['state'], 'completed')
            self.assertTrue(ended['finished_at'])
            self.assertEqual(ended['started_at'], running['started_at'])

    def test_runtime_records_interruption_and_keeps_fixed_test_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            options = SimpleNamespace(interval_hours=5/60, age_basis='first-collected',
                                      schedule_basis='last-collected', test=True)
            with self.assertRaises(KeyboardInterrupt):
                with launcher.refresh_runtime_record(directory, options, None):
                    raise KeyboardInterrupt
            ended = json.loads((Path(directory) / '.collector/refresh_status.json').read_text())
            self.assertEqual(ended['state'], 'stopped')
            self.assertTrue(ended['finished_at'])
            self.assertIsNone(ended['age_intervals'])
            self.assertEqual(ended['interval_hours'], 5/60)

    def test_first_collected_basis_uses_earliest_history_not_upload_or_restart(self):
        now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        urls = [f"https://www.instagram.com/reels/{name}/" for name in ("recent", "expired")]
        rows = [
            {"url": urls[0], "uploaded_at": "2026-09-01T00:00:00Z", "collected_at": (now - timedelta(hours=7)).isoformat(), "collection_number": 2},
            {"url": urls[0], "collected_at": (now - timedelta(hours=100)).isoformat()},
            {"url": urls[1], "collected_at": (now - timedelta(hours=200)).isoformat()},
            {"url": urls[1], "collected_at": (now - timedelta(hours=7)).isoformat(), "collection_number": 2},
        ]
        batches = []

        class Clock:
            @staticmethod
            def now(tz):
                return now

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def collect(arguments):
                batches.append((root / "urls.txt").read_text(encoding="utf-8").splitlines())
                rows.append(dict(rows[0], collected_at=now.isoformat(), collection_number=3))
                return 0

            with patch.object(launcher, "datetime", Clock), patch.object(launcher, "read_history", side_effect=lambda _: list(rows)), patch.object(launcher, "collector_main", side_effect=collect), patch.object(launcher.time, "sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    launcher.refresh_until_collection_count(DatasetConfig("fashion", root, ()), urls, [], root / "urls.txt", None, 4, False, (3, 3, 6, 12), age_basis="first-collected", schedule_basis="last-collected")
        self.assertEqual(batches, [[urls[0]]])

    def test_watch_checks_during_wait_and_respects_excel_row_range(self):
        from src.exporters.instagram_collector import write_xlsx_workbook
        now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        urls = [f"https://www.instagram.com/reels/{name}/" for name in ("waiting", "added", "excluded")]
        rows = [{"url": url, "uploaded_at": (now - timedelta(hours=100)).isoformat(), "collected_at": (now - timedelta(hours=7 if index else 0)).isoformat()} for index, url in enumerate(urls)]
        batches = []
        sleeps = []

        class Clock:
            @staticmethod
            def now(tz):
                return now

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workbook = root / "input.xlsx"
            write_xlsx_workbook(workbook, [("reels", [["url"], [urls[0]]])])

            def sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) == 1:
                    write_xlsx_workbook(workbook, [("reels", [["url"], *[[url] for url in urls]])])
                else:
                    raise KeyboardInterrupt

            def collect(arguments):
                batches.append((root / "urls.txt").read_text(encoding="utf-8").splitlines())
                rows.append(dict(rows[1], collected_at=now.isoformat(), collection_number=2))
                return 0

            with patch.object(launcher, "datetime", Clock), patch.object(launcher, "read_history", side_effect=lambda _: list(rows)), patch.object(launcher.time, "sleep", side_effect=sleep), patch.object(launcher, "collector_main", side_effect=collect):
                with self.assertRaises(KeyboardInterrupt):
                    launcher.refresh_until_collection_count(DatasetConfig("fashion", root, ()), urls[:1], [], root / "urls.txt", None, 4, False, (3, 3, 6, 12), workbook=workbook, start_row=2, end_row=3, schedule_basis="last-collected")
        self.assertEqual(batches, [[urls[1]]])
        self.assertEqual(sleeps, [30.0, 30.0])

    def test_watch_retries_unreadable_excel_without_collecting(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, "read_reel_urls_from_xlsx", side_effect=zipfile.BadZipFile("save in progress")), patch.object(launcher.time, "sleep", side_effect=KeyboardInterrupt) as sleep, patch.object(launcher, "collector_main") as collect:
            root = Path(directory)
            with self.assertRaises(KeyboardInterrupt):
                launcher.refresh_until_collection_count(DatasetConfig("fashion", root, ()), [], [], root / "urls.txt", None, 4, False, (3, 3, 6, 12), workbook=root / "input.xlsx", schedule_basis="last-collected")
        sleep.assert_called_once_with(30.0)
        collect.assert_not_called()

    def test_watch_finds_saved_new_rows_while_idle_and_bootstraps_unknown_url(self):
        from src.exporters.instagram_collector import write_xlsx_workbook
        now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        old = "https://www.instagram.com/reels/old/"
        new = "https://www.instagram.com/reels/new/"
        rows = [{"url": old, "uploaded_at": (now - timedelta(days=8)).isoformat(), "collected_at": now.isoformat()}]
        sleeps = []
        batches = []

        class Clock:
            @staticmethod
            def now(tz):
                return now

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workbook = root / "input.xlsx"
            write_xlsx_workbook(workbook, [("reels", [["url"], [old]])])

            def sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) == 1:
                    write_xlsx_workbook(workbook, [("reels", [["url"], [old], [new]])])
                else:
                    raise KeyboardInterrupt

            def collect(arguments):
                batches.append((root / "urls.txt").read_text(encoding="utf-8").splitlines())
                self.assertIn("1", arguments)
                rows.append({"url": new, "uploaded_at": now.isoformat(), "collected_at": now.isoformat()})
                return 0

            with patch.object(launcher, "datetime", Clock), patch.object(launcher, "read_history", side_effect=lambda _: list(rows)), patch.object(launcher.time, "sleep", side_effect=sleep), patch.object(launcher, "collector_main", side_effect=collect):
                with self.assertRaises(KeyboardInterrupt):
                    launcher.refresh_until_collection_count(DatasetConfig("fashion", root, ()), [old], [], root / "urls.txt", None, 4, False, (3, 3, 6, 12), workbook=workbook, schedule_basis="last-collected")
            self.assertEqual(batches, [[new]])
            self.assertEqual(sleeps, [30.0, 30.0])

    def test_refresh_export_preserves_watched_input(self):
        import subprocess
        import sys
        from src.exporters.instagram_collector import write_xlsx_workbook, protect_refresh_workbook, read_reel_urls_from_xlsx
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "reels.xlsx"
            original = [("reels", [["url"], ["https://www.instagram.com/reels/manual/"]])]
            write_xlsx_workbook(workbook, original)
            with protect_refresh_workbook(workbook):
                write_xlsx_workbook(workbook, [("reels", [["url"], ["https://www.instagram.com/reels/result/"]])])
                subprocess.run([sys.executable, "-B", "-c", "from pathlib import Path; import sys; from src.exporters.instagram_collector import write_xlsx_workbook; write_xlsx_workbook(Path(sys.argv[1]), [('reels', [['url'], ['https://www.instagram.com/reels/result/']])])", str(workbook)], cwd=launcher.PROJECT_ROOT, check=True)
            self.assertEqual(read_reel_urls_from_xlsx(workbook), ["https://www.instagram.com/reels/manual/"])
            self.assertEqual(read_reel_urls_from_xlsx(workbook.with_stem("reels_updated")), ["https://www.instagram.com/reels/result/"])
            write_xlsx_workbook(workbook, original)

    def test_cli_passes_age_options_and_keeps_one_shot_refresh(self):
        from src.exporters.instagram_collector import write_xlsx_workbook
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_xlsx_workbook(root / "reels.xlsx", [("reels", [["url"], ["https://www.instagram.com/reels/A/"]])])
            with patch.object(launcher, "refresh_until_collection_count", return_value=0) as scheduled, patch.object(launcher, "collector_main", return_value=0) as collect:
                self.assertEqual(launcher.main(["refresh", "--data-dir", str(root), "--age-schedule", "--middle-interval-hours", "2.5"]), 0)
                self.assertIsNone(scheduled.call_args.args[4])
                self.assertEqual(scheduled.call_args.args[-1], (2, 2.5, 6, 12))
                self.assertEqual(scheduled.call_args.kwargs["workbook"], root / "reels.xlsx")
                self.assertEqual(scheduled.call_args.kwargs["age_basis"], "uploaded")
                collect.assert_not_called()
                self.assertEqual(launcher.main(["refresh", "--data-dir", str(root), "--age-schedule", "--age-basis", "first-collected"]), 0)
                self.assertEqual(scheduled.call_args.kwargs["age_basis"], "first-collected")
                scheduled.reset_mock()
                self.assertEqual(launcher.main(["refresh", "--data-dir", str(root)]), 0)
                scheduled.assert_not_called()
                collect.assert_called_once()

    def test_age_schedule_boundaries_and_custom_intervals(self):
        uploaded = datetime(2026, 10, 1, tzinfo=timezone.utc)
        for age, interval in [(0, 3), (24, 3), (96, 6), (120, 12)]:
            now = uploaded + timedelta(hours=age)
            due, wake = launcher.refresh_age_timing(uploaded, now, now, (3, 3, 6, 12), schedule_basis="last-collected")
            self.assertEqual(due, now + timedelta(hours=interval))
        now = uploaded + timedelta(hours=168)
        self.assertIsNone(launcher.refresh_age_timing(uploaded, now, now, (3, 3, 6, 12), schedule_basis="last-collected"))
        now = uploaded + timedelta(hours=95)
        due, wake = launcher.refresh_age_timing(uploaded, now, now, (2, 4, 8, 12), schedule_basis="last-collected")
        self.assertEqual(due, now + timedelta(hours=4))
        self.assertEqual(wake, uploaded + timedelta(hours=96))
        now = uploaded + timedelta(hours=96)
        due, _ = launcher.refresh_age_timing(uploaded, now - timedelta(hours=1), now, (2, 4, 8, 12), schedule_basis="last-collected")
        self.assertEqual(due, now + timedelta(hours=7))

    def test_age_schedule_runs_past_four_collections_then_expires(self):
        uploaded = datetime(2026, 10, 1, tzinfo=timezone.utc)
        now = uploaded + timedelta(hours=119)
        url = "https://www.instagram.com/reels/A/"
        rows = [{"url": url, "uploaded_at": uploaded.isoformat(), "collected_at": now.isoformat(), "collection_number": 20}]
        batches = []

        class Clock:
            @staticmethod
            def now(tz):
                return now

        def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)

        def collect(arguments):
            batches.append(now)
            rows.append(dict(rows[-1], collected_at=now.isoformat(), collection_number=20 + len(batches)))
            return 0

        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, "read_history", side_effect=lambda _: list(rows)), patch.object(launcher, "datetime", Clock), patch.object(launcher.time, "sleep", side_effect=sleep), patch.object(launcher, "collector_main", side_effect=collect):
            self.assertEqual(launcher.refresh_until_collection_count(DatasetConfig("fashion", Path(directory), ()), [url], [], Path(directory) / "urls.txt", None, 4, False, (3, 3, 6, 12), schedule_basis="last-collected"), 0)
        self.assertEqual(batches, [uploaded + timedelta(hours=hour) for hour in (131, 143, 155, 167)])

    def test_age_schedule_rejects_missing_upload_time(self):
        url = "https://www.instagram.com/reels/A/"
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, "read_history", return_value=[]), patch.object(launcher, "collector_main") as collect:
            with self.assertRaisesRegex(ValueError, "uploaded_at"):
                launcher.refresh_until_collection_count(DatasetConfig("fashion", Path(directory), ()), [url], [], Path(directory) / "urls.txt", None, 4, False, (3, 3, 6, 12), schedule_basis="last-collected")
        collect.assert_not_called()

    def test_each_reel_stops_at_its_total_count_and_uses_last_collection_time(self):
        now = datetime(2026, 10, 4, 0, tzinfo=timezone.utc)
        initial = now
        urls = [f"https://www.instagram.com/reels/{name}/" for name in ("A", "B", "C", "D")]
        rows = [{"url": url, "collection_number": count, "collected_at": (now - timedelta(hours=5)).isoformat()} for url, count in zip(urls, (2, 3, 4))]
        rows.append({"url": urls[3], "collection_number": 3, "collected_at": (now - timedelta(hours=3, minutes=30)).isoformat()})
        batches = []

        class Clock:
            @staticmethod
            def now(tz):
                return now

        def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)

        def collect(arguments):
            selected = urls_file.read_text(encoding="utf-8").splitlines()
            batches.append((now, selected))
            state = launcher.refresh_url_state(rows, urls)
            for url in selected:
                rows.append({"url": url, "collection_number": state[url][0] + 1, "collected_at": now.isoformat()})
            return 0

        with tempfile.TemporaryDirectory() as directory:
            dataset = DatasetConfig("fashion", Path(directory), ())
            urls_file = Path(directory) / "urls.txt"
            with patch.object(launcher, "read_history", side_effect=lambda _: list(rows)), patch.object(launcher, "collector_main", side_effect=collect), patch.object(launcher, "datetime", Clock), patch.object(launcher.time, "sleep", side_effect=sleep), patch.object(launcher, "publish_dataset_outputs") as publish:
                self.assertEqual(launcher.refresh_until_collection_count(dataset, urls, [], urls_file, 4, 4, True, schedule_basis="last-collected"), 0)
            self.assertEqual(batches, [(initial, urls[:2]), (initial + timedelta(minutes=30), urls[3:]), (initial + timedelta(hours=4), urls[:1])])
            self.assertEqual([value[0] for value in launcher.refresh_url_state(rows, urls).values()], [4, 4, 4, 4])
            self.assertEqual(publish.call_count, 3)

    def test_already_complete_urls_do_not_launch_collector(self):
        url = "https://www.instagram.com/reels/A/"
        rows = [{"url": url, "collection_number": 5, "collected_at": "2026-10-04T00:00:00Z"}]
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, "read_history", return_value=rows), patch.object(launcher, "collector_main") as collect:
            dataset = DatasetConfig("fashion", Path(directory), ())
            self.assertEqual(launcher.refresh_until_collection_count(dataset, [url], [], Path(directory) / "urls.txt", 4, 4, False, schedule_basis="last-collected"), 0)
        collect.assert_not_called()

    def test_duplicate_history_rows_do_not_increase_count(self):
        url = "https://www.instagram.com/reels/A/"
        row = {"url": url, "collection_number": 2, "collected_at": "2026-10-04T00:00:00Z"}
        self.assertEqual(launcher.refresh_url_state([row, row], [url])[url][0], 2)

    def test_missing_timestamp_is_rejected(self):
        url = "https://www.instagram.com/reels/A/"
        with self.assertRaisesRegex(ValueError, "collected_at"):
            launcher.refresh_url_state([{"url": url, "collection_number": 3}], [url])

    def test_no_progress_stops_instead_of_collecting_forever(self):
        url = "https://www.instagram.com/reels/A/"
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, "read_history", return_value=[]), patch.object(launcher, "collector_main", return_value=0), patch.object(launcher, "_due_retry_delay", return_value=0, create=True):
            with self.assertRaisesRegex(RuntimeError, "no new collection"):
                launcher.refresh_until_collection_count(DatasetConfig("fashion", Path(directory), ()), [url], [], Path(directory) / "urls.txt", 4, 4, False, schedule_basis="last-collected")

    def test_collector_failure_stops_after_scheduler_retry_limit(self):
        url = "https://www.instagram.com/reels/A/"
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, "read_history", return_value=[]), patch.object(launcher, "collector_main", return_value=7) as collect, patch.object(launcher, "_due_retry_delay", return_value=0, create=True):
            self.assertEqual(launcher.refresh_until_collection_count(DatasetConfig("fashion", Path(directory), ()), [url], [], Path(directory) / "urls.txt", 4, 4, False, schedule_basis="last-collected"), 7)
        self.assertEqual(collect.call_count, 5)

    def test_partial_batch_retries_only_missing_urls_with_scheduler_backoff(self):
        now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        urls = [f"https://www.instagram.com/reels/{code}/" for code in ("A", "B")]
        rows, batches = [], []

        class Clock:
            @staticmethod
            def now(tz):
                return now

        def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)

        def collect(arguments):
            selected = urls_file.read_text(encoding="utf-8").splitlines()
            batches.append((now, selected))
            # First call saves A but fails for B; second saves B.
            rows.append({"url": selected[0], "collected_at": now.isoformat(), "collection_number": 1})
            return 1 if len(batches) == 1 else 0

        with tempfile.TemporaryDirectory() as directory:
            urls_file = Path(directory) / "urls.txt"
            with patch.object(launcher, "read_history", side_effect=lambda _: list(rows)), patch.object(launcher, "collector_main", side_effect=collect), patch.object(launcher, "datetime", Clock), patch.object(launcher.time, "sleep", side_effect=sleep):
                self.assertEqual(launcher.refresh_until_collection_count(DatasetConfig("fashion", Path(directory), ()), urls, [], urls_file, 1, 4, False, schedule_basis="last-collected"), 0)
        self.assertEqual([selected for _, selected in batches], [urls, urls[1:]])
        self.assertEqual((batches[1][0] - batches[0][0]).total_seconds(), 300)

    def test_terminal_stop_is_not_retried(self):
        url = "https://www.instagram.com/reels/A/"
        for code in (2, 130):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory, patch.object(launcher, "read_history", return_value=[]), patch.object(launcher, "collector_main", return_value=code) as collect:
                self.assertEqual(launcher.refresh_until_collection_count(DatasetConfig("fashion", Path(directory), ()), [url], [], Path(directory) / "urls.txt", 4, 4, False, schedule_basis="last-collected"), code)
                collect.assert_called_once()

    def test_access_failures_keep_shared_cooldown_and_authentication_barriers(self):
        url = "https://www.instagram.com/reels/A/"
        for failure in ("login_required", "challenge_required", "rate_limited"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, patch.object(launcher, "read_history", return_value=[]), patch.object(launcher, "collector_main", return_value=1) as collect, patch.object(launcher, "_collector_access_failure", return_value=failure), patch.object(launcher, "_collector_rate_limit_delay", return_value=3600) as cooldown, patch.object(launcher.time, "sleep", side_effect=InterruptedError("stop test wait")) as sleep, patch.object(launcher, "_due_retry_delay") as ordinary_backoff:
                arguments = (DatasetConfig("fashion", Path(directory), ()), [url], [], Path(directory) / "urls.txt", 4, 4, False)
                if failure == "rate_limited":
                    with self.assertRaises(InterruptedError):
                        launcher.refresh_until_collection_count(*arguments, schedule_basis="last-collected")
                    cooldown.assert_called_once()
                else:
                    self.assertEqual(launcher.refresh_until_collection_count(*arguments, schedule_basis="last-collected"), 1)
                    cooldown.assert_not_called()
                    sleep.assert_not_called()
                ordinary_backoff.assert_not_called()
                collect.assert_called_once()
