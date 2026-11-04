import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.exporters.instagram_collector import write_xlsx_workbook
from src.reels.fashion_beauty_scheduler import DatasetConfig
from scripts import instagram_reels_python as launcher


class RefreshScheduleBasisTests(unittest.TestCase):
    def test_upload_grid_does_not_drift_with_import_or_collection_delay(self):
        uploaded = datetime(2026, 10, 29, tzinfo=timezone.utc)
        now = uploaded + timedelta(hours=2)
        self.assertEqual(launcher.refresh_due_at(uploaded, now, now, 3, 'uploaded'), uploaded + timedelta(hours=3))
        self.assertEqual(launcher.refresh_age_timing(uploaded, now, now, (3, 3, 6, 12))[0], uploaded + timedelta(hours=3))
        self.assertEqual(launcher.refresh_due_at(uploaded, now, now, 3, 'last-collected'), uploaded + timedelta(hours=5))
        delayed = uploaded + timedelta(hours=3, minutes=10)
        self.assertEqual(launcher.refresh_due_at(uploaded, delayed, delayed, 3, 'uploaded'), uploaded + timedelta(hours=6))
        restarted = uploaded + timedelta(hours=7)
        self.assertLessEqual(launcher.refresh_due_at(uploaded, now, restarted, 3, 'uploaded'), restarted)
        self.assertEqual(launcher.refresh_due_at(uploaded, restarted, restarted, 3, 'uploaded'), uploaded + timedelta(hours=9))

    def test_two_hour_old_import_is_collected_at_upload_plus_three_hours(self):
        for age_schedule in (None, (3, 3, 6, 12)):
            with self.subTest(age_schedule=age_schedule), tempfile.TemporaryDirectory() as directory:
                uploaded = datetime(2026, 10, 29, tzinfo=timezone.utc)
                current = [uploaded + timedelta(hours=2)]
                url = 'https://www.instagram.com/reels/target/'
                rows = [{'url': url, 'uploaded_at': uploaded.isoformat(), 'collected_at': current[0].isoformat(), 'collection_number': 1}]
                collected = []
                class Clock:
                    @staticmethod
                    def now(tz): return current[0]
                def sleep(seconds): current[0] += timedelta(seconds=seconds)
                def collect(arguments):
                    collected.append(current[0])
                    rows.append({**rows[0], 'collected_at': current[0].isoformat(), 'collection_number': 2})
                    return 0
                root = Path(directory)
                with patch.object(launcher, 'datetime', Clock), patch.object(launcher, 'read_history', side_effect=lambda _: rows), patch.object(launcher.time, 'sleep', side_effect=sleep), patch.object(launcher, 'collector_main', side_effect=collect):
                    self.assertEqual(launcher.refresh_until_collection_count(DatasetConfig('fashion', root, ()), [url], [], root / 'urls.txt', 2, 3, False, age_schedule, schedule_basis='uploaded'), 0)
                self.assertEqual(collected, [uploaded + timedelta(hours=3)])

    def test_cli_defaults_to_uploaded_and_accepts_last_collected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'external.xlsx'
            write_xlsx_workbook(source, [('reels', [['url'], ['https://www.instagram.com/reels/target/']])])
            for options, expected in (([], 'uploaded'), (['--uploaded'], 'uploaded'), (['--last-collected'], 'last-collected'), (['--schedule-basis', 'last-collected'], 'last-collected')):
                with patch.object(launcher, 'refresh_until_collection_count', return_value=0) as scheduled:
                    self.assertEqual(launcher.main(['refresh', '--input-xlsx', str(source), '--data-dir', str(root), '--age-schedule', *options]), 0)
                    self.assertEqual(scheduled.call_args.kwargs['schedule_basis'], expected)
                    self.assertEqual(scheduled.call_args.kwargs['age_basis'], 'first-collected' if expected == 'last-collected' else 'uploaded')
                    self.assertEqual(scheduled.call_args.args[7], (2, 3, 6, 12))

    def test_default_stage_boundaries_and_end(self):
        uploaded = datetime(2026, 10, 29, tzinfo=timezone.utc)
        for age, gap in ((0, 2), (22, 2), (24, 3), (93, 3), (96, 6), (114, 6), (120, 12), (156, 12)):
            now = uploaded + timedelta(hours=age)
            due, _ = launcher.refresh_age_timing(uploaded, now, now, (2, 3, 6, 12))
            self.assertEqual(due, now + timedelta(hours=gap))
        self.assertIsNone(launcher.refresh_age_timing(uploaded, uploaded, uploaded + timedelta(hours=168), (2, 3, 6, 12)))
