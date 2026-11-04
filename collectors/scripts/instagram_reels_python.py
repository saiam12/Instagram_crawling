"""Python-only launcher for collection, refresh, follower update, and XLSX sync."""

from __future__ import annotations

import asyncio
import argparse
import json
import math
import os
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import ParseError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.reels.collection_log import append_collection_log
from src.reels.process_lock import AtomicProcessLock, process_is_alive
from src.reels.instagram_reels_browser import main as collector_main, parse_hashtag_query, reconcile_reel_exports
from src.reels.android_metric_worker import run_android_metric_worker
from src.reels.fashion_beauty_collection import (  # noqa: E402
    _COLLECTOR_RETRY_MAX_ATTEMPTS,
    _collector_access_failure,
    _collector_rate_limit_delay,
    _due_retry_delay,
    publish_dataset_outputs,
    read_history,
    run_fashion_beauty_collection,
)
from src.reels.fashion_beauty_scheduler import (  # noqa: E402
    BEAUTY_KEYWORDS,
    FASHION_KEYWORDS,
    DEFAULT_HASHTAG_CANDIDATES,
    RunConfig,
    DatasetConfig,
    _normalize_reel_url,
    _parse_timestamp,
)
from src.reels.instagram_hashtag_search import collect_hashtag_count_report  # noqa: E402
from src.exporters.instagram_collector import DataStore, read_reel_urls_from_xlsx, protect_refresh_workbook  # noqa: E402
from src.reels.collector_modes import (  # noqa: E402
    MODE_COMMANDS,
    extract_collector_mode,
)


USAGE = """usage: instagram_reels_python.py {collect,refresh,followers,xlsx,reconcile,android-worker,hashtag-posts,fashion,beauty,fashion-beauty} [collector options]

commands:
  collect     collect new Reels
  refresh     recollect Reel URLs from reels.xlsx
  followers   update saved users' follower counts (or verify account locations only)
  xlsx        synchronize CSV files into collection_data.xlsx or refresh_data.xlsx
  reconcile   merge reels_updated.xlsx and rebuild public exports
  android-worker  drain the durable Android metric queue (normally started automatically)
  hashtag-posts   collect Android related tags and replace compact counts with exact web counts when available
  fashion         run the Fashion-only scheduled collection
  beauty          run the Beauty-only scheduled collection
  fashion-beauty  alternate Fashion and Beauty every discovery window

All options after the command are passed to the Python collector. Examples:
  python scripts/instagram_reels_python.py collect --max-items 50 --background
  python scripts/instagram_reels_python.py fashion --collector-mode web --background
  python scripts/instagram_reels_python.py fashion --collector-mode hybrid --background
  python scripts/instagram_reels_python.py refresh --background
  python scripts/instagram_reels_python.py followers
  python scripts/instagram_reels_python.py followers --account-country-only --data-dir collector/.datasets/fashion
  python scripts/instagram_reels_python.py reconcile
  python scripts/instagram_reels_python.py android-worker --data-dir collector
  python scripts/instagram_reels_python.py hashtag-posts --hashtag-query '오오티디 OR 패션'
  python scripts/instagram_reels_python.py hashtag-posts --preset fashion-beauty
  python scripts/instagram_reels_python.py fashion
  python scripts/instagram_reels_python.py beauty
  python scripts/instagram_reels_python.py fashion-beauty
"""


SCHEDULED_COMMAND_DOMAINS = {
    "fashion": ("fashion",),
    "beauty": ("beauty",),
    "fashion-beauty": ("fashion", "beauty"),
}


HASHTAG_POST_PRESETS = {
    "fashion": tuple(FASHION_KEYWORDS),
    "beauty": tuple(BEAUTY_KEYWORDS),
    "fashion-beauty": tuple(dict.fromkeys((*FASHION_KEYWORDS, *BEAUTY_KEYWORDS))),
}


def option_value(arguments: list[str], name: str, default: str) -> str:
    value = default
    for index, argument in enumerate(arguments):
        if argument == name and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif argument.startswith(f"{name}="):
            value = argument.split("=", 1)[1]
    return value


def _finite_positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def _finite_nonnegative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than or equal to zero")
    return number


def _positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be an integer greater than zero")
    return number


def parse_scheduled_command(
    command: str,
    arguments: list[str],
    *,
    collector_mode: str = "hybrid",
) -> RunConfig:
    if collector_mode == "hybrid":
        detected_mode, remaining = extract_collector_mode(arguments)
        if detected_mode != "hybrid" or len(remaining) != len(arguments):
            collector_mode = detected_mode
            arguments = remaining
    domains = SCHEDULED_COMMAND_DOMAINS[command]
    parser = argparse.ArgumentParser(
        prog=f"instagram_reels_python.py {command}",
        description="Run a scheduled Fashion and/or Beauty collection.",
        epilog="Mode: --collector-mode {hybrid,web} (default: hybrid).",
    )
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data" / "collector")
    parser.add_argument("--duration-hours", type=_finite_nonnegative, default=16, help="Maximum runtime in hours (default: 16); 0 means no time limit.")
    parser.add_argument("--target-snapshots", type=_positive_integer, default=4)
    parser.add_argument("--resume-started-at", help="Resume a scheduled run from its original UTC start timestamp.")
    parser.add_argument("--discovery-hours", type=_finite_positive)
    parser.add_argument("--recollection-interval-minutes", type=_finite_positive, default=240)
    parser.add_argument(
        "--hashtag-candidates-per-keyword",
        type=_positive_integer,
        default=DEFAULT_HASHTAG_CANDIDATES,
        help="Reel URL candidates to inspect per hashtag (default: 200).",
    )
    parser.add_argument("--max-upload-days", dest="max_upload_age_days", type=_finite_nonnegative, default=30)
    parser.add_argument("--maxdays", dest="max_upload_age_days", type=_finite_nonnegative, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--discovery-interval-minutes", type=_finite_positive, default=30)
    parser.add_argument("--background", action="store_true", help="Use the saved Instagram login without showing a browser window.")
    parser.add_argument("--android-adb-path", type=Path, help="Android SDK platform-tools adb.exe path.")
    parser.add_argument("--android-device-id", help="ADB serial when more than one Android device is online.")
    parser.add_argument(
        "--collect-hashtag-media-count",
        action="store_true",
        help="Collect hashtag media_count through Android Tags search during discovery.",
    )
    parser.add_argument("--direct-reel-info-wait-seconds", type=_finite_nonnegative, default=3, help=argparse.SUPPRESS)
    parser.add_argument("--exact-metric-attempts", type=_positive_integer, default=3)
    parser.add_argument("--exact-metric-retry-delay-seconds", type=_finite_nonnegative, default=2)
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Collect only newly discovered Reels for the entire run; do not schedule or run recollections.",
    )
    parser.add_argument(
        "--base-output",
        dest="base_output",
        action="store_true",
        help="Write directly to the existing data directory's reels.* and users.* files instead of domain-prefixed files.",
    )
    parser.add_argument("--shared-data", dest="base_output", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--six-hour-new-only",
        action="store_true",
        help="Fashion+Beauty preset: 6 hours of new-only collection, 365-day uploads, and the standard reels.* and users.* outputs.",
    )
    parser.add_argument(
        "--test-single-hashtag",
        action="store_true",
        help="Testing only: collect the first configured hashtag for each active domain, without keyword rotation.",
    )
    parser.add_argument("--fashion-hashtag-query", help="Replace Fashion search keywords (fashion or fashion-beauty).")
    parser.add_argument("--beauty-hashtag-query")
    if collector_mode not in {"hybrid", "web"}:
        parser.error(f"Scheduled browser collection does not support collector mode: {collector_mode}")
    if collector_mode == "web" and "--collect-hashtag-media-count" in arguments:
        parser.error("--collect-hashtag-media-count requires hybrid collector mode")
    parser.add_argument("--test", action="store_true", help="At most 10 new Reels, 5-minute recollections, 20-minute run.")
    options = parser.parse_args(arguments)
    if options.test:
        if options.six_hour_new_only or options.new_only or options.duration_hours == 0:
            parser.error("--test cannot be combined with new-only presets or unlimited duration")
        options.duration_hours = 20 / 60
        options.recollection_interval_minutes = 5
        options.target_snapshots = 4
        options.discovery_hours = options.duration_hours - (options.target_snapshots - 1) * options.recollection_interval_minutes / 60
        if not any(arg.split("=", 1)[0] == "--data-dir" for arg in arguments):
            options.data_dir = PROJECT_ROOT / "data_test"
    if options.six_hour_new_only:
        if options.duration_hours == 0:
            parser.error("--six-hour-new-only cannot be combined with --duration-hours 0")
        if command != "fashion-beauty":
            parser.error("--six-hour-new-only can only be used with fashion-beauty")
        options.duration_hours = 6
        options.discovery_hours = 6
        options.new_only = True
        options.base_output = True
        options.max_upload_age_days = 365
    if options.exact_metric_attempts > 5:
        parser.error("--exact-metric-attempts cannot exceed 5")
    if options.duration_hours == 0:
        options.duration_hours = None
    elif options.new_only:
        # New-only runs have no post-discovery recollection period, so the
        # complete duration is an active discovery period.
        options.discovery_hours = options.duration_hours
    else:
        recollection_tail_hours = options.recollection_interval_minutes * (options.target_snapshots - 1) / 60
        max_discovery_hours = options.duration_hours - recollection_tail_hours
        if max_discovery_hours <= 0:
            parser.error("--duration-hours must exceed the recollection period unless --new-only is used")
        if options.discovery_hours is None:
            options.discovery_hours = max_discovery_hours
        elif options.discovery_hours > max_discovery_hours:
            parser.error("--discovery-hours cannot exceed --duration-hours minus the recollection period")

    try:
        fashion_keywords = (
            tuple(parse_hashtag_query(options.fashion_hashtag_query))
            if options.fashion_hashtag_query is not None
            else FASHION_KEYWORDS
        )
        beauty_keywords = (
            tuple(parse_hashtag_query(options.beauty_hashtag_query))
            if options.beauty_hashtag_query is not None
            else BEAUTY_KEYWORDS
        )
    except ValueError as error:
        parser.error(str(error))
    if "fashion" in domains and not fashion_keywords:
        parser.error("--fashion-hashtag-query must contain at least one hashtag")
    if "beauty" in domains and not beauty_keywords:
        parser.error("--beauty-hashtag-query must contain at least one hashtag")
    if "fashion" not in domains and options.fashion_hashtag_query is not None:
        parser.error("--fashion-hashtag-query can only be used with fashion or fashion-beauty")
    if "beauty" not in domains and options.beauty_hashtag_query is not None:
        parser.error("--beauty-hashtag-query can only be used with beauty or fashion-beauty")

    if not fashion_keywords or not beauty_keywords:
        parser.error("custom hashtag queries must contain at least one hashtag")
    if options.test_single_hashtag:
        # Testing only: pin the keyword list to its first hashtag.
        if "fashion" in domains:
            fashion_keywords = fashion_keywords[:1]
        if "beauty" in domains:
            beauty_keywords = beauty_keywords[:1]

    resume_started_at = None
    if options.resume_started_at:
        try:
            resume_started_at = datetime.fromisoformat(options.resume_started_at.replace("Z", "+00:00"))
        except ValueError:
            parser.error("--resume-started-at must be an ISO 8601 timestamp with a timezone")
        if resume_started_at.tzinfo is None:
            parser.error("--resume-started-at must include a timezone")
        resume_started_at = resume_started_at.astimezone(timezone.utc)

    android_adb_path = options.android_adb_path
    configured_adb = os.environ.get("INSTAGRAM_ANDROID_ADB_PATH", "").strip()
    if android_adb_path is None and configured_adb:
        android_adb_path = Path(os.path.expandvars(configured_adb.strip('"'))).expanduser()
    if android_adb_path is not None:
        android_adb_path = android_adb_path.resolve()

    return RunConfig(
        data_root=options.data_dir.resolve(),
        resume_started_at=resume_started_at,
        duration_hours=options.duration_hours,
        test_mode=options.test,
        target_snapshots=options.target_snapshots,
        discovery_hours=options.discovery_hours,
        recollection_interval_minutes=options.recollection_interval_minutes,
        discovery_interval_minutes=options.discovery_interval_minutes,
        hashtag_candidates_per_keyword=options.hashtag_candidates_per_keyword,
        max_upload_age_days=options.max_upload_age_days,
        background=options.background,
        collect_hashtag_media_count=options.collect_hashtag_media_count,
        direct_reel_info_wait_seconds=options.direct_reel_info_wait_seconds,
        exact_metric_attempts=options.exact_metric_attempts,
        exact_metric_retry_delay_seconds=options.exact_metric_retry_delay_seconds,
        new_only=options.new_only,
        base_output=options.base_output,
        fashion_keywords=fashion_keywords,
        beauty_keywords=beauty_keywords,
        domains=domains,
        collector_mode=collector_mode,
        android_adb_path=android_adb_path,
        android_device_id=options.android_device_id,
    )


def parse_fashion_command(arguments: list[str]) -> RunConfig:
    """Backward-compatible parser entry point for the fashion-only command."""
    collector_mode, remaining = extract_collector_mode(arguments)
    return parse_scheduled_command("fashion", remaining, collector_mode=collector_mode)


def sync_xlsx(data_dir: Path) -> int:
    store = DataStore(data_dir)
    try:
        store.sync_xlsx()
        print(f"XLSX saved: {store.workbook}")
    except PermissionError:
        updated = store.workbook.with_stem(f"{store.workbook.stem}_updated")
        store.sync_xlsx(updated)
        print(f"{store.workbook.name} is open. The latest data was saved to: {updated}", file=sys.stderr)
    return 0


def refresh_url_state(rows: list[dict], urls: list[str]) -> dict[str, tuple[int, datetime | None]]:
    state = {url: (0, None) for url in urls}
    timestamps: dict[str, set[datetime]] = {url: set() for url in urls}
    for row in rows:
        url = _normalize_reel_url(row.get("url"))
        if url not in state:
            continue
        timestamp = _parse_timestamp(row.get("collected_at"))
        if timestamp is None:
            raise ValueError(f"Missing or invalid collected_at for {url}; cannot schedule refresh safely.")
        timestamps[url].add(timestamp)
        try:
            number = int(row.get("collection_number") or 1)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid collection_number for {url}")
        count, last = state[url]
        state[url] = (max(count, number, len(timestamps[url])), max(last, timestamp) if last else timestamp)
    return state


def refresh_age_timing(uploaded: datetime, last: datetime | None, now: datetime, intervals: tuple[float, ...], *, schedule_basis: str = "uploaded", schedule_origin: datetime | None = None) -> tuple[datetime, datetime] | None:
    """Return collection time and next wake-up; re-evaluate at every age boundary."""
    for boundary, hours in zip((24, 96, 120, 168), intervals):
        end = uploaded + timedelta(hours=boundary)
        if now < end:
            due = refresh_due_at(schedule_origin or uploaded, last, now, hours, schedule_basis)
            due = max(uploaded, due)
            return due, min(due, end)
    return None


def refresh_due_at(uploaded: datetime, last: datetime | None, now: datetime, hours: float, basis: str) -> datetime:
    interval = timedelta(hours=hours)
    if basis == "uploaded":
        # Missed slots trigger one current measurement; never fabricate past snapshots.
        elapsed = max(timedelta(0), (last or uploaded) - uploaded)
        return uploaded + (elapsed // interval + 1) * interval
    if basis == "last-collected":
        return last + interval if last else now
    raise ValueError(f"Unknown refresh schedule basis: {basis}")


def refresh_peak_hourly_cost(interval_hours, age_intervals, test_mode):
    cost = max(1, math.ceil(1 / (min(age_intervals) if age_intervals else interval_hours)))
    return min(3, cost) if test_mode else cost


@contextmanager
def refresh_runtime_record(data_root, options, age_intervals):
    from src.reels.refresh_inbox import atomic_bytes
    path = Path(data_root) / '.collector' / 'refresh_status.json'
    status = dict(pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat(),
                  finished_at=None, state='running', age_intervals=age_intervals,
                  interval_hours=options.interval_hours, age_basis=options.age_basis,
                  schedule_basis=options.schedule_basis, test_mode=options.test,
                  duration_hours=getattr(options, 'duration_hours', 0))
    status['ends_at'] = (datetime.fromisoformat(status['started_at']) + timedelta(hours=status['duration_hours'])).isoformat() if status['duration_hours'] else None
    def save():
        try:
            atomic_bytes(path, json.dumps(status).encode('utf-8'))
        except OSError as error:
            print(f'[REFRESH] Cannot save monitor runtime: {error}', file=sys.stderr)
    save()
    try:
        yield status
    except KeyboardInterrupt:
        status['state'] = 'stopped'
        raise
    except BaseException:
        status['state'] = 'failed'
        raise
    finally:
        status['finished_at'] = datetime.now(timezone.utc).isoformat()
        if status['state'] == 'running':
            status['state'] = 'completed'
        save()


def refresh_until_collection_count(dataset: DatasetConfig, urls: list[str], arguments: list[str], urls_file: Path, collection_count: int | None, interval_hours: float, publish: bool, age_intervals: tuple[float, ...] | None = None, *, workbook: Path | None = None, start_row: int | None = None, end_row: int | None = None, age_basis: str = "uploaded", schedule_basis: str = "uploaded", test_mode: bool = False, inbox=None, duration_hours: float = 0) -> int:
    deadline = datetime.now(timezone.utc) + timedelta(hours=duration_hours) if duration_hours else None
    def pause(seconds):
        if deadline:
            seconds = min(seconds, max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds()))
        time.sleep(seconds)
    urls = list(urls)
    idle = False
    retry_attempts = {}
    retry_not_before = {}
    test_targets = {url: count + 3 for url, (count, _) in refresh_url_state(read_history(dataset), urls).items()} if test_mode else {}
    while True:
        if deadline and datetime.now(timezone.utc) >= deadline:
            print('[REFRESH] Duration limit reached; saved history and queued work preserved.', flush=True)
            return 0
        if inbox is not None:
            registered = inbox.drain()
            blocked = inbox.cancelled_urls()
            urls = [url for url in urls if url not in blocked]
            for url in registered:
                if url not in urls:
                    urls.append(url)
        if workbook is not None:
            try:
                selected = read_reel_urls_from_xlsx(workbook, start_row=start_row, end_row=end_row)
            except (OSError, zipfile.BadZipFile, ParseError, KeyError, ValueError) as error:
                print(f"[REFRESH] Cannot read Excel; retrying in 30 seconds: {error}", flush=True)
                pause(30.0)
                continue
            added = [url for url in selected if url not in urls]
            if added:
                urls.extend(added)
                from src.reels.refresh_import import import_refresh_snapshots
                import_refresh_snapshots(workbook, dataset.data_root, start_row=start_row, end_row=end_row)
                print(f"[REFRESH] Added {len(added)} Reel URLs from saved Excel rows.", flush=True)
        rows = read_history(dataset)
        state = refresh_url_state(rows, urls)
        if test_mode:
            for url, (count, _) in state.items():
                test_targets.setdefault(url, count + 3)
        now = datetime.now(timezone.utc)
        timings = {}
        upload_origins = {}
        if schedule_basis == "uploaded":
            for row in rows:
                url = _normalize_reel_url(row.get("url"))
                timestamp = _parse_timestamp(row.get("uploaded_at"))
                if url in state and timestamp is not None:
                    upload_origins[url] = timestamp
            for url in urls:
                if state[url][0] and url not in upload_origins:
                    raise ValueError(f"Missing or invalid uploaded_at for {url}; provide the upload time, or choose fixed intervals with --schedule-basis last-collected.")
        if age_intervals is not None:
            origins = {}
            origin_field = "collected_at" if age_basis == "first-collected" else "uploaded_at"
            for row in rows:
                url = _normalize_reel_url(row.get("url"))
                timestamp = _parse_timestamp(row.get(origin_field))
                if url in state and timestamp is not None:
                    origins[url] = min(origins.get(url, timestamp), timestamp) if age_basis == "first-collected" else timestamp
            for url in urls:
                if url not in origins:
                    if (workbook is not None or inbox is not None) and state[url][0] == 0:
                        timings[url] = (now, now)
                        continue
                    raise ValueError(f"Missing or invalid {origin_field} for {url}; cannot schedule by {age_basis} age.")
                timing = refresh_age_timing(origins[url], state[url][1], now, age_intervals, schedule_basis=schedule_basis, schedule_origin=upload_origins.get(url))
                if timing is not None:
                    timings[url] = timing
        else:
            for url in urls:
                due = refresh_due_at(upload_origins.get(url, now), state[url][1], now, interval_hours, schedule_basis) if state[url][0] else now
                timings[url] = (due, due)
        for url, retry_at in retry_not_before.items():
            if url in timings:
                due, wake = timings[url]
                timings[url] = (max(due, retry_at), max(wake, retry_at))
        remaining = [url for url in timings if collection_count is None or state[url][0] < (test_targets[url] if test_mode else collection_count)]
        if inbox is not None:
            peak_cost = refresh_peak_hourly_cost(interval_hours, age_intervals, test_mode)
            reservations = {}
            for url in remaining:
                hours = interval_hours
                if age_intervals:
                    age = (now - origins[url]).total_seconds() / 3600 if url in origins else 0
                    hours = next((gap for boundary, gap in zip((24, 96, 120, 168), age_intervals) if age < boundary), age_intervals[-1])
                count = max(1, math.ceil(1 / hours))
                if collection_count is not None:
                    count = min(count, (test_targets[url] if test_mode else collection_count) - state[url][0])
                reservations[url] = count
            inbox.update_workload(reservations, peak_cost)
        if not remaining:
            if inbox is not None and test_mode and urls:
                print('[REFRESH] Test queue completed.', flush=True)
                return 0
            if workbook is not None or inbox is not None:
                if not idle:
                    print("[REFRESH] No Reels due; watching saved Excel rows every 30 seconds. Stop with Ctrl+C.", flush=True)
                idle = True
                pause(1.0 if inbox is not None else 30.0)
                continue
            print("[REFRESH] All selected Reels reached the age or collection-count limit.", flush=True)
            return 0
        idle = False
        due = [url for url in remaining if timings[url][0] <= now]
        if not due:
            next_at = min(timings[url][1] for url in remaining)
            print(f"[REFRESH] Waiting until {next_at.astimezone().isoformat()} ({len(remaining)} Reels remaining).", flush=True)
            if workbook is not None or inbox is not None:
                pause(min(1.0 if inbox is not None else 30.0, max(0.0, (next_at - now).total_seconds())))
                continue
            while datetime.now(timezone.utc) < min(next_at, deadline or next_at):
                pause(min(30.0, max(0.0, (next_at - datetime.now(timezone.utc)).total_seconds())))
            continue
        if deadline and datetime.now(timezone.utc) >= deadline:
            continue
        print(f"[REFRESH] Collecting {len(due)} due Reels.", flush=True)
        urls_file.write_text("".join(f"{url}\n" for url in due), encoding="utf-8")
        batch_arguments = list(arguments)
        if not any(argument.split("=", 1)[0] == "--max-items" for argument in batch_arguments):
            batch_arguments.extend(["--max-items", str(len(due))])
        batch_started = time.monotonic()
        result = collector_main(batch_arguments)
        after = refresh_url_state(read_history(dataset), urls)
        if inbox is not None:
            completed = sum(after[url][0] > state[url][0] for url in due)
            inbox.record_batch(time.monotonic() - batch_started, completed)
        if publish:
            publish_dataset_outputs(dataset)
        access_failure = _collector_access_failure(dataset) if result else ""
        # Keep explicit stops and authentication barriers terminal. In
        # particular, exit 2 includes the repeated-429 stop after resume.
        if result in {2, 130} or access_failure in {"login_required", "challenge_required"}:
            return result
        failed = []
        for url in due:
            if after[url][0] > state[url][0]:
                retry_attempts.pop(url, None)
                retry_not_before.pop(url, None)
            else:
                failed.append(url)
        if result and not failed:
            return result
        for url in failed:
            attempt = retry_attempts.get(url, 0) + 1
            retry_attempts[url] = attempt
            if attempt >= _COLLECTOR_RETRY_MAX_ATTEMPTS:
                if result:
                    return result
                raise RuntimeError("Refresh saved no new collection snapshots after the retry limit; stopped with existing history preserved.")
            delay = _collector_rate_limit_delay(dataset) if access_failure == "rate_limited" else _due_retry_delay(attempt)
            retry_not_before[url] = datetime.now(timezone.utc) + timedelta(seconds=delay)
            print(f"[REFRESH] Retry {attempt}/{_COLLECTOR_RETRY_MAX_ATTEMPTS} in {delay:g}s: {url}", flush=True)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    command = arguments.pop(0).lower()
    if command not in {"collect", "refresh", "followers", "xlsx", "reconcile", "android-worker", "hashtag-posts", *SCHEDULED_COMMAND_DOMAINS}:
        print(f"Unknown command: {command}\n\n{USAGE}", file=sys.stderr)
        return 2
    try:
        collector_mode, arguments = extract_collector_mode(arguments)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if command in SCHEDULED_COMMAND_DOMAINS:
        try:
            return asyncio.run(
                run_fashion_beauty_collection(
                    parse_scheduled_command(command, arguments, collector_mode=collector_mode)
                )
            )
        except KeyboardInterrupt:
            print("Scheduled collection stopped; latest checkpoint outputs were preserved.")
            return 130

    default_data_dir = PROJECT_ROOT / "data" / ("refresh" if command == "refresh" else "collector")
    data_dir = Path(option_value(arguments, "--data-dir", str(default_data_dir))).resolve()
    if command == "xlsx":
        return sync_xlsx(data_dir)
    if command == "reconcile":
        try:
            result = asyncio.run(reconcile_reel_exports(data_dir))
            print(
                "Reel exports reconciled: "
                f"added={result['addedSnapshots']} filled={result['filledValues']}"
            )
            return 0
        except Exception as error:
            print(str(error), file=sys.stderr)
            return 1
    if command == "android-worker":
        if collector_mode == "web":
            print("android-worker requires Android and cannot use web collector mode", file=sys.stderr)
            return 2
        parser = argparse.ArgumentParser(prog="instagram_reels_python.py android-worker")
        parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data" / "collector")
        parser.add_argument("--android-adb-path", type=Path)
        parser.add_argument("--android-device-id")
        parser.add_argument("--android-ui-delay-seconds", type=float, default=0.35)
        parser.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
        worker_options = parser.parse_args(arguments)
        if not math.isfinite(worker_options.android_ui_delay_seconds) or worker_options.android_ui_delay_seconds < 0.1:
            parser.error("--android-ui-delay-seconds must be at least 0.1")
        return run_android_metric_worker(
            worker_options.data_dir,
            adb_path=worker_options.android_adb_path,
            device_id=worker_options.android_device_id,
            ui_delay_seconds=worker_options.android_ui_delay_seconds,
            attach_existing=not worker_options.detached,
        )
    if command == "hashtag-posts":
        if collector_mode == "web":
            print("hashtag-posts requires Android and cannot use web collector mode", file=sys.stderr)
            return 2
        parser = argparse.ArgumentParser(prog="instagram_reels_python.py hashtag-posts")
        parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data" / "collector")
        hashtag_source = parser.add_mutually_exclusive_group(required=True)
        hashtag_source.add_argument(
            "--hashtag-query",
            help="Custom hashtags separated by OR.",
        )
        hashtag_source.add_argument(
            "--preset",
            choices=tuple(HASHTAG_POST_PRESETS),
            help="Collect every saved Fashion and/or Beauty hashtag.",
        )
        parser.add_argument("--android-adb-path", type=Path)
        parser.add_argument("--android-device-id")
        parser.add_argument("--android-ui-delay-seconds", type=float, default=0.2)
        hashtag_options = parser.parse_args(arguments)
        if not math.isfinite(hashtag_options.android_ui_delay_seconds) or hashtag_options.android_ui_delay_seconds < 0.1:
            parser.error("--android-ui-delay-seconds must be at least 0.1")
        if hashtag_options.preset:
            hashtags = HASHTAG_POST_PRESETS[hashtag_options.preset]
        else:
            try:
                hashtags = parse_hashtag_query(hashtag_options.hashtag_query)
            except ValueError as error:
                parser.error(str(error))
        try:
            rows, paths, android_summaries = asyncio.run(collect_hashtag_count_report(
                hashtag_options.data_dir,
                hashtags,
                profile_dir=PROJECT_ROOT / "runtime" / "browser_profile" / ".instagram_chrome_profile",
                adb_path=hashtag_options.android_adb_path,
                device_id=hashtag_options.android_device_id,
                ui_delay_seconds=hashtag_options.android_ui_delay_seconds,
            ))
        except KeyboardInterrupt:
            print("Hashtag collection stopped; collected tag rows were saved.")
            return 130
        for summary in android_summaries:
            append_collection_log(
                hashtag_options.data_dir,
                "android",
                "related_hashtag_count_collected",
                show_in_terminal=True,
                query_hashtag=summary.get("query_hashtag", ""),
                related_hashtag_count=summary.get("related_hashtag_count", ""),
                status=summary.get("status", ""),
                error=summary.get("error", ""),
            )
        for row in rows:
            source = str(row.get("source", ""))
            if "python_web_search_graphql" not in source:
                append_collection_log(
                    hashtag_options.data_dir,
                    "android",
                    "related_hashtag_media_count_collected",
                    show_in_terminal=True,
                    query_hashtag=row.get("query_hashtag", ""),
                    hashtag=row.get("hashtag", ""),
                    media_count=row.get("media_count", ""),
                    post_count=row.get("post_count", ""),
                    status=row.get("status", ""),
                    error=row.get("error", ""),
                )
                continue
            append_collection_log(
                hashtag_options.data_dir,
                "python",
                "exact_hashtag_media_count_collected",
                show_in_terminal=True,
                query_hashtag=row.get("query_hashtag", ""),
                hashtag=row.get("hashtag", ""),
                media_count=row.get("media_count", ""),
                post_count=row.get("post_count", ""),
                status=row.get("status", ""),
                error=row.get("error", ""),
            )
        collected = sum("python_web_search_graphql_exact" in str(row.get("source", "")) for row in rows)
        print(
            "Hashtag report finished: "
            f"Android queries={len(android_summaries)}, Python exact matched rows={collected} "
            f"({paths['xlsx'].name}/json/csv)"
        )
        return 0

    refresh_options = None
    refresh_intervals = None
    if command == "refresh":
        refresh_parser = argparse.ArgumentParser(prog="instagram_reels_python.py refresh", add_help=False)
        refresh_parser.add_argument("--inbox", action="store_true")
        refresh_parser.add_argument("--duration-hours", type=_finite_nonnegative, default=0, help="Maximum refresh runtime in hours; default 0 means unlimited.")
        refresh_parser.add_argument("--test", action="store_true")
        refresh_parser.add_argument("--input-xlsx", type=Path)
        refresh_parser.add_argument("--start-row", type=int)
        refresh_parser.add_argument("--end-row", type=int)
        refresh_parser.add_argument("--collection-count", type=_positive_integer)
        refresh_parser.add_argument("--interval-hours", type=_finite_positive, default=4)
        refresh_parser.add_argument("--age-schedule", action="store_true")
        refresh_parser.add_argument("--age-basis", choices=("uploaded", "first-collected"))
        schedule_basis = refresh_parser.add_mutually_exclusive_group()
        schedule_basis.add_argument("--schedule-basis", choices=("uploaded", "last-collected"), default="uploaded")
        schedule_basis.add_argument("--uploaded", dest="schedule_basis", action="store_const", const="uploaded", default=argparse.SUPPRESS)
        schedule_basis.add_argument("--last-collected", dest="schedule_basis", action="store_const", const="last-collected", default=argparse.SUPPRESS)
        for stage, default in (("initial", 2), ("middle", 3), ("late1", 6), ("late2", 12)):
            refresh_parser.add_argument(f"--{stage}-interval-hours", type=_finite_positive, default=default)
        refresh_options, arguments = refresh_parser.parse_known_args(arguments)
        if refresh_options.test:
            refresh_options.age_schedule = False
            refresh_options.collection_count = 3
            refresh_options.interval_hours = 5 / 60
            refresh_options.schedule_basis = "last-collected"
            if not any(arg.split("=", 1)[0] == "--data-dir" for arg in arguments):
                data_dir = PROJECT_ROOT / "data" / "refresh_test"
        if refresh_options.age_basis is None:
            refresh_options.age_basis = "first-collected" if refresh_options.schedule_basis == "last-collected" else "uploaded"
        if refresh_options.age_schedule:
            refresh_intervals = tuple(getattr(refresh_options, f"{stage}_interval_hours") for stage in ("initial", "middle", "late1", "late2"))
        if refresh_options.input_xlsx is not None:
            refresh_options.input_xlsx = refresh_options.input_xlsx.resolve()

    os.chdir(PROJECT_ROOT)
    temporary_urls: Path | None = None
    refresh_dataset = None
    refresh_lock = None
    inbox = None
    refresh_inputs = ExitStack()
    try:
        if command == "refresh":
            public_data_dir = data_dir.parent.parent if data_dir.parent.name == ".datasets" else data_dir
            supervisor_lock = public_data_dir / ".datasets" / "fashion_beauty_scheduler.lock.json"
            if supervisor_lock.exists():
                owner = json.loads(supervisor_lock.read_text(encoding="utf-8"))
                if process_is_alive(int(owner.get("pid", 0) or 0)):
                    raise RuntimeError("A scheduled collector is running in this data directory. Stop it before refresh.")
            refresh_lock = AtomicProcessLock(supervisor_lock)
            if not refresh_lock.acquire():
                raise RuntimeError("Another collector or refresh is already running in this data directory.")
            if not refresh_options.inbox and ((data_dir / ".datasets").is_dir() or (not (data_dir / "reels.xlsx").exists() and not DataStore(data_dir).workbook.exists())):
                candidates = [name for name in ("fashion", "beauty") if (data_dir / ".datasets" / name / "reels.xlsx").exists()]
                if refresh_options.input_xlsx is not None:
                    selected = [name for name in candidates if refresh_options.input_xlsx.stem == ("reels" if name == "fashion" else f"{name}_reels")]
                    candidates = selected
                if len(candidates) > 1:
                    raise ValueError("Multiple datasets found; select --input-xlsx reels.xlsx or beauty_reels.xlsx.")
                if candidates:
                    name = candidates[0]
                    refresh_dataset = DatasetConfig(name, data_dir / ".datasets" / name, ())
                    if refresh_options.input_xlsx is None:
                        refresh_options.input_xlsx = data_dir / ("reels.xlsx" if name == "fashion" else f"{name}_reels.xlsx")
                        if not refresh_options.input_xlsx.exists():
                            refresh_options.input_xlsx = refresh_dataset.data_root / "reels.xlsx"
                    data_dir = refresh_dataset.data_root
                    for index, argument in enumerate(arguments):
                        if argument == "--data-dir":
                            arguments[index + 1] = str(data_dir)
                        elif argument.startswith("--data-dir="):
                            arguments[index] = f"--data-dir={data_dir}"
            workbook = refresh_options.input_xlsx if refresh_options.input_xlsx else data_dir / ("input.xlsx" if refresh_options.inbox else "reels.xlsx")
            if not workbook.exists() and refresh_options.input_xlsx is None and not refresh_options.inbox:
                workbook = DataStore(data_dir).workbook
            if not workbook.exists():
                raise FileNotFoundError(f"Refresh Excel file was not found: {workbook}")
            refresh_inputs.enter_context(protect_refresh_workbook(workbook))
            if refresh_options.inbox:
                from src.reels.refresh_inbox import RefreshInbox
                inbox = RefreshInbox(workbook, data_dir, start_row=refresh_options.start_row, end_row=refresh_options.end_row, limit=10 if refresh_options.test else None,
                                     new_url_cost=refresh_peak_hourly_cost(refresh_options.interval_hours, refresh_intervals, refresh_options.test))
                try:
                    inbox.poll()
                except PermissionError:
                    print("[REFRESH] Waiting for input workbook to close.", flush=True)
                inbox.start()
                urls = inbox.drain()
            else:
                from src.reels.refresh_import import import_refresh_snapshots
                imported = import_refresh_snapshots(
                    workbook, data_dir, start_row=refresh_options.start_row, end_row=refresh_options.end_row, limit_urls=10 if refresh_options.test else None,
                )
                if imported:
                    print(f"[REFRESH] Imported {imported} existing snapshots; collecting subsequent snapshots only.", flush=True)
                urls = read_reel_urls_from_xlsx(
                    workbook, start_row=refresh_options.start_row, end_row=refresh_options.end_row,
                )
                if refresh_options.test:
                    urls = urls[:10]
            if not urls and not refresh_options.age_schedule and inbox is None:
                raise ValueError("No Instagram Reel URLs were found in the selected Excel rows.")
            descriptor, name = tempfile.mkstemp(prefix="instagram-reel-refresh-", suffix=".txt")
            os.close(descriptor)
            temporary_urls = Path(name)
            temporary_urls.write_text("".join(f"{url}\n" for url in urls), encoding="utf-8")
            arguments.extend(["--urls-file", str(temporary_urls), "--disable-recollect-cooldown"])
            if inbox is None and not refresh_options.age_schedule and not any(argument.split("=", 1)[0] == "--max-items" for argument in arguments):
                arguments.extend(["--max-items", str(len(urls))])
        elif command == "followers" and "--foreground" not in arguments and "--background" not in arguments:
            arguments.append("--background")
        if not any(argument.split("=", 1)[0] == "--data-dir" for argument in arguments):
            arguments.extend(["--data-dir", str(data_dir)])
        if collector_mode == "web" and command in MODE_COMMANDS:
            arguments.append("--no-android-metrics")
        if command == "followers":
            return collector_main(arguments, followers_only=True)
        if command == "refresh" and (inbox is not None or refresh_options.collection_count is not None or refresh_options.age_schedule):
            dataset = refresh_dataset or DatasetConfig("fashion", data_dir, (), base_output=True)
            watch = {"age_basis": refresh_options.age_basis}
            if refresh_options.age_schedule and inbox is None:
                print(f"[REFRESH] Watching {workbook}; exports targeting this input are saved as {workbook.stem}_refresh.xlsx.", flush=True)
                print(f"[REFRESH] Age basis: {refresh_options.age_basis}.", flush=True)
                watch = dict(workbook=workbook, start_row=refresh_options.start_row, end_row=refresh_options.end_row, age_basis=refresh_options.age_basis)
            with refresh_runtime_record(dataset.data_root, refresh_options, refresh_intervals) as runtime:
                result = refresh_until_collection_count(dataset, urls, arguments, temporary_urls, refresh_options.collection_count, refresh_options.interval_hours, refresh_dataset is not None, refresh_intervals, schedule_basis=refresh_options.schedule_basis, test_mode=refresh_options.test, inbox=inbox, duration_hours=refresh_options.duration_hours, **watch)
                runtime['state'] = 'completed' if result == 0 else 'stopped' if result == 130 else 'failed'
                return result
        result = collector_main(arguments)
        if refresh_dataset is not None:
            publish_dataset_outputs(refresh_dataset)
        return result
    except KeyboardInterrupt:
        print("[REFRESH] Stopped; saved collection history was preserved.", flush=True)
        return 130
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if inbox is not None:
            inbox.close()
        refresh_inputs.close()
        if refresh_lock is not None:
            refresh_lock.release()
        if temporary_urls:
            temporary_urls.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
