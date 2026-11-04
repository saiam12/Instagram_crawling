import unittest
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from src.exporters.instagram_collector import _format_export_rows, _xlsx_cell
from src.exporters.instagram_collector import write_xlsx_workbook
from src.reels.reel_store import _json_field_value


class ExportDisplayTests(unittest.TestCase):
    def test_user_workbooks_and_csv_keep_identical_collection_history(self):
        import csv
        from src.reels.instagram_reels_browser import write_users_xlsx
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.collector').mkdir()
            (root / '.collector/users_history_active.csv').write_text(
                'user_id,username,follower_count,collected_at,2nd collect_follower_count,2nd collect_collected_at\n'
                '1,sample,100,2026-10-29T14:00:00.123Z,107,2026-10-29T16:00:00.456Z\n')
            write_users_xlsx(root)
            with (root / 'users.csv').open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row['collection_number'] for row in rows], ['1', '2'])
            self.assertEqual(rows[1]['follower_count_change'], '7')
            self.assertEqual(rows[1]['collected_at'], '2026-10-30T01:00:00+09:00')
            self.assertFalse((root / 'collection_results_users.xlsx').exists())
            with zipfile.ZipFile(root / 'refresh_data.xlsx') as combined:
                self.assertIn('107', combined.read('xl/worksheets/sheet1.xml').decode('utf-8'))
            self.assertIn('.123Z', (root / '.collector/users_history_active.csv').read_text())

    def test_already_projected_users_keep_rounds_and_previous_values(self):
        from src.exporters.instagram_collector import _xlsx_project_rows
        rows = [['collection_number', 'username', 'hours_since_previous', 'follower_count_change', 'collected_at'],
                ['3', 'sample', '+2hour', '7', '2026-10-29T15:22:00Z']]
        projected = _xlsx_project_rows('users', rows)
        values = dict(zip(projected[0], projected[1]))
        self.assertEqual(values['collection_number'], '3')
        self.assertEqual(values['hours_since_previous'], '+2hour')
        self.assertEqual(values['follower_count_change'], '7')
        self.assertEqual(_xlsx_project_rows('users', projected), projected)

    def test_public_collection_times_are_kst_without_shifting_the_instant(self):
        rows = [['collected_at', '2nd collect_collected_at'],
                ['2026-10-29T15:22:00.999Z', '2026-10-30T00:22:00.999+09:00']]
        result = _format_export_rows(rows)
        self.assertEqual(result[1], ['2026-10-30T00:22:00+09:00'] * 2)
        self.assertEqual(_format_export_rows(result), result)
        self.assertEqual(rows[1][0], '2026-10-29T15:22:00.999Z')

    def test_public_exports_drop_android_mismatch(self):
        from src.reels.instagram_reels_browser import project_public_records
        fields, rows = project_public_records('reels_rows', [{'url': 'https://www.instagram.com/reel/test/', 'android_mismatch': 'legacy detail'}], ['url', 'android_mismatch'])
        self.assertNotIn('android_mismatch', fields)
        self.assertNotIn('android_mismatch', rows[0])

    def test_public_timestamp_and_duration_do_not_mutate_history(self):
        rows = [['collected_at', 'video_duration_seconds'], ['2026-10-24T01:14:09.949000', '12.3456']]
        result = _format_export_rows(rows)
        self.assertEqual(result[1], ['2026-10-24T10:14:09+09:00', '12.3'])
        self.assertEqual(rows[1][0], '2026-10-24T01:14:09.949000')
        self.assertEqual(_json_field_value('video_duration_seconds', '12.3456'), 12.3)

    def test_percent_scientific_notation_is_numeric(self):
        cell = _xlsx_cell('A2', '9.99e-7', 'reaction_rate_change', False)
        self.assertNotIn('inlineStr', cell)
        self.assertIn('<v>9.99e-7</v>', cell)

    def test_percent_precision_has_no_forced_trailing_zeroes(self):
        for value, style in [('0.99', '11'), ('0.001', '12'), ('0.001234', '7')]:
            self.assertIn(f's="{style}"', _xlsx_cell('A2', value, 'reaction_rate_change', False))

    def test_future_workbook_contains_every_referenced_percent_style(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'styles.xlsx'
            write_xlsx_workbook(target, [('reels', [
                ['reaction_rate_change'], ['0.99'], ['0.001'], ['0.001234'], ['9.99e-7']
            ])])
            ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            with zipfile.ZipFile(target) as archive:
                styles = ElementTree.fromstring(archive.read('xl/styles.xml'))
                xfs = styles.find('m:cellXfs', ns)
                formats = {int(n.get('numFmtId')): n.get('formatCode') for n in styles.find('m:numFmts', ns)}
                sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
                codes = [formats[int(xfs[int(c.get('s'))].get('numFmtId'))]
                         for row in sheet.findall('m:sheetData/m:row', ns)[1:]
                         for c in row.findall('m:c', ns)]
            self.assertEqual(codes, ['0%', '0.0%', '0.00%', '0%'])
