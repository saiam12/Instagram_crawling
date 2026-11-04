import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from scripts import instagram_reels_python as launcher


class DataDirectoryTests(unittest.TestCase):
    def test_combined_workbook_names_follow_collection_and_refresh_directories(self):
        from src.exporters.instagram_collector import DataStore
        with tempfile.TemporaryDirectory() as directory:
            for folder, filename in [
                ('collector', 'collection_data.xlsx'),
                ('collector/.datasets/fashion', 'collection_data.xlsx'),
                ('refresh', 'refresh_data.xlsx'),
            ]:
                with self.subTest(folder=folder):
                    root = Path(directory) / folder
                    root.mkdir(parents=True)
                    (root / 'reels.csv').write_text('url,view_count\nhttps://www.instagram.com/reels/ONE/,12\n', encoding='utf-8')
                    store = DataStore(root)
                    store.sync_xlsx()
                    self.assertEqual(store.workbook, root / filename)
                    self.assertTrue(store.workbook.is_file())
                    other = 'refresh_data.xlsx' if filename == 'collection_data.xlsx' else 'collection_data.xlsx'
                    self.assertFalse((root / other).exists())

    def test_collect_and_scheduled_collection_default_to_collectors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            previous = Path.cwd()
            try:
                with patch.object(launcher, 'PROJECT_ROOT', root), patch.object(launcher, 'collector_main', return_value=0) as collect:
                    self.assertEqual(launcher.main(['collect', '--collector-mode', 'web']), 0)
                    args = collect.call_args.args[0]
                    self.assertEqual(Path(args[args.index('--data-dir') + 1]), root / 'data' / 'collector')
                    self.assertEqual(launcher.parse_scheduled_command('fashion', []).data_root, root / 'data' / 'collector')
            finally:
                os.chdir(previous)

    def test_refresh_default_is_separate_and_explicit_directory_wins(self):
        for explicit in [False, True]:
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / 'input.xlsx'
                source.touch()
                expected = root / ('custom' if explicit else 'data/refresh')
                previous = Path.cwd()
                try:
                    with patch.object(launcher, 'PROJECT_ROOT', root), patch.object(launcher, 'collector_main', return_value=0) as collect, \
                         patch.object(launcher, 'protect_refresh_workbook', return_value=nullcontext()), \
                         patch.object(launcher, 'read_reel_urls_from_xlsx', return_value=['https://www.instagram.com/reels/TEST01/']), \
                         patch('src.reels.refresh_import.import_refresh_snapshots', return_value=0):
                        args = ['refresh', '--collector-mode', 'web', '--input-xlsx', str(source)]
                        if explicit:
                            args.extend(['--data-dir', str(expected)])
                        self.assertEqual(launcher.main(args), 0)
                        actual = collect.call_args.args[0]
                        self.assertEqual(Path(actual[actual.index('--data-dir') + 1]), expected)
                finally:
                    os.chdir(previous)
