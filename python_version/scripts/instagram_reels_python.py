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
    RunConfig,
)
from exporters.instagram_collector import DataStore, read_reel_urls_from_xlsx  # noqa: E402


USAGE = """usage: instagram_reels_python.py {collect,refresh,followers,xlsx,reconcile,fashion} [collector options]

commands:
  collect     collect new Reels
  refresh     recollect Reel URLs from reels.xlsx
  followers   update saved users' follower counts
  xlsx        synchronize CSV files into instagram_data.xlsx
  reconcile   merge reels_updated.xlsx and rebuild public exports
  fashion     run the approved Fashion + Beauty scheduled collection

All options after the command are passed to the Python collector. Examples:
  python scripts/instagram_reels_python.py collect --max-items 50 --background
  python scripts/instagram_reels_python.py refresh --background --direct-concurrency 2
  python scripts/instagram_reels_python.py followers --follower-interval-seconds 8
  python scripts/instagram_reels_python.py reconcile
  python scripts/instagram_reels_python.py fashion
"""


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


def parse_fashion_command(arguments: list[str]) -> RunConfig:
    parser = argparse.ArgumentParser(
        prog="instagram_reels_python.py fashion",
        description="Run the isolated Fashion + Beauty collection scheduler.",
    )
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data_web")
    parser.add_argument("--duration-hours", type=_finite_positive, default=16)
    parser.add_argument("--discovery-hours", type=_finite_positive, default=8)
    parser.add_argument("--new-items-per-window", type=_positive_integer, default=50)
    parser.add_argument("--max-new-items-per-window", type=_positive_integer, default=500)
    parser.add_argument("--max-upload-age-days", type=_finite_nonnegative, default=30)
    parser.add_argument("--discovery-interval-minutes", type=_finite_positive, default=30)
    parser.add_argument("--fashion-hashtag-query")
    parser.add_argument("--beauty-hashtag-query")
    options = parser.parse_args(arguments)
    if options.max_new_items_per_window > 500:
        parser.error("--max-new-items-per-window cannot exceed 500")
    if options.new_items_per_window > options.max_new_items_per_window:
        parser.error("--new-items-per-window cannot exceed --max-new-items-per-window")
    if options.discovery_hours > options.duration_hours - 8:
        parser.error("--discovery-hours must end at least 8 hours before --duration-hours")

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
    if not fashion_keywords or not beauty_keywords:
        parser.error("custom hashtag queries must contain at least one hashtag")

    return RunConfig(
        data_root=options.data_dir.resolve(),
        duration_hours=options.duration_hours,
        discovery_hours=options.discovery_hours,
        discovery_interval_minutes=options.discovery_interval_minutes,
        new_items_per_window=options.new_items_per_window,
        max_new_items_per_window=options.max_new_items_per_window,
        max_upload_age_days=options.max_upload_age_days,
        fashion_keywords=fashion_keywords,
        beauty_keywords=beauty_keywords,
    )


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
    if command not in {"collect", "refresh", "followers", "xlsx", "reconcile", "fashion"}:
        print(f"Unknown command: {command}\n\n{USAGE}", file=sys.stderr)
        return 2
    if command == "fashion":
        try:
            return asyncio.run(run_fashion_beauty_collection(parse_fashion_command(arguments)))
        except KeyboardInterrupt:
            print("Fashion + Beauty collection stopped; latest checkpoint outputs were preserved.")
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
