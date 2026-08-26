from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from .fashion_beauty_collection import (
    SupervisorLock,
    _due_retry_delay,
    decide_next_work,
    invoke_generic_collector,
    publish_dataset_outputs,
    run_fashion_beauty_collection,
)

from .fashion_beauty_scheduler import (
    BEAUTY_KEYWORDS,
    FASHION_KEYWORDS,
    SNAPSHOT_OFFSETS,
    DatasetConfig,
    RunConfig,
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

    def test_initial_count_ignores_malformed_url(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        rows = [{"url": "not a URL", "collection_number": "1", "collected_at": isoformat_utc(base)}]
        self.assertEqual(initial_count_in_window(rows, base, base + timedelta(hours=1)), 0)

    def test_initial_count_rejects_invalid_port_but_keeps_valid_url(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        rows = [
            {"url": "https://instagram.com:bad/reels/a/", "collection_number": "1", "collected_at": isoformat_utc(base)},
            {"url": "https://instagram.com/reels/b/", "collection_number": "1", "collected_at": isoformat_utc(base + timedelta(minutes=1))},
        ]
        self.assertEqual(initial_count_in_window(rows, base, base + timedelta(hours=1)), 1)


class ManualClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary_directory.name)
        self.config = RunConfig(
            data_root=self.data_root,
            duration_hours=1,
            discovery_hours=1,
            discovery_interval_minutes=30,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_history(self, dataset: str, rows: list[dict[str, object]]) -> Path:
        destination = self.data_root / ".datasets" / dataset / ".collector" / "reels_history_active.csv"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=["url", "collection_number", "collected_at"])
            writer.writeheader()
            writer.writerows(rows)
        return destination

    async def test_due_jobs_from_both_domains_run_before_active_window_discovery(self) -> None:
        started_at = datetime(2026, 8, 26, 0, 30, tzinfo=timezone.utc)
        initial_at = started_at - timedelta(minutes=30)
        config = RunConfig(
            data_root=self.data_root,
            duration_hours=0.5,
            discovery_hours=0.5,
            discovery_interval_minutes=30,
        )
        histories = {
            dataset: self.write_history(
                dataset,
                [{"url": f"https://www.instagram.com/reels/{dataset}/", "collection_number": 1, "collected_at": isoformat_utc(initial_at)}],
            )
            for dataset in ("fashion", "beauty")
        }
        clock = ManualClock(started_at)
        calls: list[dict[str, object]] = []
        lock_observations: list[tuple[bool, bool]] = []

        async def fake_invoke(**kwargs: object) -> int:
            calls.append(dict(kwargs))
            lock_observations.append(
                (
                    (self.data_root / ".datasets" / "fashion_beauty_scheduler.lock.json").exists(),
                    (self.data_root / ".datasets" / str(kwargs["dataset"]) / "collector.lock.json").exists(),
                )
            )
            if kwargs["mode"] == "recollect":
                history = histories[str(kwargs["dataset"])]
                with history.open("a", newline="", encoding="utf-8") as file:
                    csv.writer(file).writerow(
                        [kwargs["urls"][0], 2, isoformat_utc(started_at)]  # type: ignore[index]
                    )
            else:
                clock.current = started_at + timedelta(hours=config.duration_hours)
            return 0

        with patch(
            "collectors.fashion_beauty_collection.wait_for_stop_or_timeout",
            new=AsyncMock(return_value=False),
        ):
            result = await run_fashion_beauty_collection(config, invoke=fake_invoke, clock=clock)

        self.assertEqual(result, 0)
        self.assertEqual([call["mode"] for call in calls[:3]], ["recollect", "recollect", "discover"])
        self.assertEqual({call["dataset"] for call in calls[:2]}, {"fashion", "beauty"})
        self.assertEqual(calls[2]["dataset"], "fashion")
        self.assertEqual(calls[2]["max_items"], 50)
        self.assertTrue(all(supervisor and not generic for supervisor, generic in lock_observations))
        self.assertTrue((self.data_root / "fashion_collector_status.json").exists())
        self.assertTrue((self.data_root / "beauty_collector_status.json").exists())

    async def test_discovery_rechecks_window_and_deadlines_after_each_bounded_batch(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        clock = ManualClock(started_at)
        calls: list[dict[str, object]] = []

        async def fake_invoke(**kwargs: object) -> int:
            calls.append(dict(kwargs))
            if len(calls) == 1:
                clock.current = started_at + timedelta(minutes=31)
            else:
                clock.current = started_at + timedelta(hours=1)
            return 0

        with patch(
            "collectors.fashion_beauty_collection.wait_for_stop_or_timeout",
            new=AsyncMock(return_value=False),
        ):
            result = await run_fashion_beauty_collection(self.config, invoke=fake_invoke, clock=clock)

        self.assertEqual(result, 2)
        self.assertEqual(
            [(call["dataset"], call["max_items"]) for call in calls],
            [("fashion", 50), ("beauty", 50)],
        )
        fashion_status = json.loads((self.data_root / "fashion_collector_status.json").read_text(encoding="utf-8"))
        self.assertIn("deadline", fashion_status["last_error"].lower())

    async def test_fresh_malformed_supervisor_lock_is_not_stolen(self) -> None:
        lock_path = self.data_root / ".datasets" / "fashion_beauty_scheduler.lock.json"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_bytes(b"")

        with patch("collectors.fashion_beauty_collection.asyncio.sleep", new=AsyncMock()) as retry_sleep:
            with self.assertRaisesRegex(RuntimeError, "initializ"):
                await SupervisorLock(lock_path).acquire()

        self.assertTrue(lock_path.exists())
        self.assertEqual(lock_path.read_bytes(), b"")
        self.assertGreaterEqual(retry_sleep.await_count, 1)

    def test_due_retry_backoff_is_bounded_for_long_outages(self) -> None:
        self.assertEqual(
            [_due_retry_delay(attempt) for attempt in (1, 2, 3, 4, 5, 6, 20_000)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0],
        )

    async def test_nonzero_due_recollection_is_published_reported_and_retried_serially(self) -> None:
        started_at = datetime(2026, 8, 26, 0, 30, tzinfo=timezone.utc)
        history = self.write_history(
            "fashion",
            [{
                "url": "https://www.instagram.com/reels/retry/",
                "collection_number": 1,
                "collected_at": isoformat_utc(started_at - timedelta(minutes=30)),
            }],
        )
        workspace_export = self.data_root / ".datasets" / "fashion" / "reels.csv"
        workspace_export.write_text("fixture", encoding="utf-8")
        clock = ManualClock(started_at)
        attempts = 0
        active = 0
        max_active = 0
        waits: list[float] = []

        async def fake_invoke(**kwargs: object) -> int:
            nonlocal attempts, active, max_active
            attempts += 1
            active += 1
            max_active = max(max_active, active)
            active -= 1
            if attempts == 1:
                return 7
            with history.open("a", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(
                    [kwargs["urls"][0], 2, isoformat_utc(clock())]  # type: ignore[index]
                )
            clock.current = started_at + timedelta(hours=self.config.duration_hours)
            return 0

        async def fake_wait(_event: object, seconds: float) -> bool:
            waits.append(seconds)
            status = json.loads((self.data_root / "fashion_collector_status.json").read_text(encoding="utf-8"))
            self.assertIn("code 7", status["last_error"])
            self.assertEqual(status["collector_failures"], 1)
            self.assertEqual((self.data_root / "fashion_reels.csv").read_text(encoding="utf-8"), "fixture")
            clock.advance(seconds)
            return False

        with patch("collectors.fashion_beauty_collection.wait_for_stop_or_timeout", new=fake_wait):
            result = await run_fashion_beauty_collection(self.config, invoke=fake_invoke, clock=clock)

        self.assertEqual(result, 7)
        self.assertEqual(attempts, 2)
        self.assertEqual(max_active, 1)
        self.assertEqual(waits, [1.0])

    async def test_failed_due_job_backoff_does_not_delay_other_due_domain(self) -> None:
        started_at = datetime(2026, 8, 26, 0, 31, tzinfo=timezone.utc)
        histories = {
            "fashion": self.write_history(
                "fashion",
                [{
                    "url": "https://www.instagram.com/reels/fashion_retry/",
                    "collection_number": 1,
                    "collected_at": isoformat_utc(started_at - timedelta(minutes=31)),
                }],
            ),
            "beauty": self.write_history(
                "beauty",
                [{
                    "url": "https://www.instagram.com/reels/beauty_due/",
                    "collection_number": 1,
                    "collected_at": isoformat_utc(started_at - timedelta(minutes=30)),
                }],
            ),
        }
        clock = ManualClock(started_at)
        calls: list[str] = []

        async def fake_invoke(**kwargs: object) -> int:
            dataset = str(kwargs["dataset"])
            calls.append(dataset)
            if calls == ["fashion"]:
                return 7
            with histories[dataset].open("a", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(
                    [kwargs["urls"][0], 2, isoformat_utc(clock())]  # type: ignore[index]
                )
            if len(calls) == 3:
                clock.current = started_at + timedelta(hours=self.config.duration_hours)
            return 0

        async def fake_wait(_event: object, seconds: float) -> bool:
            clock.advance(seconds)
            return False

        with patch("collectors.fashion_beauty_collection.wait_for_stop_or_timeout", new=fake_wait):
            result = await run_fashion_beauty_collection(self.config, invoke=fake_invoke, clock=clock)

        self.assertEqual(result, 7)
        self.assertEqual(calls, ["fashion", "beauty", "fashion"])

    async def test_invocation_exception_is_reported_and_status_is_published(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        clock = ManualClock(started_at)

        async def fake_invoke(**_kwargs: object) -> int:
            clock.current = started_at + timedelta(hours=self.config.duration_hours)
            raise RuntimeError("deterministic collector failure")

        result = await run_fashion_beauty_collection(self.config, invoke=fake_invoke, clock=clock)

        self.assertEqual(result, 2)
        for dataset in ("fashion", "beauty"):
            status = json.loads((self.data_root / f"{dataset}_collector_status.json").read_text(encoding="utf-8"))
            if dataset == "fashion":
                self.assertIn("deterministic collector failure", status["last_error"])
                self.assertEqual(status["collector_failures"], 1)
            self.assertEqual(status["state"], "completed")

    async def test_negative_collector_result_stays_nonzero_for_discovery_and_recollection(self) -> None:
        started_at = datetime(2026, 8, 26, 0, 30, tzinfo=timezone.utc)
        for mode in ("discover", "recollect"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary_directory:
                data_root = Path(temporary_directory)
                config = RunConfig(
                    data_root=data_root,
                    duration_hours=0.5,
                    discovery_hours=0.5,
                    discovery_interval_minutes=30,
                )
                if mode == "recollect":
                    history = data_root / ".datasets" / "fashion" / ".collector" / "reels_history_active.csv"
                    history.parent.mkdir(parents=True)
                    with history.open("w", newline="", encoding="utf-8-sig") as file:
                        writer = csv.DictWriter(file, fieldnames=["url", "collection_number", "collected_at"])
                        writer.writeheader()
                        writer.writerow({
                            "url": "https://www.instagram.com/reels/negative/",
                            "collection_number": 1,
                            "collected_at": isoformat_utc(started_at - timedelta(minutes=30)),
                        })
                clock = ManualClock(started_at)

                async def fake_invoke(**_kwargs: object) -> int:
                    clock.current = started_at + timedelta(minutes=30)
                    return -9

                result = await run_fashion_beauty_collection(config, invoke=fake_invoke, clock=clock)

                self.assertEqual(result, -9)
                status = json.loads((data_root / "fashion_collector_status.json").read_text(encoding="utf-8"))
                self.assertIn("code -9", status["last_error"])
                self.assertEqual(status["collector_failures"], 1)

    async def test_idle_wait_is_clamped_to_run_deadline(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        clock = ManualClock(started_at)
        config = RunConfig(
            data_root=self.data_root,
            duration_hours=5 / 3600,
            discovery_hours=0,
        )
        waits: list[float] = []

        async def fake_wait(_event: object, seconds: float) -> bool:
            waits.append(seconds)
            clock.advance(seconds)
            return False

        with patch("collectors.fashion_beauty_collection.wait_for_stop_or_timeout", new=fake_wait):
            result = await run_fashion_beauty_collection(config, invoke=AsyncMock(), clock=clock)

        self.assertEqual(result, 0)
        self.assertEqual(waits, [5.0])

    async def test_window_cap_500_stops_discovery_but_keeps_due_jobs(self) -> None:
        now = datetime(2026, 8, 26, 0, 10, tzinfo=timezone.utc)
        rows = [
            {
                "url": f"https://www.instagram.com/reels/current{index}/",
                "collection_number": 1,
                "collected_at": isoformat_utc(now.replace(minute=0) + timedelta(seconds=index)),
            }
            for index in range(500)
        ]
        rows.append(
            {
                "url": "https://www.instagram.com/reels/due/",
                "collection_number": 1,
                "collected_at": isoformat_utc(now - timedelta(hours=1)),
            }
        )

        decision = decide_next_work(self.config, rows, now)

        self.assertFalse(decision.discover)
        self.assertEqual(decision.remaining_capacity, 0)
        self.assertEqual([job.url for job in decision.due_jobs], ["https://www.instagram.com/reels/due/"])

    async def test_generic_invocation_applies_discovery_only_options(self) -> None:
        captured: list[object] = []

        async def fake_run_collector(options: object) -> int:
            captured.append(options)
            if getattr(options, "urls_file", None):
                captured.append(Path(getattr(options, "urls_file")).read_text(encoding="utf-8"))
            return 0

        with patch("collectors.fashion_beauty_collection.run_collector", new=fake_run_collector):
            await invoke_generic_collector(
                config=self.config,
                dataset="fashion",
                mode="discover",
                hashtags=("패션", "ootd"),
                max_items=37,
            )
            await invoke_generic_collector(
                config=self.config,
                dataset="beauty",
                mode="recollect",
                urls=["https://www.instagram.com/reels/due/"],
            )

        discover = captured[0]
        recollect = captured[1]
        self.assertEqual(getattr(discover, "data_dir"), (self.data_root / ".datasets" / "fashion").resolve())
        self.assertEqual(getattr(discover, "hashtags"), ["패션", "ootd"])
        self.assertEqual(getattr(discover, "max_items"), 37)
        self.assertTrue(getattr(discover, "new_urls_only"))
        self.assertTrue(getattr(discover, "followers_after_reels"))
        self.assertEqual(getattr(discover, "max_upload_age_days"), 30)
        self.assertEqual(getattr(recollect, "data_dir"), (self.data_root / ".datasets" / "beauty").resolve())
        self.assertTrue(getattr(recollect, "disable_recollect_cooldown"))
        self.assertEqual(getattr(recollect, "max_upload_age_days"), 0)
        self.assertEqual(captured[2], "https://www.instagram.com/reels/due/\n")
        self.assertFalse(Path(getattr(recollect, "urls_file")).exists())

    def test_publish_creates_only_domain_named_exports_and_preserves_base_outputs(self) -> None:
        base_files = {
            name: f"base-{name}".encode()
            for name in ("reels.csv", "reels.json", "reels.xlsx", "users.csv", "users.xlsx")
        }
        for name, content in base_files.items():
            (self.data_root / name).write_bytes(content)
        workspace = self.data_root / ".datasets" / "fashion"
        workspace.mkdir(parents=True)
        source_files = {
            name: f"fashion-{name}".encode()
            for name in ("reels.csv", "reels.json", "reels.xlsx", "users.csv", "users.xlsx")
        }
        for name, content in source_files.items():
            (workspace / name).write_bytes(content)

        written = publish_dataset_outputs(DatasetConfig("fashion", workspace, FASHION_KEYWORDS))

        self.assertEqual(set(written), {"reels_csv", "reels_json", "reels_xlsx", "users_csv", "users_xlsx"})
        for name, content in base_files.items():
            self.assertEqual((self.data_root / name).read_bytes(), content)
        for name, content in source_files.items():
            self.assertEqual((self.data_root / f"fashion_{name}").read_bytes(), content)
        self.assertFalse(list(self.data_root.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
