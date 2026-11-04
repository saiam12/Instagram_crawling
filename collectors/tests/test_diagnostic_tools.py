import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "diagnostics"


class DiagnosticToolTests(unittest.TestCase):
    def run_tool(self, name, directory, *arguments):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / name), *map(str, arguments)],
            cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )

    def test_capture_uses_selected_dataset_without_historical_patch_files_or_log(self):
        with tempfile.TemporaryDirectory(prefix="team data ") as directory:
            data = Path(directory) / "custom dataset"
            history = data / ".collector" / "reels_history_active.csv"
            history.parent.mkdir(parents=True)
            history.write_text(
                "url,collection_number,username,view_count,follower_count\n"
                "https://www.instagram.com/reel/ONE/,2,team_user,100,25\n"
                "https://www.instagram.com/reel/TWO/,2,other_user,,50\n",
                encoding="utf-8",
            )
            output = Path(directory) / "evidence"
            result = self.run_tool("capture_view_count_evaluation.py", directory,
                                   "--data-dir", data, "--output", output)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((output / "capture_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["rounds_at_capture"]["2"], {
                "rows": 2, "follower_present": 2, "view_present": 1,
                "follower_present_view_missing": 1,
            })
            self.assertEqual(Path(summary["data_source"]), data)
            self.assertNotIn("pre_patch_aggregate_recorded_in_prior_investigation", summary)
            with (output / "history_snapshot.csv").open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)

    def test_capture_missing_history_reports_error_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            result = self.run_tool("capture_view_count_evaluation.py", directory,
                                   "--data-dir", directory, "--output", output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reels_history_active.csv", result.stderr)
            self.assertFalse(output.exists())

    def test_visualization_accepts_one_custom_csv_outside_repository(self):
        with tempfile.TemporaryDirectory(prefix="team visualization ") as directory:
            source = Path(directory) / "my reels.csv"
            source.write_text("video_duration_seconds,view_count\n12,100\n20,250\n", encoding="utf-8")
            output = Path(directory) / "report" / "chart.html"
            result = self.run_tool("visualize_duration_vs_play_count.py", directory,
                                   "--csv", source, "--output", output)
            self.assertEqual(result.returncode, 0, result.stderr)
            html = output.read_text(encoding="utf-8")
            self.assertIn('"duration":12.0,"playCount":100.0', html)
            self.assertIn('"duration":20.0,"playCount":250.0', html)


if __name__ == "__main__":
    unittest.main()
