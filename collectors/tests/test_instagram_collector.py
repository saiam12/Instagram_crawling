import tempfile
import unittest
import zipfile
from pathlib import Path

from src.exporters.instagram_collector import (
    DataStore, _xlsx_cell, _xlsx_project_rows, read_reel_urls_from_xlsx, write_xlsx_workbook,
)


class XlsxCollectionTimingTests(unittest.TestCase):
    def test_upload_hours_use_each_collection_timestamp_and_timezone(self):
        headers = ['url', 'uploaded_at', 'collected_at']
        rows = [headers,
                ['one', '2026-11-01T00:00:00Z', '2026-11-01T11:30:00+09:00'],
                ['one', '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z'],
                ['two', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z']]
        for layout in ['reels', 'reels_rows', 'reels_web', 'reels_columns', 'new_reels']:
            with self.subTest(layout=layout):
                result = _xlsx_project_rows(layout, rows)
                column = result[0].index('hours_since_upload')
                self.assertEqual([row[column] for row in result[1:]], ['2.50', '24.00', '0.00'])
                self.assertEqual(result[0][column-1], 'uploaded_at')
                self.assertEqual(_xlsx_project_rows(layout, result), result)

    def test_upload_hours_missing_invalid_or_negative_stay_blank(self):
        for uploaded, collected in [('', '2026-11-01T00:00:00Z'), ('bad', 'bad'), ('2026-11-02T00:00:00Z', '2026-11-01T00:00:00Z')]:
            result = _xlsx_project_rows('reels_rows', [['uploaded_at', 'collected_at'], [uploaded, collected]])
            self.assertEqual(result[1][result[0].index('hours_since_upload')], '')

    def test_upload_hours_snapshot_and_numeric_exports(self):
        from src.reels.reel_store import _json_field_value
        result = _xlsx_project_rows('reels_columns', [
            ['uploaded_at', 'collected_at', '2nd collect_collected_at'],
            ['2026-11-01T00:00:00Z', '2026-11-01T01:00:00Z', '2026-11-01T02:30:00Z']])
        self.assertEqual(result[1][result[0].index('2nd collect_hours_since_upload')], '2.50')
        self.assertEqual(_json_field_value('hours_since_upload', '2.50'), 2.5)
        self.assertEqual(_json_field_value('hours_since_upload', ''), None)
        self.assertIn('s="8"><v>2.50</v>', _xlsx_cell('A2', '2.50', 'hours_since_upload', False))

    def test_combined_reels_uses_same_row_layout_and_intervals_as_standalone(self):
        import csv
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [
                ['collection_number', 'collected_at', 'url', 'view_count', 'extra_metric'],
                ['1', '2026-10-30T00:00:00Z', 'https://www.instagram.com/reels/ONE/', '100', 'a'],
                ['2', '2026-10-30T04:00:00Z', 'https://www.instagram.com/reels/ONE/', '200', 'b'],
            ]
            with (root / 'reels.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                csv.writer(stream).writerows(rows)
            store = DataStore(root)
            store.sync_xlsx()
            with zipfile.ZipFile(store.workbook) as archive:
                sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            def cell_value(cell):
                return ''.join(cell.itertext())
            actual = [[cell_value(cell) for cell in row] for row in sheet.findall('s:sheetData/s:row', ns)]
            expected = _xlsx_project_rows('reels_rows', rows)
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[0][1], 'hours_since_previous')
            self.assertEqual(actual[2][1], '+4hour')
            self.assertEqual(actual[2][actual[0].index('extra_metric')], 'b')

    def test_public_reels_omit_internal_metadata_including_snapshot_columns(self):
        hidden = ['korean_source', 'view_count_observed_at', 'view_count_source', 'android_mismatch']
        headers = ['url', 'view_count', *hidden, *['2nd collect_' + name for name in hidden]]
        for layout in ['reels', 'reels_rows', 'reels_web', 'reels_columns']:
            with self.subTest(layout=layout):
                result = _xlsx_project_rows(layout, [headers, ['https://www.instagram.com/reels/ONE/', '123', *['internal'] * 8]])
                self.assertTrue(set(hidden).isdisjoint(result[0]))
                self.assertFalse(any(field.endswith(tuple('_' + name for name in hidden)) for field in result[0]))
                self.assertEqual(result[1][result[0].index('view_count')], '123')

    def test_refresh_urls_use_inclusive_visible_excel_row_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "analysis.xlsx"
            write_xlsx_workbook(workbook, [("reels", [
                ["url", "title"],
                ["https://www.instagram.com/reels/first/", "first"],
                ["https://www.instagram.com/reels/second/", "second"],
                ["https://www.instagram.com/reels/third/", "third"],
            ])])

            self.assertEqual(read_reel_urls_from_xlsx(workbook, start_row=3, end_row=4), [
                "https://www.instagram.com/reels/second/",
                "https://www.instagram.com/reels/third/",
            ])
            self.assertEqual(read_reel_urls_from_xlsx(workbook, start_row=4), [
                "https://www.instagram.com/reels/third/",
            ])
            with self.assertRaisesRegex(ValueError, "end_row"):
                read_reel_urls_from_xlsx(workbook, start_row=4, end_row=3)

    def test_refresh_urls_accept_a_single_analysis_sheet_with_reel_url_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "analysis.xlsx"
            write_xlsx_workbook(workbook, [("analysis", [
                ["reel_url", "score"],
                ["https://www.instagram.com/reel/example/", "1"],
            ])])

            self.assertEqual(read_reel_urls_from_xlsx(workbook), [
                "https://www.instagram.com/reels/example/",
            ])

    def test_combined_workbook_contains_only_hashtags_reels_and_users_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            for name in ("hashtags", "reels", "users", "fashion_reels"):
                (data_dir / f"{name}.csv").write_text(
                    "name,value\nexample,1\n", encoding="utf-8"
                )

            DataStore(data_dir).sync_xlsx()

            with zipfile.ZipFile(data_dir / "refresh_data.xlsx") as workbook:
                workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
            self.assertIn('name="hashtags"', workbook_xml)
            self.assertIn('name="reels"', workbook_xml)
            self.assertIn('name="users"', workbook_xml)
            self.assertNotIn('name="fashion_reels"', workbook_xml)

    def test_change_cells_use_signed_counts_and_compact_percent_precision(self) -> None:
        for value in ("19", "-1", "0"):
            with self.subTest(value=value):
                self.assertIn('s="6"', _xlsx_cell("A2", value, "view_count_change", False))
        for value, style in (("0.25", "11"), ("-0.25", "11"), ("0", "11"),
                             ("0.001", "12"), ("-0.0012", "7")):
            with self.subTest(value=value):
                cell = _xlsx_cell("B2", value, "reaction_rate_change", False)
                self.assertIn(f's="{style}"', cell)
                self.assertIn(f"<v>{value}</v>", cell)

    def test_video_duration_cells_use_one_decimal_place(self) -> None:
        self.assertIn('s="9"', _xlsx_cell("A2", "12.75", "video_duration_seconds", False))

    def test_x_placeholder_in_numeric_column_is_right_aligned(self) -> None:
        self.assertIn('s="10"', _xlsx_cell("A2", "X", "like_count", False))

    def test_xlsx_only_suffix_preserves_numeric_cell_types(self) -> None:
        self.assertIn('s="4"', _xlsx_cell("A2", "1234", "view_count_xlsx_only", False))

    def test_view_count_is_numeric_and_wide_refresh_gets_delta(self) -> None:
        rows = [[
            "url",
            "collected_at",
            "view_count",
            "2nd collect_collected_at",
            "2nd collect_view_count",
        ], [
            "https://www.instagram.com/reels/example/",
            "2026-01-01T00:00:00Z",
            "1000",
            "2026-01-01T06:00:00Z",
            "1250",
        ]]

        projected = _xlsx_project_rows("reels_columns", rows)
        values = dict(zip(projected[0], projected[1]))

        self.assertEqual(values["view_count"], "1000")
        self.assertEqual(values["2nd collect_view_count"], "1,250(+250)")

    def test_row_layout_preserves_each_collection_as_a_row(self) -> None:
        rows = [[
            "collection_number",
            "days_since_previous",
            "url",
            "collected_at",
            "view_count",
        ], [
            "1", "", "https://www.instagram.com/reels/example/",
            "2026-01-01T00:00:00Z", "1000",
        ], [
            "2", "0.25", "https://www.instagram.com/reels/example/",
            "2026-01-01T06:00:00Z", "1250",
        ]]

        projected = _xlsx_project_rows("reels_rows", rows)

        self.assertEqual(len(projected), 3)
        self.assertNotIn("collection_label", projected[0])
        self.assertEqual(projected[2][projected[0].index("view_count")], "1250")
        self.assertEqual(projected[0][1], "hours_since_previous")
        self.assertNotIn("days_since_previous", projected[0])
        self.assertEqual(projected[1][1], "")
        self.assertEqual(projected[2][1], "+6hour")

    def test_row_elapsed_hours_tracks_each_reel_and_ignores_rounded_days(self) -> None:
        rows = [
            ["collection_number", "days_since_previous", "url", "collected_at"],
            ["1", "", "https://www.instagram.com/reels/a/", "2026-01-01T00:00:00Z"],
            ["1", "", "https://www.instagram.com/reels/b/", "2026-01-01T01:00:00Z"],
            ["2", "+0.2day", "https://www.instagram.com/reels/a/", "2026-01-01T04:30:00Z"],
            ["2", "+0.2day", "https://www.instagram.com/reels/b/", "2026-01-01T05:00:00Z"],
        ]
        projected = _xlsx_project_rows("reels_rows", rows)
        self.assertEqual([row[1] for row in projected], ["hours_since_previous", "", "", "+4.5hour", "+4hour"])
        self.assertEqual(_xlsx_project_rows("reels_rows", projected), projected)

    def test_reel_collection_time_and_elapsed_days_are_separate(self) -> None:
        rows = [[
            "url",
            "collected_at",
            "2nd collect_collected_at",
            "2nd collect_like_count",
            "3rd collect_collected_at",
            "3rd collect_like_count",
        ], [
            "https://www.instagram.com/reels/example/",
            "2026-01-01T00:00:00Z",
            "2026-01-01T09:00:00Z",
            "110",
            "2026-01-02T06:00:00Z",
            "120",
        ]]

        projected = _xlsx_project_rows("reels_web", rows)
        header = projected[0]
        values = dict(zip(header, projected[1]))

        self.assertEqual(values["2nd collect_collected_at"], '2026-01-01T18:00:00+09:00')
        self.assertEqual(values["2nd collect_days_since_previous"], "+0.4day")
        self.assertEqual(values["3rd collect_collected_at"], '2026-01-02T15:00:00+09:00')
        self.assertEqual(values["3rd collect_days_since_previous"], "+0.9day")

    def test_user_collection_history_uses_rows_and_collection_numbers(self) -> None:
        rows = [[
            "user_id",
            "username",
            "biography",
            "follower_count",
            "collected_at",
            "2nd collect_follower_count",
            "2nd collect_collected_at",
            "3rd collect_follower_count",
            "3rd collect_collected_at",
        ], [
            "1",
            "sample",
            "profile biography",
            "1000",
            "2026-01-01T00:00:00Z",
            "1100",
            "2026-01-01T06:00:00Z",
            "1050",
            "2026-01-03T06:00:00Z",
        ]]

        projected = _xlsx_project_rows("users", rows)

        self.assertEqual(projected[0], [
            "collection_number",
            "hours_since_previous",
            "user_id",
            "username",
            "biography",
            "profile_category",
            "account_country",
            "post_count",
            "follower_count",
            "following_count",
            "follower_count_change",
            "collected_at",
        ])
        self.assertEqual(projected[1:], [
            ["1", "", "1", "sample", "profile biography", "", "", "", "1000", "", "", '2026-01-01T09:00:00+09:00'],
            ["2", "+6hour", "1", "sample", "profile biography", "", "", "", "1100", "", "100", '2026-01-01T15:00:00+09:00'],
            ["3", "+48hour", "1", "sample", "profile biography", "", "", "", "1050", "", "-50", '2026-01-03T15:00:00+09:00'],
        ])

    def test_user_collection_history_keeps_uncollected_user_as_first_row(self) -> None:
        rows = [[
            "user_id",
            "username",
            "biography",
            "follower_count",
            "collected_at",
            "2nd collect_follower_count",
            "2nd collect_collected_at",
        ], [
            "2",
            "pending_user",
            "",
            "",
            "",
            "",
            "",
        ]]

        projected = _xlsx_project_rows("users", rows)

        self.assertEqual(projected, [
            ["collection_number", "hours_since_previous", "user_id", "username", "biography", "profile_category", "account_country", "post_count", "follower_count", "following_count", "follower_count_change", "collected_at"],
            ["1", "", "2", "pending_user", "", "", "", "", "", "", "", ""],
        ])

    def test_user_collection_history_leaves_change_blank_without_a_previous_count(self) -> None:
        rows = [[
            "user_id",
            "username",
            "biography",
            "follower_count",
            "collected_at",
            "2nd collect_follower_count",
            "2nd collect_collected_at",
        ], [
            "3",
            "late_count",
            "",
            "",
            "",
            "1100",
            "2026-01-02T00:00:00Z",
        ]]

        projected = _xlsx_project_rows("users", rows)

        self.assertEqual(projected[2], [
            "2", "", "3", "late_count", "", "", "", "", "1100", "", "", '2026-01-02T09:00:00+09:00'
        ])

    def test_user_elapsed_hours_uses_previous_successful_snapshot_only(self) -> None:
        rows = [[
            "user_id",
            "username",
            "biography",
            "follower_count",
            "collected_at",
            "2nd collect_follower_count",
            "2nd collect_collected_at",
            "3rd collect_follower_count",
            "3rd collect_collected_at",
        ], [
            "4",
            "failed_middle_snapshot",
            "",
            "1000",
            "2026-01-01T00:00:00Z",
            "",
            "2026-01-02T00:00:00Z",
            "1100",
            "2026-01-03T00:00:00Z",
        ]]

        projected = _xlsx_project_rows("users", rows)

        self.assertEqual(projected[3], [
            "3", "+48hour", "4", "failed_middle_snapshot", "", "", "", "", "1100", "", "100",
            '2026-01-03T09:00:00+09:00',
        ])

    def test_user_baseline_ignores_malformed_timestamp_and_negative_count(self) -> None:
        invalid_middle_snapshots = (
            ("1050", " not-a-timestamp "),
            ("1050", "   "),
            ("-5", "2026-01-02T00:00:00Z"),
        )

        for middle_count, middle_timestamp in invalid_middle_snapshots:
            with self.subTest(
                middle_count=middle_count,
                middle_timestamp=middle_timestamp,
            ):
                rows = [[
                    "user_id",
                    "username",
                    "follower_count",
                    "collected_at",
                    "2nd collect_follower_count",
                    "2nd collect_collected_at",
                    "3rd collect_follower_count",
                    "3rd collect_collected_at",
                ], [
                    "5",
                    "invalid_middle_snapshot",
                    "1000",
                    "2026-01-01T00:00:00Z",
                    middle_count,
                    middle_timestamp,
                    "1100",
                    "2026-01-03T00:00:00Z",
                ]]

                projected = _xlsx_project_rows("users", rows)

                self.assertEqual(projected[3], [
                    "3", "+48hour", "5", "invalid_middle_snapshot", "", "", "", "", "1100", "", "100",
                    '2026-01-03T09:00:00+09:00',
                ])

    def test_reel_reaction_rate_is_exported_as_a_fixed_field(self) -> None:
        rows = [[
            "url",
            "collected_at",
            "follower_count",
            "reaction_rate",
            "follower_count_collected_at",
            "follower_lookup_status",
        ], [
            "https://www.instagram.com/reels/example/",
            "2026-01-01T00:00:00Z",
            "1000",
            "0.1",
            "2026-01-01T00:00:01Z",
            "success",
        ]]

        projected = _xlsx_project_rows("reels_rows", rows)

        self.assertIn("reaction_rate", projected[0])
        self.assertNotIn("follower_count_collected_at", projected[0])
        self.assertNotIn("follower_lookup_status", projected[0])


if __name__ == "__main__":
    unittest.main()
