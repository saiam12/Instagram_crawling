from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

from exporters.instagram_collector import write_xlsx_workbook
from scripts.instagram_reels_python import main as launcher_main, parse_fashion_command, parse_scheduled_command

from .fashion_beauty_collection import (
    SupervisorLock,
    _due_retry_delay,
    datasets,
    decide_next_work,
    invoke_generic_collector,
    publish_dataset_outputs,
    run_fashion_beauty_collection,
)

from .fashion_beauty_scheduler import (
    BEAUTY_KEYWORDS,
    FASHION_KEYWORDS,
    KEYWORDS_PER_WINDOW,
    SIX_HOUR_NEW_ONLY_KEYWORDS_PER_WINDOW,
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


def read_xlsx_rows(path: Path) -> list[list[str]]:
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as workbook:
        worksheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    return [
        ["".join(node.text or "" for node in cell.findall(f".//{namespace}t")) for cell in row.findall(f"{namespace}c")]
        for row in worksheet.findall(f".//{namespace}row")
    ]


class CommandTests(unittest.TestCase):
    def test_scheduled_commands_select_their_requested_domains(self) -> None:
        config = parse_fashion_command([])

        self.assertEqual(config.domains, ("fashion",))
        self.assertEqual(config.duration_hours, 16)
        self.assertEqual(config.discovery_hours, 8)
        self.assertEqual(config.new_items_per_window, 300)
        self.assertEqual(config.max_new_items_per_window, 300)
        self.assertEqual(config.keywords_per_window, 5)
        self.assertEqual(config.max_upload_age_days, 30)
        self.assertEqual(parse_scheduled_command("beauty", []).domains, ("beauty",))
        self.assertEqual(
            parse_scheduled_command("fashion-beauty", []).domains,
            ("fashion", "beauty"),
        )
        self.assertTrue(parse_scheduled_command("fashion", ["--background"]).background)
        self.assertEqual(parse_scheduled_command("beauty", ["--maxdays", "14"]).max_upload_age_days, 14)

    def test_discovery_must_end_eight_hours_before_run_end(self) -> None:
        with self.assertRaises(SystemExit):
            parse_fashion_command(["--duration-hours", "16", "--discovery-hours", "9"])

    def test_six_hour_new_only_preset_uses_shared_standard_outputs(self) -> None:
        config = parse_scheduled_command("fashion-beauty", ["--six-hour-new-only"])

        self.assertEqual(config.duration_hours, 6)
        self.assertEqual(config.discovery_hours, 6)
        self.assertTrue(config.new_only)
        self.assertTrue(config.base_output)
        self.assertEqual(config.max_upload_age_days, 365)
        self.assertEqual(config.new_items_per_window, 250)
        self.assertEqual(config.max_new_items_per_window, 250)
        self.assertEqual(config.keywords_per_window, SIX_HOUR_NEW_ONLY_KEYWORDS_PER_WINDOW)
        self.assertEqual(config.fashion_keywords, FASHION_KEYWORDS)
        self.assertEqual(config.beauty_keywords, BEAUTY_KEYWORDS)
        self.assertTrue(all(dataset.data_root == config.data_root for dataset in datasets(config)))

    def test_six_hour_new_only_preset_requires_both_domains(self) -> None:
        with self.assertRaises(SystemExit):
            parse_scheduled_command("fashion", ["--six-hour-new-only"])

    def test_custom_discovery_interval_changes_the_active_domain(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        config = parse_fashion_command(["--discovery-interval-minutes", "15"])

        self.assertEqual(
            window_dataset(
                started_at,
                started_at + timedelta(minutes=15),
                config.discovery_interval_minutes,
            ),
            "beauty",
        )

    def test_custom_hashtag_query_replaces_the_domain_keywords(self) -> None:
        config = parse_fashion_command([
            "--fashion-hashtag-query",
            "runway OR streetwear",
        ])

        self.assertEqual(keyword_group(config.fashion_keywords, 1), ("runway", "streetwear"))
        self.assertEqual(config.beauty_keywords, BEAUTY_KEYWORDS)

    def test_fashion_rejects_unsafe_numeric_options(self) -> None:
        invalid_arguments = (
            ["--duration-hours", "nan"],
            ["--discovery-hours", "0"],
            ["--discovery-interval-minutes", "inf"],
            ["--new-items-per-window", "0"],
            ["--max-new-items-per-window", "301"],
            ["--new-items-per-window", "51", "--max-new-items-per-window", "50"],
            ["--max-upload-age-days", "-1"],
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                parse_fashion_command(arguments)

    def test_fashion_launcher_dispatches_without_running_the_real_collector(self) -> None:
        with patch(
            "scripts.instagram_reels_python.run_fashion_beauty_collection",
            new=AsyncMock(return_value=0),
        ):
            result = launcher_main(["fashion"])

        self.assertEqual(result, 0)


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
        self.assertEqual(len(keyword_group(FASHION_KEYWORDS, 3)), KEYWORDS_PER_WINDOW)

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
        self.assertEqual(
            keyword_group(FASHION_KEYWORDS, 1),
            tuple(FASHION_KEYWORDS[:KEYWORDS_PER_WINDOW]),
        )
        self.assertEqual(
            keyword_group(FASHION_KEYWORDS, 11),
            tuple(FASHION_KEYWORDS[:KEYWORDS_PER_WINDOW]),
        )

    def test_five_keyword_groups_advance_the_start_keyword_and_cycle(self) -> None:
        size = SIX_HOUR_NEW_ONLY_KEYWORDS_PER_WINDOW
        self.assertEqual(keyword_group(FASHION_KEYWORDS, 1, size), tuple(FASHION_KEYWORDS[:size]))
        self.assertEqual(keyword_group(FASHION_KEYWORDS, 2, size), tuple(FASHION_KEYWORDS[size : size * 2]))
        self.assertEqual(keyword_group(FASHION_KEYWORDS, 10, size), tuple(FASHION_KEYWORDS[size * 9 :]))
        self.assertEqual(keyword_group(FASHION_KEYWORDS, 11, size), tuple(FASHION_KEYWORDS[:size]))

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
        self.assertEqual(calls[2]["max_items"], 300)
        self.assertTrue(all(supervisor and not generic for supervisor, generic in lock_observations))
        self.assertTrue((self.data_root / "fashion_collector_status.json").exists())
        self.assertTrue((self.data_root / "beauty_collector_status.json").exists())

    async def test_new_only_mode_skips_existing_due_jobs_and_discovers(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        base_history = self.data_root / ".collector" / "reels_history_active.csv"
        base_history.parent.mkdir(parents=True, exist_ok=True)
        with base_history.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=["url", "collection_number", "collected_at"])
            writer.writeheader()
            writer.writerow({
                "url": "https://www.instagram.com/reels/due/",
                "collection_number": 1,
                "collected_at": isoformat_utc(started_at - timedelta(minutes=30)),
            })
        config = RunConfig(
            data_root=self.data_root,
            duration_hours=0.5,
            discovery_hours=0.5,
            discovery_interval_minutes=30,
            new_only=True,
            base_output=True,
        )
        clock = ManualClock(started_at)
        calls: list[dict[str, object]] = []

        async def fake_invoke(**kwargs: object) -> int:
            calls.append(dict(kwargs))
            clock.current = started_at + timedelta(minutes=30)
            return 0

        result = await run_fashion_beauty_collection(config, invoke=fake_invoke, clock=clock)

        self.assertEqual(result, 0)
        self.assertEqual([call["mode"] for call in calls], ["discover"])
        self.assertEqual(calls[0]["dataset"], "fashion")
        self.assertTrue((self.data_root / "fashion_collector_status.json").exists())
        self.assertTrue((self.data_root / "beauty_collector_status.json").exists())

    async def test_new_only_mode_processes_a_keyword_group_once_before_advancing_to_the_next_window(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        clock = ManualClock(started_at)
        config = RunConfig(
            data_root=self.data_root,
            duration_hours=1,
            discovery_hours=1,
            discovery_interval_minutes=30,
            new_items_per_window=600,
            max_new_items_per_window=600,
            new_only=True,
            domains=("fashion",),
        )
        calls: list[dict[str, object]] = []

        async def fake_invoke(**kwargs: object) -> int:
            calls.append(dict(kwargs))
            return 0

        async def fake_wait(_event: object, seconds: float) -> bool:
            clock.advance(seconds)
            return False

        with patch("collectors.fashion_beauty_collection.wait_for_stop_or_timeout", new=fake_wait):
            result = await run_fashion_beauty_collection(config, invoke=fake_invoke, clock=clock)

        self.assertEqual(result, 0)
        self.assertEqual([call["max_items"] for call in calls], [600, 600])
        self.assertEqual(calls[0]["hashtags"], tuple(FASHION_KEYWORDS[:KEYWORDS_PER_WINDOW]))
        self.assertEqual(
            calls[1]["hashtags"],
            tuple(FASHION_KEYWORDS[KEYWORDS_PER_WINDOW : KEYWORDS_PER_WINDOW * 2]),
        )

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
            [("fashion", 300), ("beauty", 300)],
        )
        fashion_status = json.loads((self.data_root / "fashion_collector_status.json").read_text(encoding="utf-8"))
        self.assertIn("deadline", fashion_status["last_error"].lower())

    async def test_fashion_only_discovers_every_window(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        clock = ManualClock(started_at)
        config = RunConfig(
            data_root=self.data_root,
            duration_hours=1,
            discovery_hours=1,
            discovery_interval_minutes=30,
            domains=("fashion",),
        )
        calls: list[dict[str, object]] = []

        async def fake_invoke(**kwargs: object) -> int:
            calls.append(dict(kwargs))
            clock.current += timedelta(minutes=31)
            return 0

        result = await run_fashion_beauty_collection(config, invoke=fake_invoke, clock=clock)

        self.assertEqual([call["dataset"] for call in calls], ["fashion", "fashion"])
        self.assertEqual(calls[0]["hashtags"], tuple(FASHION_KEYWORDS[:KEYWORDS_PER_WINDOW]))
        self.assertEqual(
            calls[1]["hashtags"],
            tuple(FASHION_KEYWORDS[KEYWORDS_PER_WINDOW : KEYWORDS_PER_WINDOW * 2]),
        )

    async def test_discovery_progress_is_carried_across_retries_in_one_window(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        clock = ManualClock(started_at)
        config = RunConfig(
            data_root=self.data_root,
            duration_hours=1,
            discovery_hours=1,
            discovery_interval_minutes=30,
            domains=("fashion",),
        )
        history = self.data_root / ".datasets" / "fashion" / ".collector" / "reels_history_active.csv"
        calls: list[dict[str, object]] = []

        async def fake_invoke(**kwargs: object) -> int:
            calls.append(dict(kwargs))
            if len(calls) == 1:
                history.parent.mkdir(parents=True)
                with history.open("w", newline="", encoding="utf-8-sig") as file:
                    writer = csv.DictWriter(file, fieldnames=["url", "collection_number", "collected_at"])
                    writer.writeheader()
                    writer.writerow({
                        "url": "https://www.instagram.com/reels/progress/",
                        "collection_number": 1,
                        "collected_at": isoformat_utc(started_at),
                    })
            else:
                clock.current = started_at + timedelta(hours=1)
            return 0

        await run_fashion_beauty_collection(config, invoke=fake_invoke, clock=clock)

        self.assertEqual([call["progress_offset"] for call in calls], [0, 1])

    async def test_worker_stop_request_stops_the_supervisor_without_a_restart(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        clock = ManualClock(started_at)
        calls: list[str] = []

        async def fake_invoke(**kwargs: object) -> int:
            calls.append(str(kwargs["dataset"]))
            kwargs["stop_event"].set()  # type: ignore[index]
            return 0

        result = await run_fashion_beauty_collection(self.config, invoke=fake_invoke, clock=clock)

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["fashion"])

    async def test_overdue_snapshots_from_a_prior_run_are_not_recollected_immediately(self) -> None:
        started_at = datetime(2026, 8, 27, tzinfo=timezone.utc)
        clock = ManualClock(started_at)
        config = RunConfig(
            data_root=self.data_root,
            duration_hours=1,
            discovery_hours=1,
            discovery_interval_minutes=30,
            domains=("fashion",),
        )
        self.write_history(
            "fashion",
            [{
                "url": "https://www.instagram.com/reels/prior-run/",
                "collection_number": 1,
                "collected_at": isoformat_utc(started_at - timedelta(hours=12)),
            }],
        )
        calls: list[dict[str, object]] = []

        async def fake_invoke(**kwargs: object) -> int:
            calls.append(dict(kwargs))
            kwargs["stop_event"].set()  # type: ignore[index]
            return 0

        await run_fashion_beauty_collection(config, invoke=fake_invoke, clock=clock)

        self.assertEqual([(call["dataset"], call["mode"]) for call in calls], [("fashion", "discover")])

    async def test_fashion_beauty_publishes_each_domain_to_its_own_outputs(self) -> None:
        started_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        clock = ManualClock(started_at)
        config = RunConfig(
            data_root=self.data_root,
            duration_hours=1,
            discovery_hours=1,
            discovery_interval_minutes=30,
            domains=("fashion", "beauty"),
        )
        calls: list[str] = []

        async def fake_invoke(**kwargs: object) -> int:
            dataset = str(kwargs["dataset"])
            calls.append(dataset)
            workspace = self.data_root / ".datasets" / dataset
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "reels.csv").write_text(f"{dataset}-only", encoding="utf-8")
            clock.current += timedelta(minutes=31)
            return 0

        await run_fashion_beauty_collection(config, invoke=fake_invoke, clock=clock)

        self.assertEqual(calls, ["fashion", "beauty"])
        self.assertEqual((self.data_root / "fashion_reels.csv").read_text(encoding="utf-8"), "fashion-only")
        self.assertEqual((self.data_root / "beauty_reels.csv").read_text(encoding="utf-8"), "beauty-only")

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
            with (self.data_root / "fashion_reels.csv").open("r", newline="", encoding="utf-8-sig") as file:
                published_rows = list(csv.DictReader(file))
            self.assertEqual([row["collection_number"] for row in published_rows], ["1"])
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

    async def test_window_cap_300_stops_discovery_but_keeps_due_jobs(self) -> None:
        now = datetime(2026, 8, 26, 0, 10, tzinfo=timezone.utc)
        rows = [
            {
                "url": f"https://www.instagram.com/reels/current{index}/",
                "collection_number": 1,
                "collected_at": isoformat_utc(now.replace(minute=0) + timedelta(seconds=index)),
            }
            for index in range(300)
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
                config=replace(self.config, background=True),
                dataset="fashion",
                mode="discover",
                hashtags=("패션", "ootd"),
                max_items=37,
                progress_offset=4,
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
        self.assertEqual(getattr(discover, "progress_offset"), 4)
        self.assertTrue(getattr(discover, "background"))
        self.assertEqual(getattr(discover, "direct_reel_info_wait_seconds"), 3)
        self.assertEqual(getattr(discover, "exact_metric_attempts"), 3)
        self.assertEqual(getattr(discover, "exact_metric_retry_delay_seconds"), 2)
        self.assertEqual(getattr(discover, "hashtag_candidates_per_keyword"), 50)
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

    def test_publish_keeps_each_recollection_as_a_separate_reel_row(self) -> None:
        workspace = self.data_root / ".datasets" / "fashion"
        history = workspace / ".collector" / "reels_history_active.csv"
        history.parent.mkdir(parents=True)
        fields = ["collection_number", "days_since_previous", "collected_at", "url", "view_count"]
        rows = [
            ["1", "", "2026-08-26T00:00:00Z", "https://www.instagram.com/reels/row-test/", "100"],
            ["2", "+0.1day", "2026-08-26T00:30:00Z", "https://www.instagram.com/reels/row-test/", "110"],
        ]
        with history.open("w", newline="", encoding="utf-8-sig") as file:
            csv.writer(file).writerows([fields, *rows])

        publish_dataset_outputs(DatasetConfig("fashion", workspace, FASHION_KEYWORDS))

        with (self.data_root / "fashion_reels.csv").open("r", newline="", encoding="utf-8-sig") as file:
            published_rows = list(csv.DictReader(file))
        published_json = json.loads((self.data_root / "fashion_reels.json").read_text(encoding="utf-8"))
        workbook_rows = read_xlsx_rows(self.data_root / "fashion_reels.xlsx")

        self.assertEqual([row["collection_number"] for row in published_rows], ["1", "2"])
        self.assertEqual([row["view_count"] for row in published_rows], ["100", "110"])
        self.assertEqual(published_rows[1]["hours_since_previous"], "+0.5hour")
        self.assertEqual([row["collection_number"] for row in published_json], ["1", "2"])
        self.assertEqual(len(workbook_rows), 3)

    def test_publish_projects_fashion_and_beauty_intervals_to_hours_only(self) -> None:
        base_files = {
            "reels.csv": b"baseline reels csv",
            "reels.json": b"baseline reels json",
            "reels.xlsx": b"baseline reels xlsx",
            "users.csv": b"baseline users csv",
            "users.xlsx": b"baseline users xlsx",
        }
        for name, content in base_files.items():
            (self.data_root / name).write_bytes(content)

        reels_fields = [
            "collection_number", "collected_at", "url",
            "2nd collect_collected_at", "2nd collect_days_since_previous",
        ]
        reels_row = [
            "2", "2026-08-26T00:00:00Z", "https://www.instagram.com/reels/hour-test/",
            "2026-08-26T00:30:00Z", "+0.1day",
        ]
        users_fields = [
            "collection_number", "days_since_previous", "user_id", "username",
            "biography", "profile_category", "post_count", "follower_count", "follower_count_change", "collected_at",
        ]
        users_rows = [
            ["1", "", "42", "hour_user", "profile", "의류(브랜드)", "2078", "100", "", "2026-08-26T00:00:00Z"],
            ["2", "+0.2day", "42", "hour_user", "profile", "의류(브랜드)", "2080", "110", "10", "2026-08-26T04:00:00Z"],
        ]

        for dataset_name, keywords in (("fashion", FASHION_KEYWORDS), ("beauty", BEAUTY_KEYWORDS)):
            with self.subTest(dataset=dataset_name):
                workspace = self.data_root / ".datasets" / dataset_name
                workspace.mkdir(parents=True)
                with (workspace / "reels.csv").open("w", newline="", encoding="utf-8-sig") as file:
                    csv.writer(file).writerows([reels_fields, reels_row])
                (workspace / "reels.json").write_text(
                    json.dumps([dict(zip(reels_fields, reels_row))]), encoding="utf-8"
                )
                write_xlsx_workbook(workspace / "reels.xlsx", [("reels", [reels_fields, reels_row])])
                with (workspace / "users.csv").open("w", newline="", encoding="utf-8-sig") as file:
                    csv.writer(file).writerows([users_fields, *users_rows])
                write_xlsx_workbook(workspace / "users.xlsx", [("users", [users_fields, *users_rows])])

                publish_dataset_outputs(DatasetConfig(dataset_name, workspace, keywords))

                with (self.data_root / f"{dataset_name}_reels.csv").open("r", newline="", encoding="utf-8-sig") as file:
                    published_reels = list(csv.DictReader(file))
                with (self.data_root / f"{dataset_name}_users.csv").open("r", newline="", encoding="utf-8-sig") as file:
                    published_users = list(csv.DictReader(file))
                published_json = json.loads((self.data_root / f"{dataset_name}_reels.json").read_text(encoding="utf-8"))
                reel_xlsx = read_xlsx_rows(self.data_root / f"{dataset_name}_reels.xlsx")
                user_xlsx = read_xlsx_rows(self.data_root / f"{dataset_name}_users.xlsx")

                self.assertEqual(published_reels[0]["2nd collect_hours_since_previous"], "+0.5hour")
                self.assertNotIn("2nd collect_days_since_previous", published_reels[0])
                self.assertEqual(published_json[0]["2nd collect_hours_since_previous"], "+0.5hour")
                self.assertEqual(published_users[0]["hours_since_previous"], "")
                self.assertEqual(published_users[1]["hours_since_previous"], "+4hour")
                self.assertNotIn("days_since_previous", published_users[1])
                self.assertIn("2nd collect_hours_since_previous", reel_xlsx[0])
                self.assertIn("+0.5hour", reel_xlsx[1])
                self.assertIn("hours_since_previous", user_xlsx[0])
                self.assertIn("+4hour", user_xlsx[2])

        for name, content in base_files.items():
            self.assertEqual((self.data_root / name).read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
