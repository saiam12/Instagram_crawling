import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.exporters.instagram_collector import write_xlsx_workbook
from src.reels.refresh_import import import_refresh_snapshots
from src.reels.fashion_beauty_collection import read_history
from src.reels.fashion_beauty_scheduler import DatasetConfig
from src.reels.instagram_reels_browser import AtomicProcessLock, LongReelStore, REEL_HISTORY_FILENAME, integrate_long_collected_record
from scripts.instagram_reels_python import main


class RefreshImportTests(unittest.TestCase):
    def test_imports_selected_url_history_once_and_next_round_is_appended(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'external.xlsx'
            url = 'https://www.instagram.com/reels/target/'
            write_xlsx_workbook(source, [('reels', [
                ['url', 'collected_at', 'collection_number', 'like_count', 'user_id', 'uploaded_at'],
                [url, '2026-10-20T00:00:00Z', '1', '10', '123', '2026-10-19T00:00:00Z'],
                [url, '2026-10-20T04:00:00Z', '2', '12', '123', '2026-10-19T00:00:00Z'],
                ['https://www.instagram.com/reels/excluded/', '2026-10-20T00:00:00Z', '1', '99'],
            ])])
            original = source.read_bytes()
            self.assertEqual(import_refresh_snapshots(source, root, start_row=3, end_row=3), 2)
            self.assertEqual(import_refresh_snapshots(source, root), 1)
            self.assertEqual(import_refresh_snapshots(source, root), 0)
            rows = read_history(DatasetConfig('refresh', root, ()))
            result = integrate_long_collected_record(rows, {'url': url, 'collected_at': '2026-10-20T08:00:00Z', 'like_count': 15}, enforce_cooldown=False)
            self.assertEqual(result['collection_number'], 3)
            self.assertEqual([str(row['like_count']) for row in rows if row['url'] == url], ['10', '12', '15'])
            self.assertEqual(source.read_bytes(), original)

    def test_wide_export_and_existing_metrics_take_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'external.xlsx'
            write_xlsx_workbook(source, [('reels', [
                ['url', 'collection_number', 'collected_at', 'like_count', '2nd collect_collected_at', '2nd collect_like_count'],
                ['https://www.instagram.com/reels/target/', '2', '2026-10-20T00:00:00.123Z', '10', '2026-10-20T04:00:00Z', '12(+2)'],
            ])])
            self.assertEqual(import_refresh_snapshots(source, root), 2)
            self.assertEqual(import_refresh_snapshots(source, root), 0)
            rows = read_history(DatasetConfig('refresh', root, ()))
            self.assertEqual([row['collection_number'] for row in rows], ['1', '2'])
            self.assertEqual([row['like_count'] for row in rows], ['10', '12'])

    def test_import_refuses_active_collector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'external.xlsx'
            write_xlsx_workbook(source, [('reels', [['url'], ['https://www.instagram.com/reels/target/']])])
            lock = AtomicProcessLock(root / 'collector.lock.json')
            self.assertTrue(lock.acquire())
            try:
                with self.assertRaises(RuntimeError):
                    import_refresh_snapshots(source, root)
            finally:
                lock.release()

    def test_common_store_recovers_imported_snapshots_and_appends_next_round(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'external.xlsx'
            url = 'https://www.instagram.com/reels/target/'
            write_xlsx_workbook(source, [('reels', [['url', 'collected_at', 'like_count'],
                [url, '2026-10-20T00:00:00Z', '10'], [url, '2026-10-20T04:00:00Z', '12']])])
            import_refresh_snapshots(source, root)
            async def run():
                store = await LongReelStore.create(root / '.collector' / REEL_HISTORY_FILENAME, disable_recollect_cooldown=True)
                self.assertEqual(len(store.rows), 2)
                result = await store.append({'url': url, 'collected_at': '2026-10-20T08:00:00Z', 'like_count': 15})
                self.assertEqual(result['collection_number'], 3)
                await store.flush()
            asyncio.run(run())
            self.assertEqual(len(read_history(DatasetConfig('refresh', root, ()))), 3)

    def test_external_file_uses_common_collector_without_overwriting_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'reels.xlsx'
            write_xlsx_workbook(source, [('reels', [['url', 'collected_at', 'like_count'], ['https://www.instagram.com/reels/target/', '2026-10-20T00:00:00Z', '10']])])
            original = source.read_bytes()
            def collect(arguments):
                rows = read_history(DatasetConfig('refresh', root, ()))
                self.assertEqual(len(rows), 1)
                self.assertIn('--disable-recollect-cooldown', arguments)
                write_xlsx_workbook(source, [('reels', [['url']])])
                return 0
            with patch('scripts.instagram_reels_python.collector_main', side_effect=collect):
                self.assertEqual(main(['refresh', '--input-xlsx', str(source), '--data-dir', str(root)]), 0)
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue((root / 'reels_updated.xlsx').exists())
