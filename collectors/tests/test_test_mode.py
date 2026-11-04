import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.exporters.instagram_collector import write_xlsx_workbook
from scripts import instagram_reels_python as launcher
from src.reels.fashion_beauty_collection import read_history
from src.reels.fashion_beauty_scheduler import DatasetConfig


class TestModeTests(unittest.TestCase):
    def test_scheduled_test_is_short_and_isolated(self):
        config = launcher.parse_scheduled_command('fashion', ['--test'])
        self.assertTrue(config.test_mode)
        self.assertEqual(config.duration_hours, 20 / 60)
        self.assertAlmostEqual(config.discovery_hours, 5 / 60)
        self.assertEqual(config.recollection_interval_minutes, 5)
        self.assertEqual(config.target_snapshots, 4)
        self.assertEqual(config.data_root.name, 'data_test')

    def test_refresh_selects_and_imports_at_most_ten_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'external.xlsx'
            write_xlsx_workbook(source, [('reels', [['url', 'collected_at'], *[[f'https://www.instagram.com/reels/test{i}/', '2026-10-29T00:00:00Z'] for i in range(15)]])])
            original = source.read_bytes()
            with patch.object(launcher, 'refresh_until_collection_count', return_value=0) as scheduled:
                self.assertEqual(launcher.main(['refresh', '--test', '--input-xlsx', str(source), '--data-dir', str(root)]), 0)
            self.assertEqual(len(scheduled.call_args.args[1]), 10)
            self.assertEqual(scheduled.call_args.args[5], 5 / 60)
            self.assertTrue(scheduled.call_args.kwargs['test_mode'])
            self.assertEqual(scheduled.call_args.kwargs['schedule_basis'], 'last-collected')
            self.assertEqual(len(read_history(DatasetConfig('fashion', root, ()))), 10)
            self.assertEqual(source.read_bytes(), original)
