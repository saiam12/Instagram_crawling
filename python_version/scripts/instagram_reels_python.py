"""Python-only launcher for collection, refresh, follower update, and XLSX sync."""

from __future__ import annotations

import asyncio
import argparse
import math
import os
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.instagram_reels_browser import (  # noqa: E402
    main as collector_main,
    parse_hashtag_query,
    reconcile_reel_exports,
)
from collectors.fashion_beauty_collection import run_fashion_beauty_collection  # noqa: E402
from collectors.fashion_beauty_scheduler import (  # noqa: E402
    BEAUTY_KEYWORDS,
    FASHION_KEYWORDS,
    KEYWORDS_PER_WINDOW,
    RunConfig,
    SIX_HOUR_NEW_ONLY_KEYWORDS_PER_WINDOW,
)
from exporters.instagram_collector import DataStore, read_reel_urls_from_xlsx  # noqa: E402


USAGE = """usage: instagram_reels_python.py {collect,refresh,followers,xlsx,reconcile,fashion,beauty,fashion-beauty} [collector options]

commands:
  collect     collect new Reels
  refresh     recollect Reel URLs from reels.xlsx
  followers   update saved users' follower counts
  xlsx        synchronize CSV files into instagram_data.xlsx
  reconcile   merge reels_updated.xlsx and rebuild public exports
  fashion         run the Fashion-only scheduled collection
  beauty          run the Beauty-only scheduled collection
  fashion-beauty  alternate Fashion and Beauty every discovery window

All options after the command are passed to the Python collector. Examples:
  python scripts/instagram_reels_python.py collect --max-items 50 --background
  python scripts/instagram_reels_python.py refresh --background --direct-concurrency 2
  python scripts/instagram_reels_python.py followers --follower-interval-seconds 8
  python scripts/instagram_reels_python.py reconcile
  python scripts/instagram_reels_python.py fashion
  python scripts/instagram_reels_python.py beauty
  python scripts/instagram_reels_python.py fashion-beauty
"""


SCHEDULED_COMMAND_DOMAINS = {
    "fashion": ("fashion",),
    "beauty": ("beauty",),
    "fashion-beauty": ("fashion", "beauty"),
}


def option_value(arguments: list[str], name: str, default: str) -> str:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError):
        return default


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


def parse_scheduled_command(command: str, arguments: list[str]) -> RunConfig:
    domains = SCHEDULED_COMMAND_DOMAINS[command]
    parser = argparse.ArgumentParser(
        prog=f"instagram_reels_python.py {command}",
        description="Run a scheduled Fashion and/or Beauty collection.",
    )
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data_web")
    parser.add_argument("--duration-hours", type=_finite_positive, default=16)
    parser.add_argument("--discovery-hours", type=_finite_positive, default=7)
    parser.add_argument("--new-items-per-window", type=_positive_integer, default=300)
    parser.add_argument("--max-new-items-per-window", type=_positive_integer, default=300)
    parser.add_argument("--max-upload-age-days", "--maxdays", dest="max_upload_age_days", type=_finite_nonnegative, default=30)
    parser.add_argument("--discovery-interval-minutes", type=_finite_positive, default=30)
    parser.add_argument("--background", action="store_true", help="Use the saved Instagram login without showing a browser window.")
    parser.add_argument("--direct-reel-info-wait-seconds", type=_finite_nonnegative, default=3)
    parser.add_argument("--exact-metric-attempts", type=_positive_integer, default=3)
    parser.add_argument("--exact-metric-retry-delay-seconds", type=_finite_nonnegative, default=2)
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Collect only newly discovered Reels for the entire run; do not schedule or run recollections.",
    )
    parser.add_argument(
        "--base-output",
        "--shared-data",
        dest="base_output",
        action="store_true",
        help="Write directly to the existing data directory's reels.* and users.* files instead of domain-prefixed files.",
    )
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
    parser.add_argument("--fashion-hashtag-query")
    parser.add_argument("--beauty-hashtag-query")
    options = parser.parse_args(arguments)
    keywords_per_window = KEYWORDS_PER_WINDOW
    if options.six_hour_new_only:
        if command != "fashion-beauty":
            parser.error("--six-hour-new-only can only be used with fashion-beauty")
        options.duration_hours = 6
        options.discovery_hours = 6
        options.new_only = True
        options.base_output = True
        options.max_upload_age_days = 365
        # Keep hashtag discovery inside the 30-minute window: inspect about
        # 50 candidates for each of the 5 active hashtags and
        # retain every qualifying Reel from that one 30-minute window.
        keywords_per_window = SIX_HOUR_NEW_ONLY_KEYWORDS_PER_WINDOW
        options.new_items_per_window = 250
        options.max_new_items_per_window = 250
    if options.max_new_items_per_window > 300:
        parser.error("--max-new-items-per-window cannot exceed 300")
    if options.new_items_per_window > options.max_new_items_per_window:
        parser.error("--new-items-per-window cannot exceed --max-new-items-per-window")
    if options.exact_metric_attempts > 5:
        parser.error("--exact-metric-attempts cannot exceed 5")
    if options.new_only:
        # New-only runs have no post-discovery recollection period, so the
        # complete duration is an active discovery period.
        options.discovery_hours = options.duration_hours
    elif options.discovery_hours > options.duration_hours - 9:
        parser.error("--discovery-hours must end at least 9 hours before --duration-hours")

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
        # Keep the reduced scope opt-in so normal scheduled collection still
        # rotates five keywords per discovery window.
        if "fashion" in domains:
            fashion_keywords = fashion_keywords[:1]
        if "beauty" in domains:
            beauty_keywords = beauty_keywords[:1]
        keywords_per_window = 1

    return RunConfig(
        data_root=options.data_dir.resolve(),
        duration_hours=options.duration_hours,
        discovery_hours=options.discovery_hours,
        discovery_interval_minutes=options.discovery_interval_minutes,
        new_items_per_window=options.new_items_per_window,
        max_new_items_per_window=options.max_new_items_per_window,
        max_upload_age_days=options.max_upload_age_days,
        background=options.background,
        direct_reel_info_wait_seconds=options.direct_reel_info_wait_seconds,
        exact_metric_attempts=options.exact_metric_attempts,
        exact_metric_retry_delay_seconds=options.exact_metric_retry_delay_seconds,
        keywords_per_window=keywords_per_window,
        new_only=options.new_only,
        base_output=options.base_output,
        fashion_keywords=fashion_keywords,
        beauty_keywords=beauty_keywords,
        domains=domains,
    )


def parse_fashion_command(arguments: list[str]) -> RunConfig:
    """Backward-compatible parser entry point for the fashion-only command."""
    return parse_scheduled_command("fashion", arguments)


def sync_xlsx(data_dir: Path) -> int:
    store = DataStore(data_dir)
    try:
        store.sync_xlsx()
        print(f"XLSX saved: {store.workbook}")
    except PermissionError:
        updated = data_dir / "instagram_data_updated.xlsx"
        store.sync_xlsx(updated)
        print(f"instagram_data.xlsx is open. The latest data was saved to: {updated}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    command = arguments.pop(0).lower()
    if command not in {"collect", "refresh", "followers", "xlsx", "reconcile", *SCHEDULED_COMMAND_DOMAINS}:
        print(f"Unknown command: {command}\n\n{USAGE}", file=sys.stderr)
        return 2
    if command in SCHEDULED_COMMAND_DOMAINS:
        try:
            return asyncio.run(run_fashion_beauty_collection(parse_scheduled_command(command, arguments)))
        except KeyboardInterrupt:
            print("Scheduled collection stopped; latest checkpoint outputs were preserved.")
            return 130

    data_dir = Path(option_value(arguments, "--data-dir", str(PROJECT_ROOT / "data_web"))).resolve()
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

    os.chdir(PROJECT_ROOT)
    temporary_urls: Path | None = None
    try:
        if command == "refresh":
            workbook = data_dir / "reels.xlsx"
            if not workbook.exists():
                workbook = data_dir / "instagram_data.xlsx"
            if not workbook.exists():
                raise FileNotFoundError(f"reels.xlsx or instagram_data.xlsx was not found in: {data_dir}")
            urls = read_reel_urls_from_xlsx(workbook)
            descriptor, name = tempfile.mkstemp(prefix="instagram-reel-refresh-", suffix=".txt")
            os.close(descriptor)
            temporary_urls = Path(name)
            temporary_urls.write_text("".join(f"{url}\n" for url in urls), encoding="utf-8")
            arguments.extend(["--urls-file", str(temporary_urls)])
        elif command == "followers":
            arguments.extend(["--followers-only", "--background"])
        if "--data-dir" not in arguments:
            arguments.extend(["--data-dir", str(data_dir)])
        return collector_main(arguments)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if temporary_urls:
            temporary_urls.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
