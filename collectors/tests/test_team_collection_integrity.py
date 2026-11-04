from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.exporters import instagram_collector as exporter
from src.reels import instagram_reels_browser as browser
from src.reels.reel_store import write_csv_records

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor" / "backend"))
from state import Snapshot


class ReconcileWorkbookTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconcile_keeps_latest_fallback_while_original_workbook_is_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = await browser.LongReelStore.create(root / ".collector" / browser.REEL_HISTORY_FILENAME)
            await store.append({"url": "https://www.instagram.com/reels/TARGET/",
                                "collected_at": "2026-10-01T00:00:00Z", "view_count": 100})
            await store.flush()
            browser.write_reel_xlsx(root / "reels_updated.xlsx", store.rows, store.fields,
                                    layout="rows", project_rows=False)
            original_write = exporter.write_xlsx_workbook

            def locked_workbook(destination, sheets):
                if destination.name == "reels.xlsx":
                    raise PermissionError("Workbook is open in Excel")
                return original_write(destination, sheets)

            with patch.object(exporter, "write_xlsx_workbook", side_effect=locked_workbook):
                result = await browser.reconcile_reel_exports(root)
            self.assertTrue(result["outputs"]["xlsx"].is_file())
            self.assertFalse(result["updatedWorkbookRemoved"])
            self.assertEqual(browser._reel_records_from_xlsx(result["outputs"]["xlsx"])[0]["view_count"], "100")


class CollectorLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_incomplete_lock_is_preserved_instead_of_stolen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.lock.json"
            path.write_text("", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                await browser.CollectorLock(directory).acquire()
            self.assertEqual(path.read_text(encoding="utf-8"), "")

    async def test_released_collector_can_start_again_and_blocks_another_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            first = await browser.CollectorLock(directory).acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "Another collector"):
                    await browser.CollectorLock(directory).acquire()
            finally:
                await first.release()
            second = await browser.CollectorLock(directory).acquire()
            await second.release()
            self.assertFalse((Path(directory) / "collector.lock.json").exists())


class MonitorCompletionTests(unittest.TestCase):
    def preview(self, root, patch_result):
        row = {"url": "https://www.instagram.com/reels/TARGET/", "username": "creator",
               "collected_at": "2026-10-01T00:00:00Z", "like_count": "1234",
               "share_count": "", "saved_count": ""}
        write_csv_records(root / ".collector" / browser.REEL_HISTORY_FILENAME, [row], list(row))
        result = {"target": {"url": row["url"], "collected_at": row["collected_at"]},
                  "observed_reel_url": row["url"], "observed_username": "creator",
                  "status": "collected", "metrics": {"like_count": 1200, "share_count": 7, "saved_count": 8},
                  **patch_result}
        completed = root / ".collector" / "android_metric_queue" / "completed" / "job.json"
        completed.parent.mkdir(parents=True)
        completed.write_text(json.dumps(result), encoding="utf-8")
        rows, warning = Snapshot(root).rows({"id": "test", "path": root})
        self.assertEqual(warning, "")
        # The monitor only projects data; the underlying exact count stays intact.
        self.assertEqual(browser.read_csv_objects(root / ".collector" / browser.REEL_HISTORY_FILENAME)[1][0]["like_count"], "1234")
        return rows[0]

    def test_unverified_completion_does_not_change_preview(self):
        for evidence in ({"observed_reel_url": "https://www.instagram.com/reels/OTHER/"},
                         {"observed_username": "other_creator"},
                         {"observed_reel_url": "", "observed_username": ""}):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as directory:
                row = self.preview(Path(directory), evidence)
                self.assertEqual(row["like_count"], "1234")
                self.assertFalse(row["android_available"])

    def test_compact_completion_does_not_replace_exact_count(self):
        for recollection in (False, True):
            with self.subTest(recollection=recollection), tempfile.TemporaryDirectory() as directory:
                row = self.preview(Path(directory), {"compact_fields": ["like_count"], "recollection": recollection})
                self.assertEqual(row["like_count"], "1234")
                self.assertEqual(row["share_count"], 7)

    def test_verified_exact_completion_and_private_marker_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self.preview(Path(directory), {"like_count_private": True})
            self.assertEqual(row["like_count"], "X")
            self.assertTrue(row["android_available"])


if __name__ == "__main__":
    unittest.main()
