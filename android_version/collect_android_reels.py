"""Collect only newly visible Instagram Reels from an Android emulator."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

from openpyxl import Workbook

from android_collector.adb_driver import AdbDriver, select_online_device
from android_collector.models import CollectorError
from android_collector.new_only_schedule import (
    BEAUTY_KEYWORDS,
    FASHION_KEYWORDS,
    NewOnlyScheduleOptions,
    run_new_only_schedule,
)
from android_collector.store import CollectionStore
from android_collector.workflows import CollectorOptions, run_feed, run_hashtag, run_refresh
from android_collector.xlsx_style import apply_python_compatible_xlsx_style


PROJECT_ROOT = Path(__file__).resolve().parent
COMMANDS = {"collect", "feed", "hashtag", "refresh", "fashion", "beauty", "fashion-beauty", "xlsx", "reconcile"}


def default_adb_path() -> Path:
    android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if android_home:
        return Path(android_home) / "platform-tools" / "adb.exe"
    return Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe"


def parse_hashtags(value: str) -> tuple[str, ...]:
    hashtags = tuple(part.strip().lstrip("#") for part in value.split("OR") if part.strip().lstrip("#"))
    if not hashtags:
        raise argparse.ArgumentTypeError("--hashtag-query must contain at least one value.")
    return hashtags


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be an integer greater than zero")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be an integer greater than or equal to zero")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def nonnegative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to zero")
    return number


def output_stems(output_stem: str) -> tuple[str, str]:
    if not output_stem.replace("_", "").replace("-", "").isalnum():
        raise argparse.ArgumentTypeError("--output-stem may contain only letters, numbers, underscores, and hyphens.")
    if output_stem == "reels":
        return "reels", "users"
    if output_stem.endswith("_reels"):
        return output_stem, f"{output_stem[:-6]}_users"
    return output_stem, f"{output_stem}_users"


def _instagram_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.casefold().endswith("instagram.com"):
        raise argparse.ArgumentTypeError("--start-url must be an https://www.instagram.com/ URL.")
    return value


def _add_android_options(
    target: argparse.ArgumentParser,
    *,
    include_hashtag: bool = True,
    include_new_only: bool = True,
) -> None:
    target.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data_android")
    target.add_argument("--adb-path", type=Path, default=default_adb_path())
    target.add_argument("--device-id", help="ADB serial of the Android Studio emulator to use.")
    target.add_argument("--adb-user-home", type=Path, default=PROJECT_ROOT / ".android")
    target.add_argument("--max-items", type=positive_int, default=50)
    target.add_argument(
        "--interval-seconds",
        "--delay-seconds",
        dest="delay_seconds",
        type=nonnegative_float,
        default=0.4,
        help="Maximum wait between Android UI actions in seconds (default: 0.4).",
    )
    target.add_argument("--checkpoint-items", type=positive_int, default=100)
    target.add_argument("--progress-offset", type=nonnegative_int, default=0)
    target.add_argument("--manual", action="store_true")
    target.add_argument("--start-url", type=_instagram_url)
    target.add_argument("--output-stem", default="reels")
    if include_new_only:
        target.add_argument("--new-only", action="store_true", help="Accepted for Python compatibility; Android collection is always new-only.")
    target.add_argument("--background", action="store_true", help="Accepted for compatibility; the emulator app remains the collection surface.")
    target.add_argument(
        "--fast",
        action="store_true",
        help="Reuse same-run author profiles and keep XML evidence without per-Reel PNG captures.",
    )
    target.add_argument(
        "--verbose-progress",
        "--show-collected-data",
        dest="verbose_progress",
        action="store_true",
        help="For every saved Reel, print each field as collected, visible-only, or unavailable.",
    )
    if include_hashtag:
        target.add_argument("--hashtag-query", "--hashtag", dest="hashtag_query", type=parse_hashtags, default=())
        target.add_argument(
            "--fashion",
            dest="fashion",
            action="store_true",
            help="Use the built-in fashion hashtag set without typing a query.",
        )
        target.add_argument(
            "--beauty",
            dest="beauty",
            action="store_true",
            help="Use the built-in beauty hashtag set without typing a query.",
        )
        target.add_argument(
            "--keywords-per-run",
            type=positive_int,
            default=5,
            help="Number of built-in fashion/beauty hashtags to use in this one collection run (default: 5).",
        )


def _add_schedule_options(target: argparse.ArgumentParser) -> None:
    target.add_argument("--duration-hours", type=positive_float, default=16.0)
    target.add_argument("--discovery-interval-minutes", type=positive_float, default=30.0)
    target.add_argument("--new-items-per-window", type=positive_int, default=300)
    target.add_argument("--max-new-items-per-window", type=positive_int, default=300)
    target.add_argument("--keywords-per-window", type=positive_int, default=5)
    target.add_argument("--base-output", "--shared-data", action="store_true")
    target.add_argument("--six-hour-new-only", action="store_true")
    target.add_argument("--test-single-hashtag", action="store_true")
    target.add_argument("--fashion-hashtag-query", type=parse_hashtags)
    target.add_argument("--beauty-hashtag-query", type=parse_hashtags)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or (arguments[0].startswith("-") and arguments[0] not in {"-h", "--help"}):
        arguments.insert(0, "collect")
    if arguments[0] == "followers":
        raise SystemExit("Android Emulator version does not support the followers-only command.")
    parser = argparse.ArgumentParser(description="Collect new, visibly rendered Instagram Reels from an Android emulator.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("collect", "feed", "hashtag"):
        command_parser = subparsers.add_parser(command)
        _add_android_options(command_parser)
    refresh_parser = subparsers.add_parser("refresh")
    _add_android_options(refresh_parser, include_hashtag=False, include_new_only=False)
    for command in ("fashion", "beauty", "fashion-beauty"):
        command_parser = subparsers.add_parser(command)
        _add_android_options(command_parser, include_hashtag=False)
        _add_schedule_options(command_parser)
    for command in ("xlsx", "reconcile"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data_android")
        command_parser.add_argument("--output-stem", default="reels")
    options = parser.parse_args(arguments)
    try:
        options.reel_stem, options.user_stem = output_stems(options.output_stem)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    if options.command == "hashtag" and not options.hashtag_query:
        parser.error("hashtag requires --hashtag or --hashtag-query.")
    if options.command in {"fashion", "beauty", "fashion-beauty"}:
        if options.manual:
            parser.error("--manual is not available for scheduled collection.")
        if options.six_hour_new_only:
            options.duration_hours = 6.0
            options.new_items_per_window = 250
            options.max_new_items_per_window = 250
            options.base_output = True
    return options


def create_driver(adb_path: Path, device_id: str | None, adb_user_home: Path) -> AdbDriver:
    if not adb_path.is_file():
        raise CollectorError(f"ADB executable was not found: {adb_path}. Install Android SDK Platform Tools or pass --adb-path.")
    adb_user_home.mkdir(parents=True, exist_ok=True)
    selected_device = select_online_device(adb_path, device_id, adb_user_home)
    return AdbDriver(adb_path, selected_device, adb_user_home)


def collector_options(options: argparse.Namespace) -> CollectorOptions:
    selected_hashtags = list(getattr(options, "hashtag_query", ()))
    keywords_per_run = getattr(options, "keywords_per_run", 5)
    if getattr(options, "fashion", False):
        selected_hashtags.extend(FASHION_KEYWORDS[:keywords_per_run])
    if getattr(options, "beauty", False):
        selected_hashtags.extend(BEAUTY_KEYWORDS[:keywords_per_run])
    hashtags: dict[str, str] = {}
    for hashtag in selected_hashtags:
        normalized = str(hashtag).strip().lstrip("#")
        if normalized:
            hashtags.setdefault(normalized.casefold(), normalized)
    return CollectorOptions(
        max_items=options.max_items,
        delay_seconds=options.delay_seconds,
        checkpoint_items=options.checkpoint_items,
        progress_offset=options.progress_offset,
        manual=options.manual,
        start_url=options.start_url or "",
        hashtags=tuple(hashtags.values()),
        verbose_progress=options.verbose_progress,
        capture_screenshots=not bool(getattr(options, "fast", False)),
        reuse_profiles_within_run=bool(getattr(options, "fast", False)),
    )


def sync_xlsx(data_dir: Path, reel_stem: str, user_stem: str) -> Path:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for stem, sheet_name in ((reel_stem, "reels"), (user_stem, "users")):
        worksheet = workbook.create_sheet(sheet_name)
        path = data_dir / f"{stem}.csv"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                worksheet.append(row)
    apply_python_compatible_xlsx_style(workbook)
    destination = data_dir / "instagram_data.xlsx"
    temporary = destination.with_name(f"{destination.stem}.tmp{destination.suffix}")
    workbook.save(temporary)
    workbook.close()
    temporary.replace(destination)
    return destination


def _run_new_collection(options: argparse.Namespace, driver: AdbDriver) -> int:
    store = CollectionStore(options.data_dir, reel_stem=options.reel_stem, user_stem=options.user_stem)
    collection = collector_options(options)
    if options.command == "refresh":
        return run_refresh(collection, driver, store)
    if options.command == "hashtag" or collection.hashtags:
        if not collection.hashtags:
            raise CollectorError("--hashtag-query is required for hashtag collection.")
        return run_hashtag(collection, driver, store)
    return run_feed(collection, driver, store)


def _run_schedule(options: argparse.Namespace, driver: AdbDriver) -> int:
    domains = ("fashion", "beauty") if options.command == "fashion-beauty" else (options.command,)
    schedule = NewOnlyScheduleOptions(
        data_dir=options.data_dir,
        duration_hours=options.duration_hours,
        discovery_interval_minutes=options.discovery_interval_minutes,
        new_items_per_window=options.new_items_per_window,
        max_new_items_per_window=options.max_new_items_per_window,
        keywords_per_window=options.keywords_per_window,
        test_single_hashtag=options.test_single_hashtag,
        base_output=options.base_output,
        fashion_keywords=options.fashion_hashtag_query or FASHION_KEYWORDS,
        beauty_keywords=options.beauty_hashtag_query or BEAUTY_KEYWORDS,
    )
    return run_new_only_schedule(collector_options(options), schedule, driver, domains)


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    try:
        if options.command == "xlsx":
            print(f"XLSX synchronized: {sync_xlsx(options.data_dir.resolve(), options.reel_stem, options.user_stem)}")
            return 0
        if options.command == "reconcile":
            CollectionStore(options.data_dir, reel_stem=options.reel_stem, user_stem=options.user_stem).export()
            print("Android exports reconciled from the local observation history.")
            return 0
        driver = create_driver(options.adb_path, options.device_id, options.adb_user_home)
        if options.background:
            print("--background uses the already logged-in Android app; no browser window is created.", file=sys.stderr)
        stored = _run_schedule(options, driver) if options.command in {"fashion", "beauty", "fashion-beauty"} else _run_new_collection(options, driver)
    except (CollectorError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    action = "refreshed" if options.command == "refresh" else "saved"
    print(f"Android Reel snapshots {action}: {stored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
