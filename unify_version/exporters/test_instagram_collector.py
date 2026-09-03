import tempfile
import unittest
import zipfile
from pathlib import Path

from exporters.instagram_collector import DataStore, _xlsx_cell, _xlsx_project_rows


class XlsxCollectionTimingTests(unittest.TestCase):
    def test_combined_workbook_contains_only_hashtags_reels_and_users_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            for name in ("hashtags", "reels", "users", "fashion_reels"):
                (data_dir / f"{name}.csv").write_text(
                    "name,value\nexample,1\n", encoding="utf-8"
                )

            DataStore(data_dir).sync_xlsx()

            with zipfile.ZipFile(data_dir / "instagram_data.xlsx") as workbook:
                workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
            self.assertIn('name="hashtags"', workbook_xml)
            self.assertIn('name="reels"', workbook_xml)
            self.assertIn('name="users"', workbook_xml)
            self.assertNotIn('name="fashion_reels"', workbook_xml)

    def test_change_cells_use_signed_formats_except_for_zero(self) -> None:
        for value in ("19", "-1", "0"):
            with self.subTest(value=value):
                self.assertIn('s="6"', _xlsx_cell("A2", value, "view_count_change", False))
        for value in ("0.25", "-0.25", "0"):
            with self.subTest(value=value):
                self.assertIn('s="7"', _xlsx_cell("B2", value, "reaction_rate_change", False))

    def test_video_duration_cells_use_one_decimal_place(self) -> None:
        self.assertIn('s="9"', _xlsx_cell("A2", "12.75", "video_duration_seconds", False))

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

        self.assertEqual(values["2nd collect_collected_at"], "2026-01-01T09:00:00Z")
        self.assertEqual(values["2nd collect_days_since_previous"], "+0.4day")
        self.assertEqual(values["3rd collect_collected_at"], "2026-01-02T06:00:00Z")
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
            "days_since_previous",
            "user_id",
            "username",
            "biography",
            "profile_category",
            "post_count",
            "follower_count",
            "follower_count_change",
            "collected_at",
        ])
        self.assertEqual(projected[1:], [
            ["1", "", "1", "sample", "profile biography", "", "", "1000", "", "2026-01-01T00:00:00Z"],
            ["2", "+0.3day", "1", "sample", "profile biography", "", "", "1100", "100", "2026-01-01T06:00:00Z"],
            ["3", "+2day", "1", "sample", "profile biography", "", "", "1050", "-50", "2026-01-03T06:00:00Z"],
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
            ["collection_number", "days_since_previous", "user_id", "username", "biography", "profile_category", "post_count", "follower_count", "follower_count_change", "collected_at"],
            ["1", "", "2", "pending_user", "", "", "", "", "", ""],
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
            "2", "", "3", "late_count", "", "", "", "1100", "", "2026-01-02T00:00:00Z"
        ])

    def test_user_elapsed_days_uses_previous_successful_snapshot_only(self) -> None:
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
            "3", "+2day", "4", "failed_middle_snapshot", "", "", "", "1100", "100",
            "2026-01-03T00:00:00Z",
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
                    "3", "+2day", "5", "invalid_middle_snapshot", "", "", "", "1100", "100",
                    "2026-01-03T00:00:00Z",
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
