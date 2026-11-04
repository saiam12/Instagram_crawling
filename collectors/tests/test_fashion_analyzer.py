from __future__ import annotations

import asyncio
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.reels import fashion_analyzer


class FashionAnalyzerTests(unittest.TestCase):
    def test_default_repository_resolves_the_analyzer_after_collector_move(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        expected_script = repository / "analyzers" / "reel_analyzer.py"
        self.assertTrue(expected_script.is_file())

        async def fake_analyzer(*, url, output_file, analyzer_script, analyzer_python, korean):
            self.assertEqual(analyzer_script, expected_script)
            self.assertIsNone(korean)
            return url, output_file, 1, "test failure"

        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(fashion_analyzer, "_read_rows", return_value=[{"url": "https://www.instagram.com/reels/one/", "reaction_rate": "90"}]), \
             patch.object(fashion_analyzer, "_run_analyzer", new=fake_analyzer), \
             patch.object(fashion_analyzer, "ANALYZER_RETRY_DELAY_SECONDS", 0), \
             patch.object(fashion_analyzer, "_run_reel_insights", new=AsyncMock()) as insights:
            summary = asyncio.run(fashion_analyzer.analyze_qualifying_fashion_reels(temporary))
            self.assertEqual(summary["failed"], 1)
            insights.assert_awaited_once_with(Path(temporary).resolve(), repository)

    def test_failed_jobs_retry_three_times_and_stop_on_success(self) -> None:
        for succeeds_on in (2, 4, None):
            with self.subTest(succeeds_on=succeeds_on), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                calls = []
                async def run(*, url, output_file, **kwargs):
                    calls.append(url)
                    if len(calls) == succeeds_on:
                        output_file.write_text(json.dumps([{"reel_id": "one"}]), encoding="utf-8")
                        return url, output_file, 0, ""
                    return url, output_file, 1, "pool lock timeout"
                with patch.object(fashion_analyzer, "_read_rows", return_value=[{"url": "test", "reaction_rate": "90"}]), \
                     patch.object(fashion_analyzer, "_run_analyzer", run), \
                     patch.object(fashion_analyzer, "_run_reel_insights", AsyncMock()), \
                     patch.object(Path, "is_file", return_value=True), \
                     patch.object(fashion_analyzer.asyncio, "sleep", AsyncMock()) as sleep:
                    summary = asyncio.run(fashion_analyzer.analyze_qualifying_fashion_reels(root, repo_root=root))
                expected = succeeds_on or 4
                self.assertEqual(len(calls), expected)
                self.assertEqual(sleep.await_count, expected - 1)
                self.assertEqual([call.args[0] for call in sleep.await_args_list], [2.0, 4.0, 6.0][:expected-1])
                self.assertEqual(summary["failed"], int(succeeds_on is None))
                self.assertEqual(summary["succeeded"], int(succeeds_on is not None))
                state = json.loads((root / ".collector" / "fashion_analyzer_state.json").read_text(encoding="utf-8"))
                self.assertEqual(state["records"]["test"]["attempt_count"], expected)

    def test_reel_insights_uses_the_accumulated_analysis_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            script = repository / "analyzers" / "reel_insights.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            analysis = root / "reel_analysis.json"
            analysis.write_text("[]", encoding="utf-8")
            process = Mock(returncode=0)
            process.communicate = AsyncMock(return_value=(b"", b""))

            with patch.object(fashion_analyzer, "_analyzer_python", return_value="python"), \
                 patch.object(asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)) as create:
                asyncio.run(fashion_analyzer._run_reel_insights(root, repository))

            command = create.await_args.args
            self.assertEqual(command[:2], ("python", str(script)))
            self.assertIn(str(analysis), command)
            self.assertIn(str(root / "reel_insights.html"), command)

    def test_threshold_uses_ratio_and_deduplicates_to_latest_snapshot(self) -> None:
        rows = [
            {"url": "https://www.instagram.com/reels/one/", "reaction_rate": "89"},
            {"url": "https://www.instagram.com/reels/one/", "reaction_rate": "90", "collected_at": "2026-01-02"},
            {"url": "https://www.instagram.com/reels/two/", "reaction_rate": "9000%", "collected_at": "2026-01-01"},
        ]
        selected = fashion_analyzer.qualifying_reels(rows)
        self.assertEqual([row["url"] for row in selected], ["https://www.instagram.com/reels/one/", "https://www.instagram.com/reels/two/"])
        self.assertEqual(selected[1]["reaction_rate"], 90.0)

    def test_analyzer_results_are_saved_and_success_is_not_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / ".collector" / "reels_history_active.csv"
            history.parent.mkdir(parents=True)
            with history.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["url", "reaction_rate", "collected_at"])
                writer.writeheader()
                writer.writerow({"url": "https://www.instagram.com/reels/one/", "reaction_rate": "90", "collected_at": "2026-01-01"})

            async def fake_run_analyzer(*, url, output_file, analyzer_script, analyzer_python, korean):
                self.assertIsNone(korean)
                output_file.write_text(json.dumps([{"reel_id": "one", "analysis": {"summary": "ok"}}]), encoding="utf-8")
                return url, output_file, 0, ""

            with patch.object(fashion_analyzer, "_run_analyzer", fake_run_analyzer), \
                 patch.object(Path, "is_file", return_value=True), \
                 patch.object(fashion_analyzer, "_run_reel_insights", new=AsyncMock()):
                summary = asyncio.run(fashion_analyzer.analyze_qualifying_fashion_reels(root, repo_root=root))
                self.assertEqual(summary["succeeded"], 1)
                second = asyncio.run(fashion_analyzer.analyze_qualifying_fashion_reels(root, repo_root=root))
                self.assertEqual(second["queued"], 0)
            output = json.loads((root / "reel_analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(output[0]["reel_url"], "https://www.instagram.com/reels/one/")


if __name__ == "__main__":
    unittest.main()
