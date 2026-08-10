"""Download media from an Instagram Professional account using Meta's Graph API.

This script only accesses an Instagram Business/Creator account that has
authorized your Meta app. It does not automate the Instagram website.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


API_VERSION = "v26.0"
DEFAULT_FIELDS = (
    "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,"
    "username,like_count,comments_count"
)


def get_json(url: str) -> dict:
    """Request one Graph API page and return its JSON object."""
    try:
        with urlopen(url, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Graph API error ({error.code}): {details}") from error
    except URLError as error:
        raise RuntimeError(f"Network error: {error.reason}") from error


def fetch_media(ig_user_id: str, access_token: str, limit: int | None) -> list[dict]:
    params = {"fields": DEFAULT_FIELDS, "access_token": access_token, "limit": "100"}
    next_url = (
        f"https://graph.facebook.com/{API_VERSION}/{ig_user_id}/media?"
        f"{urlencode(params)}"
    )
    media: list[dict] = []

    while next_url and (limit is None or len(media) < limit):
        page = get_json(next_url)
        media.extend(page.get("data", []))
        next_url = page.get("paging", {}).get("next")

    return media[:limit] if limit is not None else media


def write_csv(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id",
        "timestamp",
        "username",
        "media_type",
        "caption",
        "permalink",
        "media_url",
        "thumbnail_url",
        "like_count",
        "comments_count",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export authorized Instagram Professional-account media to CSV."
    )
    parser.add_argument(
        "--ig-user-id",
        default=os.getenv("INSTAGRAM_IG_USER_ID"),
        help="Instagram User ID (or set INSTAGRAM_IG_USER_ID).",
    )
    parser.add_argument(
        "--access-token",
        default=os.getenv("INSTAGRAM_ACCESS_TOKEN"),
        help="Meta Graph API access token (or set INSTAGRAM_ACCESS_TOKEN).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of posts to export (default: all available).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("instagram_media.csv"),
        help="CSV output path (default: instagram_media.csv).",
    )
    args = parser.parse_args()

    if not args.ig_user_id or not args.access_token:
        parser.error(
            "Provide --ig-user-id and --access-token, or set the corresponding "
            "INSTAGRAM_IG_USER_ID and INSTAGRAM_ACCESS_TOKEN environment variables."
        )
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1.")

    try:
        rows = fetch_media(args.ig_user_id, args.access_token, args.limit)
        write_csv(rows, args.output)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Saved {len(rows)} posts to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
