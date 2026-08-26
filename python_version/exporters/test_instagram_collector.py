import unittest

from exporters.instagram_collector import _xlsx_project_rows


class XlsxCollectionTimingTests(unittest.TestCase):
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
            "user_id",
            "username",
            "biography",
            "follower_count",
            "follower_count_change",
            "collected_at",
        ])
        self.assertEqual(projected[1:], [
            ["1", "1", "sample", "profile biography", "1000", "", "2026-01-01T00:00:00Z"],
            ["2", "1", "sample", "profile biography", "1100", "100", "2026-01-01T06:00:00Z"],
            ["3", "1", "sample", "profile biography", "1050", "-50", "2026-01-03T06:00:00Z"],
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
            ["collection_number", "user_id", "username", "biography", "follower_count", "follower_count_change", "collected_at"],
            ["1", "2", "pending_user", "", "", "", ""],
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
            "2", "3", "late_count", "", "1100", "", "2026-01-02T00:00:00Z"
        ])

    def test_retired_reel_fields_are_not_exported(self) -> None:
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

        self.assertNotIn("reaction_rate", projected[0])
        self.assertNotIn("follower_count_collected_at", projected[0])
        self.assertNotIn("follower_lookup_status", projected[0])


if __name__ == "__main__":
    unittest.main()
