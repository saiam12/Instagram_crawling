import os
import tempfile
import unittest
from pathlib import Path

from src.reels.android_metric_queue import _android_metric_queue_paths
from src.reels.process_lock import AtomicProcessLock
from src.reels.reel_store import open_csv_snapshot, write_csv_records


@unittest.skipUnless(os.name == "nt", "Windows history snapshot behavior")
class HistorySnapshotConcurrencyTests(unittest.TestCase):
    def test_read_while_writer_lock_is_held_and_replace_while_snapshot_is_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / ".collector" / "reels_history_active.csv"
            history.parent.mkdir()
            write_csv_records(history, [{"url": "old"}], ["url"])
            lock = AtomicProcessLock(_android_metric_queue_paths(root)["history_lock"])
            self.assertTrue(lock.acquire())
            try:
                with open_csv_snapshot(history, wait_seconds=0) as snapshot:
                    write_csv_records(history, [{"url": "new"}], ["url"])
                    self.assertIn("old", snapshot.read())
                with open_csv_snapshot(history) as snapshot:
                    self.assertIn("new", snapshot.read())
            finally:
                lock.release()
