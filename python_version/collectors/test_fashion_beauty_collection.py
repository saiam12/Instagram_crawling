from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fashion_beauty_scheduler import (
    BEAUTY_KEYWORDS,
    FASHION_KEYWORDS,
    SNAPSHOT_OFFSETS,
    DatasetConfig,
    due_jobs,
    initial_count_in_window,
    is_initial_candidate_allowed,
    keyword_group,
    window_dataset,
)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SchedulerTests(unittest.TestCase):
    def test_due_time_is_anchored_to_first_snapshot(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        rows = [
            {"url": "https://www.instagram.com/reels/a/", "collection_number": "1", "collected_at": isoformat_utc(base)},
            {"url": "https://www.instagram.com/reels/a/", "collection_number": "2", "collected_at": isoformat_utc(base + timedelta(minutes=30))},
        ]
        jobs = due_jobs(DatasetConfig("fashion", Path("C:/tmp"), FASHION_KEYWORDS), rows, base + timedelta(hours=1))
        self.assertEqual([(job.url, job.due_at) for job in jobs], [("https://www.instagram.com/reels/a/", base + timedelta(hours=1))])

    def test_six_snapshots_produce_no_future_job(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        rows = [{"url": "https://www.instagram.com/reels/a/", "collection_number": str(index + 1), "collected_at": isoformat_utc(base + SNAPSHOT_OFFSETS[index])} for index in range(6)]
        self.assertEqual(due_jobs(DatasetConfig("fashion", Path("C:/tmp"), FASHION_KEYWORDS), rows, base + timedelta(days=2)), [])

    def test_windows_alternate_and_each_keyword_set_has_48_entries(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        self.assertEqual(window_dataset(base, base), "fashion")
        self.assertEqual(window_dataset(base, base + timedelta(minutes=30)), "beauty")
        self.assertEqual(len(FASHION_KEYWORDS), 48)
        self.assertEqual(len(BEAUTY_KEYWORDS), 48)
        self.assertEqual(len(keyword_group(FASHION_KEYWORDS, 7)), 6)

    def test_upload_age_filters_only_initial_candidates(self) -> None:
        captured = datetime(2026, 8, 26, tzinfo=timezone.utc)
        self.assertTrue(is_initial_candidate_allowed("2026-07-27T00:00:00Z", captured, 30))
        self.assertFalse(is_initial_candidate_allowed("2026-07-26T23:59:59Z", captured, 30))

    def test_initial_count_uses_only_initial_rows_in_half_open_window(self) -> None:
        start = datetime(2026, 8, 26, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        rows = [
            {"url": "https://www.instagram.com/reels/a/", "collection_number": "1", "collected_at": isoformat_utc(start)},
            {"url": "https://www.instagram.com/reels/b/", "collection_number": "2", "collected_at": isoformat_utc(start + timedelta(minutes=10))},
            {"url": "https://www.instagram.com/reels/c/", "collection_number": "1", "collected_at": isoformat_utc(end)},
            {"url": "https://www.instagram.com/reels/d/", "collection_number": "1", "collected_at": "not-a-timestamp"},
        ]
        self.assertEqual(initial_count_in_window(rows, start, end), 1)

    def test_keyword_groups_are_one_based_and_cycle(self) -> None:
        self.assertEqual(keyword_group(FASHION_KEYWORDS, 1), tuple(FASHION_KEYWORDS[:6]))
        self.assertEqual(keyword_group(FASHION_KEYWORDS, 9), tuple(FASHION_KEYWORDS[:6]))

    def test_malformed_url_or_timestamp_is_excluded_from_due_jobs(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        rows = [
            {"url": "not a URL", "collection_number": "1", "collected_at": isoformat_utc(base)},
            {"url": "https://www.instagram.com/reels/b/", "collection_number": "1", "collected_at": "not-a-timestamp"},
        ]
        self.assertEqual(due_jobs(DatasetConfig("fashion", Path("C:/tmp"), FASHION_KEYWORDS), rows, base + timedelta(days=2)), [])


if __name__ == "__main__":
    unittest.main()
