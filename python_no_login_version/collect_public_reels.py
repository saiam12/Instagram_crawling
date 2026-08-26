"""Recollect previously discovered Instagram Reels without a login profile."""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path


NO_LOGIN_ROOT = Path(__file__).resolve().parent
SHARED_PYTHON_ROOT = NO_LOGIN_ROOT.parent / "python_version"
sys.path.insert(0, str(SHARED_PYTHON_ROOT))

from collectors.instagram_reels_browser import main as collector_main  # noqa: E402
from collectors.instagram_reels_browser import normalize_reel_url  # noqa: E402
from exporters.instagram_collector import read_reel_urls_from_xlsx  # noqa: E402


CANONICAL_DATA_DIR = SHARED_PYTHON_ROOT / "data_web"


USAGE = """usage: collect_public_reels.py [refresh] [--limit N] [collector options]

Recollect only Reels that were first saved by the logged-in Python collector.
The temporary browser does not create or reuse an Instagram login profile.

recommended:
  python collect_public_reels.py refresh --background
  python collect_public_reels.py --limit 50 --background

useful collector options:
  --interval-seconds N
  --background
  --data-dir PATH

By default, targets are read from python_version\\data_web\\reels.xlsx and
results are appended to python_version\\data_web. Direct URLs, URL files, and
new-feed or hashtag discovery are deliberately unavailable in this launcher.
Instagram can still require login or rate-limit anonymous visitors; this tool
does not bypass those restrictions.
"""


def has_option(arguments: list[str], name: str) -> bool:
    return any(value == name or value.startswith(f"{name}=") for value in arguments)


def data_dir_from_arguments(arguments: list[str]) -> Path:
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--data-dir":
            if index + 1 >= len(arguments):
                raise ValueError("--data-dir requires a folder path.")
            return Path(arguments[index + 1]).expanduser().resolve()
        if value.startswith("--data-dir="):
            directory = value.split("=", 1)[1].strip()
            if not directory:
                raise ValueError("--data-dir requires a folder path.")
            return Path(directory).expanduser().resolve()
        index += 1
    return CANONICAL_DATA_DIR


def read_initial_reel_urls(data_dir: Path) -> list[str]:
    workbook_path = data_dir / "reels.xlsx"
    if workbook_path.exists():
        return read_reel_urls_from_xlsx(workbook_path)

    csv_path = data_dir / "reels.csv"
    if not csv_path.exists():
        raise ValueError(
            f"No reels.xlsx or reels.csv was found in {data_dir}. "
            "Run the logged-in Python collector first to save new Reels."
        )

    urls: list[str] = []
    seen: set[str] = set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            normalized = normalize_reel_url(str(row.get("url", "")))
            if normalized and normalized["url"] not in seen:
                seen.add(normalized["url"])
                urls.append(normalized["url"])
    return urls


def prepare_arguments(argv: list[str]) -> tuple[list[str], int]:
    forwarded: list[str] = []
    limit = 0
    index = 0
    blocked_options = {
        "--url",
        "--urls-file",
        "--hashtag-query",
        "--start-url",
        "--output-stem",
        "--followers-only",
        "--followers-after-reels",
    }
    while index < len(argv):
        value = argv[index]
        option_name = value.split("=", 1)[0]
        if value == "refresh":
            index += 1
            continue
        if option_name in blocked_options:
            raise ValueError(
                f"{option_name} is unavailable here. This launcher only recollects URLs already saved by the Python collector."
            )
        if value == "--limit":
            if index + 1 >= len(argv):
                raise ValueError("--limit requires a non-negative integer.")
            raw_limit = argv[index + 1]
            index += 2
        elif value.startswith("--limit="):
            raw_limit = value.split("=", 1)[1]
            index += 1
        else:
            forwarded.append(value)
            index += 1
            continue
        try:
            limit = int(raw_limit)
        except ValueError as error:
            raise ValueError("--limit requires a non-negative integer.") from error
        if limit < 0:
            raise ValueError("--limit requires a non-negative integer.")

    if not has_option(forwarded, "--data-dir"):
        forwarded.extend(["--data-dir", str(CANONICAL_DATA_DIR)])
    if not has_option(forwarded, "--no-login"):
        forwarded.append("--no-login")
    return forwarded, limit


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(value in {"-h", "--help"} for value in arguments):
        print(USAGE)
        return 0
    temporary_urls: Path | None = None
    try:
        forwarded, limit = prepare_arguments(arguments)
        urls = read_initial_reel_urls(data_dir_from_arguments(forwarded))
        if limit:
            urls = urls[:limit]
        if not urls:
            raise ValueError(
                "No first-collection Reel URLs are available. Run the logged-in Python collector first."
            )
        descriptor, name = tempfile.mkstemp(prefix="instagram-recollect-reels-", suffix=".txt")
        os.close(descriptor)
        temporary_urls = Path(name)
        temporary_urls.write_text("".join(f"{url}\n" for url in urls), encoding="utf-8")
        forwarded.extend(["--urls-file", str(temporary_urls)])
        return collector_main(forwarded)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        if temporary_urls:
            temporary_urls.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
