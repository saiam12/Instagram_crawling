"""Python-only launcher for collection, refresh, follower update, and XLSX sync."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.instagram_reels_browser import (  # noqa: E402
    main as collector_main,
    reconcile_reel_exports,
)
from exporters.instagram_collector import DataStore, read_reel_urls_from_xlsx  # noqa: E402


USAGE = """usage: instagram_reels_python.py {collect,refresh,followers,xlsx,reconcile} [collector options]

commands:
  collect     collect new Reels
  refresh     recollect Reel URLs from reels.xlsx
  followers   update saved users' follower counts
  xlsx        synchronize CSV files into instagram_data.xlsx
  reconcile   merge reels_updated.xlsx and rebuild public exports

All options after the command are passed to the Python collector. Examples:
  python scripts/instagram_reels_python.py collect --max-items 50 --background
  python scripts/instagram_reels_python.py refresh --background --direct-concurrency 2
  python scripts/instagram_reels_python.py followers --follower-interval-seconds 8
  python scripts/instagram_reels_python.py reconcile
"""


def option_value(arguments: list[str], name: str, default: str) -> str:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError):
        return default


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
    if command not in {"collect", "refresh", "followers", "xlsx", "reconcile"}:
        print(f"Unknown command: {command}\n\n{USAGE}", file=sys.stderr)
        return 2
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
