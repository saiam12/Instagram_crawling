"""Python implementation of the Instagram Reels browser collector.

The command-line interface and CSV schemas intentionally match
``instagram_reels_browser.mjs``. The Python implementation keeps its browser
profile and collected data under ``python_version`` so it stays independent
from the existing JavaScript and PowerShell implementation. Browser automation
is imported lazily; pure data utilities and tests do not require Playwright to
be installed.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import csv
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence
from urllib.parse import parse_qs, quote, urljoin, urlparse
from xml.etree import ElementTree


PYTHON_VERSION_ROOT = Path(__file__).resolve().parent.parent

if __package__:
    from .android_reel_metrics import (
        AndroidMetricResult,
        AndroidReelMetricsEnricher,
        merge_android_metrics,
        write_hashtag_post_counts,
    )
    from .instagram_follower_enricher import (
        FollowerEnricher,
        ensure_user_history,
        read_csv_objects,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from collectors.android_reel_metrics import (  # type: ignore[no-redef]
        AndroidMetricResult,
        AndroidReelMetricsEnricher,
        merge_android_metrics,
        write_hashtag_post_counts,
    )
    from collectors.instagram_follower_enricher import (  # type: ignore[no-redef]
        FollowerEnricher,
        ensure_user_history,
        read_csv_objects,
    )


CSV_FIELDS = [
    "collected_at",
    "url",
    "user_id",
    "username",
    "title",
    "hashtags",
    "audio_name",
    "location_name",
    "ad",
    "uploaded_at",
    "video_duration_seconds",
    "days_since_upload",
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "repost_count",
    "saved_count",
    "follower_count",
]
RECOLLECT_FIELDS = [
    "collected_at",
    "days_since_upload",
    "video_duration_seconds",
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "repost_count",
    "saved_count",
    "follower_count",
]
# Keep refreshes ordered without exposing labels such as "Initial" and
# "2nd collect" in the row-oriented output files.
REEL_CHANGE_METRICS = [
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "repost_count",
    "saved_count",
    "follower_count",
]
REEL_METRIC_CHANGE_FIELDS = [f"{field}_change" for field in REEL_CHANGE_METRICS]
REEL_CHANGE_FIELDS = [*REEL_METRIC_CHANGE_FIELDS, "reaction_rate_change"]
UNAVAILABLE_LIKE_COUNT_MARKER = "X"
ROW_COLLECTION_FIELDS = [
    "collection_number",
    "days_since_previous",
    *CSV_FIELDS,
    "reaction_rate",
    *REEL_CHANGE_FIELDS,
]
REEL_HISTORY_DIRECTORY = ".collector"
REEL_HISTORY_FILENAME = "reels_history_active.csv"
PYTHON_COLLECTION_LOG_FILENAME = "python.log"
ANDROID_COLLECTION_LOG_FILENAME = "android.log"
LEGACY_REEL_HISTORY_FILENAMES = ("reels_history.csv",)
PUBLIC_REELS_STEM = "reels"
LEGACY_REEL_DROPPED_FIELDS = {
    "collection_label",
    "follower_count_collected_at",
    "follower_lookup_status",
}
REEL_SUCCESS_INTERVAL_SECONDS = 0.5
FOLLOWER_SUCCESS_INTERVAL_SECONDS = 0.3
# The Python collector owns users.xlsx, so follower_count is intentionally not
# part of a Reel-to-Android handoff decision.  Likewise, title, hashtags,
# audio, and location may be genuinely absent from a public Reel; an empty
# value for those optional fields is not evidence that Python failed.
#
# The three excluded counts are Android-owned in the hybrid pipeline.  Python
# values are still retained when available, but they never delay an Android
# URL handoff.
PYTHON_TO_ANDROID_EXCLUDED_COUNT_FIELDS = frozenset({
    "view_count",
    "share_count",
    "repost_count",
})
PYTHON_TO_ANDROID_REQUIRED_FIELDS = (
    "url",
    "user_id",
    "username",
    "ad",
    "uploaded_at",
    "video_duration_seconds",
    "days_since_upload",
    "like_count",
    "comment_count",
)
PYTHON_TO_ANDROID_RETRY_DELAY_SECONDS = 3.0
ANDROID_METRIC_QUEUE_DIRECTORY = "android_metric_queue"
ANDROID_METRIC_QUEUE_IDLE_SECONDS = 12.0
ANDROID_METRIC_QUEUE_EXPORT_BATCH_SIZE = 20
ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL = 3
COLLECTION_MAX_CONSECUTIVE_FAILURES = 5
COLLECTION_STOP_FILENAME = "collection_stop.json"
ANDROID_METRIC_FIELDS = (
    "like_count",
    "view_count",
    "comment_count",
    "share_count",
    "repost_count",
    "saved_count",
    "audio_name",
)
PYTHON_ANDROID_COMPARISON_COUNT_FIELDS = (
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "repost_count",
    "saved_count",
)
HASHTAG_REDISCOVERY_INTERVAL_SECONDS = 60
HASHTAG_GRID_INITIAL_LOAD_MILLISECONDS = 2_500
HASHTAG_GRID_SCROLL_SETTLE_MILLISECONDS = 1_000
HASHTAG_GRID_MAX_SCROLL_ATTEMPTS = 60
HASHTAG_GRID_MAX_UNCHANGED_ATTEMPTS = 8
FOLLOWER_PROFILE_SETTLE_MILLISECONDS = 700
FOLLOWER_PAGE_RECYCLE_LOOKUP_COUNT = 500
PROFILE_REEL_VIEW_SETTLE_MILLISECONDS = 700
PROFILE_REEL_VIEW_SCROLL_ATTEMPTS = 30
PROFILE_REEL_VIEW_SCROLL_MILLISECONDS = 500
PROFILE_REEL_VIEW_PAGE_ATTEMPTS = 2
PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS = 1_500
REEL_STORE_FLUSH_RECORD_COUNT = 100
REEL_PAGE_RECYCLE_ITEM_COUNT = 200
REEL_TRANSITION_TIMEOUT_SECONDS = 3.0
REEL_TRANSITION_POLL_MILLISECONDS = 100
REEL_TRANSITION_SETTLE_MILLISECONDS = 200
REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD = 8
REEL_MAX_CONSECUTIVE_RECOVERY_FAILURES = COLLECTION_MAX_CONSECUTIVE_FAILURES
REEL_STATUS_WRITE_INTERVAL_SECONDS = 60
DIRECT_REEL_CONCURRENCY = 1
DIRECT_REEL_SETTLE_MILLISECONDS = 250
# Keep listening for JSON responses that the current Reel page has already
# initiated. This is deliberately a passive wait: it must not make a second
# Instagram request simply because a metric arrives late.
PASSIVE_RESPONSE_METADATA_TIMEOUT_MILLISECONDS = 20_000
PASSIVE_RESPONSE_EXTENDED_WAIT_MILLISECONDS = 60_000
DIRECT_REEL_METADATA_TIMEOUT_MILLISECONDS = PASSIVE_RESPONSE_METADATA_TIMEOUT_MILLISECONDS
# The legacy media-info fetch is retained for compatibility and diagnostics,
# but disabled in normal operation. Collection must rely only on data emitted
# while Instagram renders the requested page.
DIRECT_REEL_INFO_REQUESTS_ENABLED = False
DIRECT_REEL_INFO_TIMEOUT_SECONDS = 5.0
DIRECT_REEL_INFO_FETCH_TIMEOUT_MILLISECONDS = 4_000
DIRECT_REEL_INFO_FRESH_PAGE_SETTLE_MILLISECONDS = 1_000
EXACT_METRIC_MAX_ATTEMPTS = 3
EXACT_METRIC_RETRY_DELAY_SECONDS = 2.0
EXACT_REEL_DETAIL_SETTLE_MILLISECONDS = 1_000
EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS = 3_000
EXACT_REEL_DETAIL_POLL_MILLISECONDS = 150
EXACT_REEL_DETAIL_PAGE_ATTEMPTS = 3
EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS = 1_500


def collection_log_path(data_dir: Path | str, source: str) -> Path:
    """Return the live log dedicated to one collection source."""
    filenames = {
        "python": PYTHON_COLLECTION_LOG_FILENAME,
        "android": ANDROID_COLLECTION_LOG_FILENAME,
    }
    try:
        filename = filenames[source]
    except KeyError as error:
        raise ValueError(f"Unknown collection log source: {source}") from error
    return Path(data_dir).resolve() / REEL_HISTORY_DIRECTORY / filename


def _collection_log_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        text = str(value)
    return " ".join(text.split())


def append_collection_log(data_dir: Path | str, source: str, event: str, **values: Any) -> Path:
    """Append one human-readable, source-specific collection event.

    The browser process and the detached Android worker intentionally use
    different files so their live output never interleaves.  Logging must not
    interrupt a collection if a user has the file open in another program.
    """
    path = collection_log_path(data_dir, source)
    details = " ".join(
        f"{key}={rendered}"
        for key, value in values.items()
        if (rendered := _collection_log_value(value))
    )
    line = f"{isoformat_utc()} [{source.upper()}] {event}"
    if details:
        line = f"{line} | {details}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(f"{line}\n")
            file.flush()
    except OSError:
        # Collection data is written through its own atomic history path;
        # a transient log-file lock must not make the data capture fail.
        pass
    return path
ANONYMOUS_FOLLOWER_MAX_ATTEMPTS = 3
ANONYMOUS_FOLLOWER_RETRY_SECONDS = 1.0
FOLLOWER_WEB_MAX_ATTEMPTS = 3
FOLLOWER_WEB_RETRY_DELAY_SECONDS = 2.0
RECOLLECT_COOLDOWN_SCHEDULE = [
    {"label": "6시간", "seconds": 6 * 60 * 60},
    {"label": "1일", "seconds": 24 * 60 * 60},
    {"label": "3일", "seconds": 3 * 24 * 60 * 60},
    {"label": "1주", "seconds": 7 * 24 * 60 * 60},
    {"label": "2주", "seconds": 14 * 24 * 60 * 60},
    {"label": "1달", "calendar_months": 1},
]
PROFILE_INFO_TEXT_PATTERN = re.compile(
    r"(?:계정\s*정보|프로필|about\s+this\s+account|account\s+(?:info|information)|view\s+profile|profile)",
    re.I,
)
PROFILE_CATEGORY_TEXT_PATTERN = re.compile(
    r"^[^\r\n]{1,100}\([^\r\n()]{1,60}\)$"
)
INSTAGRAM_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]{1,30}$")

DIRECT_REEL_INFO_REQUEST_SCRIPT = r"""async mediaId => {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), __FETCH_TIMEOUT_MILLISECONDS__);
  try {
    const response = await fetch(`/api/v1/media/${encodeURIComponent(mediaId)}/info/`, {
      credentials: 'include',
      headers: {'X-IG-App-ID': '936619743392459', 'X-Requested-With': 'XMLHttpRequest'},
      signal: controller.signal
    });
    const result = {
      status: response.status,
      url: response.url,
      contentType: response.headers.get('content-type') || '',
      media: null,
      jsonError: false
    };
    try {
      const payload = await response.json();
      const media = payload?.items?.[0];
      if (media && typeof media === 'object') {
        const user = media.user || media.owner || {};
        result.media = {
          code: media.code,
          shortcode: media.shortcode,
          pk: media.pk,
          id: media.id,
          product_type: media.product_type,
          media_type: media.media_type,
          play_count: media.play_count,
          like_count: media.like_count,
          comment_count: media.comment_count,
          media_repost_count: media.media_repost_count,
          repost_count: media.repost_count,
          taken_at: media.taken_at,
          video_duration: media.video_duration,
          user: {
            pk: user.pk,
            pk_id: user.pk_id,
            id: user.id,
            username: user.username,
            follower_count: user.follower_count
          }
        };
      }
    } catch (_error) {
      result.jsonError = true;
    }
    return result;
  } finally {
    clearTimeout(timeoutId);
  }
}"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def js_round(value: float) -> int:
    """Match JavaScript Math.round for the collector's non-negative values."""
    return math.floor(value + 0.5)


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    korean_date = re.fullmatch(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if korean_date:
        try:
            parsed = datetime(
                int(korean_date.group(1)),
                int(korean_date.group(2)),
                int(korean_date.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
    elif isinstance(value, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", text):
        number = float(value)
        if number < 1_000_000_000_000:
            number *= 1000
        try:
            parsed = datetime.fromtimestamp(number / 1000, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    if parsed.year < 2005 or parsed.year > utc_now().year + 2:
        return None
    return parsed


def is_instagram_reels_surface(value: str) -> bool:
    parsed = urlparse(urljoin("https://www.instagram.com/", value))
    return parsed.hostname is not None and parsed.hostname.endswith("instagram.com") and bool(re.match(r"^/reels?(?:/|$)", parsed.path, re.I))


def is_instagram_hashtag_surface(value: str) -> bool:
    parsed = urlparse(urljoin("https://www.instagram.com/", value))
    if parsed.hostname is None or not parsed.hostname.endswith("instagram.com"):
        return False
    if re.match(r"^/explore/tags/[^/]+/?$", parsed.path, re.I):
        return True
    if not re.match(r"^/explore/search/keyword/?$", parsed.path, re.I):
        return False
    query = parse_qs(parsed.query).get("q", [""])[0].strip()
    return query.startswith("#") and len(query) > 1


def parse_hashtag_query(value: Any) -> list[str]:
    query = str(value or "").strip()
    if not query:
        return []
    parts = re.split(r"\s+or\s+", query, flags=re.I)
    if any(not part.strip() for part in parts):
        raise ValueError("--hashtag-query must join complete hashtag names with OR.")
    unique: dict[str, str] = {}
    for part in parts:
        hashtag = unicodedata.normalize("NFC", part.strip().strip("\"'").lstrip("#").strip())
        if not hashtag or not all(character == "_" or character.isalnum() for character in hashtag):
            raise ValueError(f"Invalid hashtag in --hashtag-query: {part.strip()}")
        unique.setdefault(hashtag.casefold(), hashtag)
    return list(unique.values())


def _hashtags(value: Any) -> list[str]:
    # Python's regex engine has no JavaScript-style Unicode property escape.
    return re.findall(r"#[\w]+", str(value or ""), flags=re.UNICODE)


def hashtag_page_url(hashtag: str) -> str:
    # The generic keyword-search surface can mix accounts, audio and posts,
    # and in current Instagram builds it often leaves the Reel grid empty for
    # broad Korean tags.  Open the tag's own media grid instead.
    return f"https://www.instagram.com/explore/tags/{quote(str(hashtag).strip().lstrip('#'), safe='')}/"


def normalize_reel_url(value: Any) -> dict[str, str] | None:
    parsed = urlparse(urljoin("https://www.instagram.com/", str(value or "")))
    match = re.match(r"^/reels?/([A-Za-z0-9_-]+)/?", parsed.path, re.I)
    if not match:
        return None
    return {"url": f"https://www.instagram.com/reels/{match.group(1)}/", "shortcode": match.group(1)}


def reel_detail_page_url(value: Any) -> str:
    """Return Instagram's singular Reel detail route for browser navigation.

    Storage and deduplication retain the canonical ``/reels/<shortcode>/`` URL,
    while the browser opens ``/reel/<shortcode>/`` because that surface can
    expose the active media's page JSON more completely.
    """
    normalized = normalize_reel_url(value)
    if normalized is None:
        raise ValueError(f"Invalid Instagram Reel URL: {value}")
    return f"https://www.instagram.com/reel/{quote(normalized['shortcode'], safe='')}/"


def filter_new_urls(urls: list[str], history_rows: list[dict[str, Any]]) -> list[str]:
    """Keep discovered URLs that are absent from the collected Reel history."""
    history_urls = {
        normalized["url"] if (normalized := normalize_reel_url(row.get("url"))) else str(row.get("url") or "")
        for row in history_rows
    }
    return [
        url
        for url in urls
        if (normalized["url"] if (normalized := normalize_reel_url(url)) else url) not in history_urls
    ]


def shortcode_to_media_id(value: Any) -> str:
    shortcode = str(value or "").strip()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if not shortcode or any(character not in alphabet for character in shortcode):
        return ""
    media_id = 0
    for character in shortcode:
        media_id = media_id * 64 + alphabet.index(character)
    return str(media_id)


def normalize_search_grid_reel_url(value: Any) -> dict[str, str] | None:
    """Normalize a Reel link discovered on an Instagram search-result card.

    Instagram currently exposes Reel cards as /p/{shortcode}/ links on keyword
    search pages. Call this only for cards that the DOM has already identified
    as video/Reel cards so ordinary photo posts are never converted to Reels.
    """
    normalized = normalize_reel_url(value)
    if normalized:
        return normalized
    parsed = urlparse(urljoin("https://www.instagram.com/", str(value or "")))
    match = re.match(r"^/p/([A-Za-z0-9_-]+)/?", parsed.path, re.I)
    if not match:
        return None
    return {"url": f"https://www.instagram.com/reels/{match.group(1)}/", "shortcode": match.group(1)}


def is_search_grid_card_visible(bounds: dict[str, Any], viewport_width: Any, viewport_height: Any) -> bool:
    """Return whether a card rectangle overlaps both viewport axes."""
    try:
        left = float(bounds.get("left"))
        right = float(bounds.get("right"))
        top = float(bounds.get("top"))
        bottom = float(bounds.get("bottom"))
        width = float(bounds.get("width"))
        height = float(bounds.get("height"))
        viewport_right = float(viewport_width)
        viewport_bottom = float(viewport_height)
    except (TypeError, ValueError):
        return False
    return all(math.isfinite(value) for value in [left, right, top, bottom, width, height, viewport_right, viewport_bottom]) and (
        width > 0 and height > 0 and right > 0 and left < viewport_right and bottom > 0 and top < viewport_bottom
    )


def visible_search_grid_reel_urls(candidates: Iterable[dict[str, Any]]) -> list[str]:
    """Keep only visible Reel cards from the active hashtag search grid."""
    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        bounds = candidate.get("bounds")
        visible = (
            is_search_grid_card_visible(bounds, candidate.get("viewportWidth"), candidate.get("viewportHeight"))
            if isinstance(bounds, dict)
            else bool(candidate.get("visible"))
        )
        if not visible or not candidate.get("gridCard"):
            continue
        normalized = normalize_search_grid_reel_url(candidate.get("href"))
        if normalized and normalized["url"] not in seen:
            seen.add(normalized["url"])
            urls.append(normalized["url"])
    return urls


def parse_metric_count(value: Any) -> int | str:
    compact = re.sub(r"\s+", "", str("" if value is None else value).strip().replace(",", ""))
    match = re.search(r"(\d+(?:\.\d+)?)(천|만|억|[kmb])?", compact, re.I)
    if not match:
        return ""
    multipliers = {"": 1, "천": 1_000, "만": 10_000, "억": 100_000_000, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    number = float(match.group(1)) * multipliers[(match.group(2) or "").lower()]
    return js_round(number) if math.isfinite(number) else ""


def calculate_reaction_rate(view_count: Any, follower_count: Any) -> float | str:
    """Return views divided by followers, or an empty value when unavailable."""
    views = parse_metric_count(view_count)
    followers = parse_metric_count(follower_count)
    if not isinstance(views, int) or not isinstance(followers, int) or followers <= 0:
        return ""
    return views / followers


def calculate_reel_derived_fields(
    current: dict[str, Any], previous: dict[str, Any] | None,
) -> dict[str, float | int | str]:
    """Calculate stable rate and change fields for one collection-history row."""
    reaction_rate = calculate_reaction_rate(
        current.get("view_count"), current.get("follower_count")
    )
    derived: dict[str, float | int | str] = {"reaction_rate": reaction_rate}
    if previous is None:
        return {**derived, **{field: "" for field in REEL_CHANGE_FIELDS}}

    for field in REEL_CHANGE_METRICS:
        current_value = parse_metric_count(current.get(field))
        previous_value = parse_metric_count(previous.get(field))
        derived[f"{field}_change"] = (
            current_value - previous_value
            if isinstance(current_value, int) and isinstance(previous_value, int)
            else ""
        )

    previous_rate = calculate_reaction_rate(
        previous.get("view_count"), previous.get("follower_count")
    )
    derived["reaction_rate_change"] = (
        reaction_rate - previous_rate
        if isinstance(reaction_rate, float) and isinstance(previous_rate, float)
        else ""
    )
    return derived


def is_like_count_marked_unavailable(value: Any) -> bool:
    """Return whether a visible but intentionally undisclosed like count is stored."""
    return str(value or "").strip().upper() == UNAVAILABLE_LIKE_COUNT_MARKER


def should_mark_like_count_unavailable(
    visible_record: dict[str, Any], metadata: dict[str, Any] | None,
) -> bool:
    """Identify a rendered like control that exposes no exact count to this viewer.

    The collector waits for the page's own JSON responses first. Only after
    that wait has completed do we turn this UI state into the explicit ``X``
    marker; an absent control remains a normal missing-metric failure.
    """
    return bool(visible_record.get("likeControlPresent")) and (
        exact_nonnegative_integer((metadata or {}).get("likeCount")) is None
    )


def enrich_reel_collection_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add calculated fields while comparing each Reel with its prior collection."""
    enriched: list[dict[str, Any]] = [{} for _ in rows]
    latest_by_url: dict[str, dict[str, Any]] = {}
    ordered_rows = sorted(
        enumerate(rows),
        key=lambda item: (
            (normalize_reel_url(item[1].get("url")) or {"url": str(item[1].get("url", "") or "")})["url"],
            parse_datetime(item[1].get("collected_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            item[0],
        ),
    )
    for index, row in ordered_rows:
        normalized = normalize_reel_url(row.get("url"))
        url = normalized["url"] if normalized else str(row.get("url", "") or "")
        enriched[index] = {**row, **calculate_reel_derived_fields(row, latest_by_url.get(url))}
        latest_by_url[url] = row
    return enriched


def parse_follower_count(value: Any) -> int | str:
    text = str("" if value is None else value).replace("\u00a0", " ").strip()
    number = r"([0-9][0-9,.]*)\s*(천|만|억|[KMB])?"
    patterns = [
        rf"(?:followers?|팔로워)\s*[:：]?\s*{number}",
        rf"{number}\s*(?:followers?|팔로워)",
        rf"^\s*{number}\s*$",
    ]
    match = next((found for pattern in patterns if (found := re.search(pattern, text, re.I))), None)
    if not match:
        return ""
    unit = (match.group(2) or "").lower()
    multipliers = {"": 1, "천": 1_000, "만": 10_000, "억": 100_000_000, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    number_value = float(match.group(1).replace(",", "")) * multipliers[unit]
    return js_round(number_value) if math.isfinite(number_value) else ""


def first_follower_count(candidates: Iterable[Any]) -> int | str:
    for candidate in candidates:
        count = parse_follower_count(candidate)
        if count != "":
            return count
    return ""


def follower_count_success(count: int, source_field: str) -> dict[str, Any]:
    return {
        "status": "success",
        "followerCount": count,
        "error": "",
        "source": "instagram_web",
        "sourceField": source_field,
    }


def exact_visible_profile_follower_count(value: Any) -> int | None:
    """Read a full integer follower value from the rendered profile header.

    Compact labels such as ``1.6만`` and ``3.2K`` are deliberately rejected:
    their original integer cannot be recovered exactly.  The profile header is
    only used when Instagram visibly renders a complete integer.
    """
    values = value if isinstance(value, (list, tuple, set)) else [value]
    number = r"(?P<count>\d{1,3}(?:,\d{3})*|\d+)"
    suffix = r"(?![\d.]|\s*(?:천|만|억|[KMB]))"
    patterns = [
        re.compile(rf"(?:팔로워|followers?)\s*[:：]?\s*{number}{suffix}", re.I),
        re.compile(rf"{number}{suffix}\s*(?:팔로워|followers?)", re.I),
    ]
    for raw in values:
        text = str(raw or "").replace("\u00a0", " ")
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            try:
                return int(match.group("count").replace(",", ""))
            except ValueError:
                continue
    return None


def exact_visible_profile_post_count(value: Any) -> int | None:
    """Read a full integer post count from the rendered profile header."""
    values = value if isinstance(value, (list, tuple, set)) else [value]
    number = r"(?P<count>\d{1,3}(?:,\d{3})*|\d+)"
    suffix = r"(?![\d.]|\s*(?:천|만|억|[KMB]))"
    patterns = [
        re.compile(rf"(?:게시물|posts?)\s*[:：]?\s*{number}{suffix}", re.I),
        re.compile(rf"{number}{suffix}\s*(?:게시물|posts?)", re.I),
    ]
    for raw in values:
        text = str(raw or "").replace("\u00a0", " ")
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            try:
                return int(match.group("count").replace(",", ""))
            except ValueError:
                continue
    return None


def exact_visible_profile_following_count(value: Any) -> int | None:
    """Read a full integer following value from the rendered profile header."""
    values = value if isinstance(value, (list, tuple, set)) else [value]
    number = r"(?P<count>\d{1,3}(?:,\d{3})*|\d+)"
    suffix = r"(?![\d.]|\s*(?:천|만|억|[KMB]))"
    patterns = [
        re.compile(rf"(?:팔로우|following)\s*[:：]?\s*{number}{suffix}", re.I),
        re.compile(rf"{number}{suffix}\s*(?:팔로우|following)", re.I),
    ]
    for raw in values:
        text = str(raw or "").replace("\u00a0", " ")
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            try:
                return int(match.group("count").replace(",", ""))
            except ValueError:
                continue
    return None


def exact_profile_post_count(user: dict[str, Any]) -> int | None:
    """Use the API's full media count when available, otherwise return None."""
    for field in ["media_count", "post_count", "posts_count"]:
        count = exact_nonnegative_integer(user.get(field))
        if count is not None:
            return count
    return None


def profile_category_from_data(user: dict[str, Any]) -> str:
    """Return Instagram's professional-account category when it is supplied."""
    for field in ["category_name", "business_category_name", "professional_category", "category"]:
        candidate = user.get(field)
        if isinstance(candidate, dict):
            candidate = candidate.get("name") or candidate.get("title") or candidate.get("label")
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def profile_category_from_visible_text(value: Any) -> str:
    """Keep visible professional categories such as ``의류(브랜드)`` as user data."""
    values = value if isinstance(value, (list, tuple, set)) else [value]
    for raw in values:
        for line in re.split(r"[\r\n]+", str(raw or "")):
            text = re.sub(r"\s+", " ", line).strip()
            if PROFILE_CATEGORY_TEXT_PATTERN.fullmatch(text):
                return text
    return ""


def follower_count_from_instagram_data(value: Any, username: str, depth: int = 0) -> int | str:
    if not isinstance(value, (dict, list)) or depth > 20:
        return ""
    if isinstance(value, dict):
        expected = str(username or "").lower()
        candidate_username = str(value.get("username") or value.get("user_name") or "").lower()
        if candidate_username and candidate_username == expected:
            edge = value.get("edge_followed_by") if isinstance(value.get("edge_followed_by"), dict) else {}
            for candidate in [value.get("follower_count"), value.get("followers_count"), edge.get("count")]:
                count = js_round(float(candidate)) if isinstance(candidate, (int, float)) else parse_follower_count(candidate)
                if isinstance(count, int) and count >= 0:
                    return count
        children = value.values()
    else:
        children = value
    for child in children:
        found = follower_count_from_instagram_data(child, username, depth + 1)
        if found != "":
            return found
    return ""


def profile_snapshot_from_instagram_data(value: Any, username: str, depth: int = 0) -> dict[str, Any]:
    """Find an exact target-profile snapshot in a response the page initiated."""
    if not isinstance(value, (dict, list)) or depth > 20:
        return {}
    expected = str(username or "").strip().lstrip("@").casefold()
    if isinstance(value, dict):
        observed_username = str(value.get("username") or value.get("user_name") or "").strip().lstrip("@").casefold()
        if expected and observed_username == expected:
            edge = _dict(value.get("edge_followed_by"))
            following = _dict(value.get("edge_follow"))
            follower_count = next(
                (
                    count
                    for raw in [value.get("follower_count"), value.get("followers_count"), edge.get("count")]
                    if (count := exact_nonnegative_integer(raw)) is not None
                ),
                None,
            )
            if follower_count is not None:
                return {
                    "followerCount": follower_count,
                    "followingCount": next(
                        (
                            count
                            for raw in [value.get("following_count"), value.get("follows_count"), following.get("count")]
                            if (count := exact_nonnegative_integer(raw)) is not None
                        ),
                        None,
                    ),
                    "biography": str(value.get("biography") or ""),
                    "profile_category": profile_category_from_data(value),
                    "postCount": exact_profile_post_count(value),
                }
        children = value.values()
    else:
        children = value
    for child in children:
        snapshot = profile_snapshot_from_instagram_data(child, username, depth + 1)
        if snapshot:
            return snapshot
    return {}


def truncate_caption(value: Any, max_characters: int = 300) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    return normalized if len(normalized) <= max_characters else f"{normalized[:max_characters]}..."


def extract_hashtags(value: Any, additional_tags: Iterable[Any] = ()) -> str:
    unique: dict[str, str] = {}
    for candidate in [*_hashtags(value), *additional_tags]:
        match = re.search(r"#[\w]+", str(candidate or ""), re.UNICODE)
        if match:
            unique.setdefault(match.group(0).casefold(), match.group(0))
    return " ".join(unique.values())


def caption_without_hashtags(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"#[\w]+", " ", str(value or ""), flags=re.UNICODE)).strip()


def normalize_upload_time(value: Any) -> str:
    parsed = parse_datetime(value)
    return isoformat_utc(parsed) if parsed else ""


def format_audio_name(artist_value: Any, title_value: Any) -> str:
    artist = str(artist_value or "").strip()
    title = str(title_value or "").strip()
    if not title:
        return ""
    return title if not artist or artist.casefold() in title.casefold() else f"{artist} · {title}"


def has_ad_signal(value: Any) -> bool:
    if value is True or value == 1:
        return True
    text = str(value or "").strip()
    return bool(text and not re.fullmatch(r"(?:false|0|null|none)", text, re.I))


def media_is_advertisement(media: dict[str, Any]) -> bool:
    direct = [media.get(field) for field in ["is_ad", "is_sponsored", "is_paid_partnership", "ad_id", "ad_client_token", "ad_action", "sponsored_label"]]
    if any(has_ad_signal(item) for item in direct):
        return True
    commercial_type = f"{media.get('commerciality_status', '')} {media.get('commercial_content_type', '')}".strip()
    if commercial_type and not re.search(r"(?:^|\s)(?:not[_ -]?commercial|organic|none|false)(?:\s|$)", commercial_type, re.I) and re.search(r"(?:sponsor|paid|advertis|commercial)", commercial_type, re.I):
        return True
    for field in ["sponsor_tags", "paid_partnership_info", "affiliate_info"]:
        value = media.get(field)
        if (isinstance(value, list) and value) or (not isinstance(value, list) and has_ad_signal(value)):
            return True
    return False


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def exact_nonnegative_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def exact_nonnegative_number(value: Any) -> int | float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def exact_follower_result_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    candidate = metadata or {}
    count = exact_nonnegative_integer(candidate.get("followerCount"))
    if count is None or candidate.get("followerSourceField") != "follower_count":
        return None
    return follower_count_success(count, "follower_count")


async def resolve_follower_result(
    metadata: dict[str, Any] | None,
    cache_key: str,
    cache: dict[str, dict[str, Any]],
    profile_lookup: Callable[[], Any],
) -> dict[str, Any]:
    direct = exact_follower_result_from_metadata(metadata)
    if direct is not None:
        if cache_key:
            cache[cache_key] = direct
        return direct
    if cache_key and cache_key in cache:
        return cache[cache_key]
    result = await profile_lookup()
    if (
        cache_key
        and result.get("status") == "success"
        and exact_nonnegative_integer(result.get("followerCount")) is not None
    ):
        cache[cache_key] = result
    return result


def has_exact_engagement_metadata(metadata: dict[str, Any] | None) -> bool:
    candidate = metadata or {}
    # Instagram does not expose a repost aggregate for every Reel. A missing
    # field is kept blank, so likes and comments remain the required fields.
    return all(
        exact_nonnegative_integer(candidate.get(field)) is not None
        for field in ["likeCount", "commentCount"]
    )


def metadata_from_media(
    media: dict[str, Any],
    *,
    include_follower_count: bool = False,
) -> dict[str, Any]:
    user = _dict(media.get("user") or media.get("owner"))
    direct_user = _dict(media.get("user"))
    clips = _dict(media.get("clips_metadata"))
    music = _dict(clips.get("music_info"))
    asset = _dict(music.get("music_asset_info"))
    original = _dict(clips.get("original_sound_info"))
    music_consumption = _dict(music.get("music_consumption_info"))
    music_artist = _dict(music_consumption.get("ig_artist"))
    original_artist = _dict(original.get("ig_artist") or original.get("artist"))
    location = _dict(media.get("location") or media.get("location_info"))
    caption = _dict(media.get("caption"))
    upload_candidates = [media.get(field) for field in ["taken_at", "taken_at_timestamp", "datePublished", "uploadDate", "published_at", "publish_time", "creation_time", "created_at"]]
    play_count = exact_nonnegative_integer(media.get("play_count"))
    video_duration = exact_nonnegative_number(
        media.get("video_duration")
        if media.get("video_duration") is not None
        else clips.get("video_duration")
    )
    follower_count = (
        exact_nonnegative_integer(direct_user.get("follower_count"))
        if include_follower_count
        else None
    )
    repost_value = media.get("media_repost_count") if media.get("media_repost_count") is not None else media.get("repost_count")
    repost_count = exact_nonnegative_integer(repost_value)
    return {
        "userId": str(user.get("pk") or user.get("pk_id") or user.get("id") or "").strip(),
        "username": str(user.get("username") or "").strip(),
        "caption": str(caption.get("text") or media.get("caption_text") or "").strip(),
        "audioName": format_audio_name(asset.get("display_artist") or asset.get("artist_name") or music_artist.get("username"), asset.get("title") or asset.get("song_name"))
        or format_audio_name(original_artist.get("username") or user.get("username"), original.get("original_audio_title") or original.get("audio_name") or original.get("title")),
        "locationName": str(location.get("name") or location.get("short_name") or media.get("location_name") or "").strip(),
        "ad": media_is_advertisement(media),
        "uploadedAt": next((normalized for item in upload_candidates if (normalized := normalize_upload_time(item))), ""),
        "videoDurationSeconds": video_duration,
        # The Reel permalink feed omits views, while the creator's Reels
        # connection exposes the exact integer as play_count.
        "viewCount": play_count,
        "viewSourceField": "play_count" if play_count is not None else None,
        "followerCount": follower_count,
        "followerSourceField": "follower_count" if follower_count is not None else None,
        "likeCount": exact_nonnegative_integer(media.get("like_count")),
        "commentCount": exact_nonnegative_integer(media.get("comment_count")),
        "repostCount": repost_count,
        "isReel": media_is_reel(media),
    }


def media_is_reel(media: dict[str, Any]) -> bool:
    product_type = str(media.get("product_type") or media.get("productType") or "").strip().casefold()
    return bool(
        product_type in {"clips", "clip", "reel", "reels"}
        or media.get("is_clips_media") is True
        or media.get("is_reel") is True
        or isinstance(media.get("clips_metadata"), dict) and bool(media.get("clips_metadata"))
    )


def merge_reel_metadata(current: dict[str, Any] | None, found: dict[str, Any] | None) -> dict[str, Any]:
    existing = current or {}
    observed = found or {}
    metric_fields = {"viewCount", "followerCount", "likeCount", "commentCount", "repostCount"}
    merged: dict[str, Any] = {}
    for field in ["userId", "username", "caption", "audioName", "locationName", "ad", "uploadedAt", "videoDurationSeconds", "viewCount", "viewSourceField", "followerCount", "followerSourceField", "likeCount", "commentCount", "repostCount", "isReel"]:
        if field in {"ad", "isReel"}:
            merged[field] = bool(existing.get(field) or observed.get(field))
        elif field in metric_fields:
            merged[field] = existing.get(field) if exact_nonnegative_integer(existing.get(field)) is not None else observed.get(field)
        elif field == "viewSourceField":
            merged[field] = existing.get(field) if exact_nonnegative_integer(existing.get("viewCount")) is not None else observed.get(field)
        elif field == "followerSourceField":
            merged[field] = existing.get(field) if exact_nonnegative_integer(existing.get("followerCount")) is not None else observed.get(field)
        elif field == "videoDurationSeconds":
            merged[field] = existing.get(field) if exact_nonnegative_number(existing.get(field)) is not None else observed.get(field)
        else:
            merged[field] = existing.get(field) if existing.get(field) not in (None, "") else observed.get(field, "")
    return merged


def merge_direct_reel_metadata(current: dict[str, Any] | None, direct: dict[str, Any] | None) -> dict[str, Any]:
    merged = merge_reel_metadata(current, direct)
    observed = direct or {}
    for field in ["viewCount", "followerCount", "likeCount", "commentCount", "repostCount"]:
        value = exact_nonnegative_integer(observed.get(field))
        if value is not None:
            merged[field] = value
    if exact_nonnegative_integer(observed.get("viewCount")) is not None:
        merged["viewSourceField"] = observed.get("viewSourceField")
    if exact_nonnegative_integer(observed.get("followerCount")) is not None:
        merged["followerSourceField"] = observed.get("followerSourceField")
    duration = exact_nonnegative_number(observed.get("videoDurationSeconds"))
    if duration is not None:
        merged["videoDurationSeconds"] = duration
    return merged


def exact_view_counts_from_metadata(shortcode: str, metadata: dict[str, Any] | None) -> dict[str, int]:
    candidate = metadata or {}
    count = exact_nonnegative_integer(candidate.get("viewCount"))
    if not shortcode or count is None or candidate.get("viewSourceField") not in {"play_count", "view_count", "visible_dom", "profile_grid_dom"}:
        return {}
    return {shortcode: count}


def direct_reel_info_diagnostic_message(diagnostic: dict[str, str] | None, missing_field: str) -> str:
    result = diagnostic or {}
    if result.get("status") == "success":
        return f"Direct Reel info returned HTTP 200 but did not include an exact raw {missing_field}."
    return str(result.get("error") or "").strip()


def _parse_direct_reel_info_payload(raw_body: Any) -> dict[str, Any] | None:
    """Parse a direct-endpoint payload, tolerating a harmless XSSI prefix."""
    body = str(raw_body or "").lstrip("\ufeff \t\r\n")
    candidates = [body]
    object_start, object_end = body.find("{"), body.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(body[object_start:object_end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _direct_reel_info_invalid_json_error(endpoint: dict[str, Any]) -> str:
    content_type = str(endpoint.get("contentType") or "").split(";", 1)[0].strip()
    body_length = len(str(endpoint.get("text") or ""))
    details = [part for part in [content_type, f"{body_length} bytes" if body_length else ""] if part]
    suffix = f" ({', '.join(details)})" if details else ""
    return f"Direct Reel info returned an invalid JSON response{suffix}."


def is_direct_reel_info_html_response(diagnostic: dict[str, str] | None) -> bool:
    """Return whether Instagram served a web document instead of the API JSON."""
    return (diagnostic or {}).get("status") == "html_response"


def _set_direct_reel_info_diagnostic(
    diagnostic: dict[str, str] | None,
    status: str,
    error: str = "",
) -> None:
    if diagnostic is not None:
        diagnostic.clear()
        diagnostic.update({"status": status, "error": error})


def is_direct_reel_info_access_denied(diagnostic: dict[str, str] | None) -> bool:
    """Identify request statuses that must not be retried in the same session."""
    result = diagnostic or {}
    return result.get("status") == "http_error" and bool(
        re.search(r"\bHTTP\s+(?:401|403|429)\b", str(result.get("error") or ""))
    )


def collect_reel_metadata(
    value: Any,
    destination: dict[str, dict[str, Any]],
    depth: int = 0,
    *,
    include_follower_count: bool = False,
) -> None:
    if not isinstance(value, (dict, list)) or depth > 16:
        return
    if isinstance(value, dict):
        shortcode = str(value.get("code") or value.get("shortcode") or value.get("media_code") or "")
        if re.fullmatch(r"[A-Za-z0-9_-]+", shortcode):
            merged = merge_reel_metadata(
                destination.get(shortcode),
                metadata_from_media(value, include_follower_count=include_follower_count),
            )
            if any(merged.values()):
                destination[shortcode] = merged
        children = value.values()
    else:
        children = value
    for child in children:
        if isinstance(child, (dict, list)):
            collect_reel_metadata(
                child,
                destination,
                depth + 1,
                include_follower_count=include_follower_count,
            )


async def _collect_reel_metadata_from_response(response: Any, destination: dict[str, dict[str, Any]]) -> None:
    """Merge metadata from Instagram JSON and Fetch/XHR response bodies.

    Instagram sometimes serves a JSON GraphQL payload with a non-JSON content
    type (notably ``text/plain``).  Those Fetch/XHR responses still carry the
    same passive ``play_count`` value, so use the transport type as a second
    eligibility signal and tolerate Instagram's XSSI prefix while parsing.
    """
    try:
        parsed = urlparse(response.url)
        content_type = response.headers.get("content-type", "")
        request = getattr(response, "request", None)
        resource_type = str(getattr(request, "resource_type", "") or "").casefold()
        is_json_content = "json" in content_type.casefold()
        is_fetch_or_xhr = resource_type in {"fetch", "xhr"}
        if (
            not parsed.hostname
            or not parsed.hostname.endswith("instagram.com")
            or not (is_json_content or is_fetch_or_xhr)
        ):
            return
        body = await response.text()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = _parse_direct_reel_info_payload(body)
        if not isinstance(payload, (dict, list)):
            return
        collect_reel_metadata(
            payload,
            destination,
            include_follower_count=True,
        )
    except Exception:
        return


async def _collect_profile_snapshot_from_response(
    response: Any,
    username: str,
    destination: dict[str, Any],
) -> None:
    """Read the target profile from a GraphQL/XHR response already in flight."""
    try:
        parsed = urlparse(response.url)
        content_type = response.headers.get("content-type", "")
        request = getattr(response, "request", None)
        resource_type = str(getattr(request, "resource_type", "") or "").casefold()
        if (
            not parsed.hostname
            or not parsed.hostname.endswith("instagram.com")
            or not ("json" in content_type.casefold() or resource_type in {"fetch", "xhr"})
        ):
            return
        body = await response.text()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = _parse_direct_reel_info_payload(body)
        snapshot = profile_snapshot_from_instagram_data(payload, username)
        if snapshot and exact_nonnegative_integer(snapshot.get("followerCount")) is not None:
            destination.update(snapshot)
    except Exception:
        return


async def _cancel_pending_response_tasks(response_tasks: set[asyncio.Task[None]]) -> None:
    pending_tasks = [task for task in response_tasks if not task.done()]
    for task in pending_tasks:
        task.cancel()
    if pending_tasks:
        done, _ = await asyncio.wait(pending_tasks, timeout=0.5)
        if done:
            await asyncio.gather(*done, return_exceptions=True)


def build_collected_record(record: dict[str, Any], response_metadata: dict[str, Any] | None = None, collected_at: str | None = None) -> dict[str, Any]:
    response = response_metadata or {}
    timestamp = collected_at or isoformat_utc()
    title_candidate = str(record.get("title", "")).strip()
    inferred_username = (
        title_candidate
        if not any([response.get("userId"), response.get("username"), record.get("userId"), record.get("username"), record.get("audioName"), record.get("uploadedAt")])
        and bool(INSTAGRAM_USERNAME_PATTERN.fullmatch(title_candidate))
        else ""
    )
    full_caption = response.get("caption") or ("" if inferred_username else title_candidate)
    uploaded_at = normalize_upload_time(record.get("uploadedAt")) or normalize_upload_time(response.get("uploadedAt"))
    raw_view_count = response.get("viewCount")
    view_count = exact_nonnegative_integer(raw_view_count)
    video_duration_seconds = exact_nonnegative_number(response.get("videoDurationSeconds"))
    if video_duration_seconds is None:
        video_duration_seconds = exact_nonnegative_number(record.get("videoDurationSeconds"))
    return {
        "collected_at": timestamp,
        "url": record.get("url", ""),
        "user_id": response.get("userId") or record.get("userId") or "",
        "username": response.get("username") or record.get("username") or inferred_username,
        "title": truncate_caption(caption_without_hashtags(full_caption), 300),
        "hashtags": extract_hashtags(full_caption, record.get("hashtagTexts") or []),
        "audio_name": response.get("audioName") or record.get("audioName") or "",
        "location_name": response.get("locationName") or record.get("locationName") or "",
        "ad": "true" if response.get("ad") is True or record.get("ad") is True else "false",
        "uploaded_at": uploaded_at,
        "video_duration_seconds": video_duration_seconds if video_duration_seconds is not None else "",
        "days_since_upload": days_since_upload(uploaded_at, timestamp),
        "view_count": view_count,
        "like_count": exact_nonnegative_integer(response.get("likeCount")),
        "comment_count": exact_nonnegative_integer(response.get("commentCount")),
        "share_count": "",
        "repost_count": exact_nonnegative_integer(response.get("repostCount")),
        "saved_count": "",
        "follower_count": "",
    }


def build_hashtag_grid_candidate_record(
    url: str,
    response_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a Reel record from data already rendered on a hashtag grid.

    The grid card URL contains the same shortcode as the canonical Reel URL.
    Do not navigate to the singular ``/reel/`` detail route just to turn that
    shortcode into a record: the current tag page has already requested any
    embedded/GraphQL data that is available for the card.
    """
    normalized = normalize_search_grid_reel_url(url)
    if normalized is None:
        return None
    metadata = response_metadata or {}
    return {
        **normalized,
        "userId": metadata.get("userId", ""),
        "username": metadata.get("username", ""),
        "title": metadata.get("caption", ""),
        "hashtagTexts": [],
        "audioName": metadata.get("audioName", ""),
        "locationName": metadata.get("locationName", ""),
        "ad": metadata.get("ad") is True,
        "uploadedAt": metadata.get("uploadedAt", ""),
        "videoDurationSeconds": metadata.get("videoDurationSeconds", ""),
    }


def build_anonymous_refresh_record(existing: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    """Keep prior static Reel data while replacing only freshly observed public metrics."""
    refreshed = {field: existing.get(field, "") for field in CSV_FIELDS}
    # Keep the stored identity: history/cooldown/export grouping intentionally
    # uses the raw URL column, while the history lookup accepts legacy URL forms.
    refreshed["url"] = refreshed["url"] or observed.get("url")
    refreshed["collected_at"] = observed.get("collected_at") or refreshed["collected_at"]
    for field in ["view_count", "like_count", "comment_count", "share_count", "repost_count", "saved_count"]:
        value = observed.get(field)
        refreshed[field] = (
            UNAVAILABLE_LIKE_COUNT_MARKER
            if field == "like_count" and is_like_count_marked_unavailable(value)
            else exact_nonnegative_integer(value)
        )
    # The anonymous refresh is a metrics snapshot. Keep the initial
    # video-duration value instead of treating an unavailable or changed public
    # page representation as a new duration observation.
    follower_count = exact_nonnegative_integer(observed.get("follower_count"))
    refreshed["follower_count"] = follower_count if follower_count is not None else ""
    refreshed["days_since_upload"] = days_since_upload(refreshed.get("uploaded_at"), refreshed.get("collected_at"))
    return refreshed


def build_anonymous_refresh_from_history(rows: Iterable[dict[str, Any]], observed: dict[str, Any]) -> dict[str, Any] | None:
    """Build an anonymous snapshot only when the same canonical URL has prior history."""
    target = normalize_reel_url(observed.get("url"))
    target_url = target["url"] if target else str(observed.get("url") or "")
    matching = [
        row
        for row in rows
        if (normalized := normalize_reel_url(row.get("url"))) and normalized["url"] == target_url
    ]
    if not matching:
        return None
    existing = max(
        matching,
        key=lambda row: parse_datetime(row.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    return build_anonymous_refresh_record(existing, observed)


def build_anonymous_refresh_from_exact_metrics(
    rows: Iterable[dict[str, Any]],
    observed: dict[str, Any],
    shortcode: str,
    view_counts: dict[str, int],
    follower_result: dict[str, Any],
) -> dict[str, Any] | None:
    """Combine the exact creator-Reels view value and a trusted follower result."""
    candidate = dict(observed)
    candidate["view_count"] = exact_nonnegative_integer(view_counts.get(shortcode))
    follower_count = exact_nonnegative_integer(follower_result.get("followerCount"))
    candidate["follower_count"] = (
        follower_count
        if follower_result.get("status") == "success"
        and follower_result.get("sourceField") in {"follower_count", "edge_followed_by.count", "passive_profile_response.follower_count", "profile_header_text"}
        and follower_count is not None
        else ""
    )
    return build_anonymous_refresh_from_history(rows, candidate)


def has_complete_reel_core_data(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    return (
        all(str(record.get(field, "")).strip() for field in ["url", "user_id", "username", "uploaded_at"])
        and all(exact_nonnegative_integer(record.get(field)) is not None for field in ["view_count", "comment_count", "follower_count"])
        and (
            exact_nonnegative_integer(record.get("like_count")) is not None
            or is_like_count_marked_unavailable(record.get("like_count"))
        )
        and str(record.get("ad", "")).lower() in {"true", "false"}
    )


def missing_python_to_android_handoff_fields(record: dict[str, Any] | None) -> tuple[str, ...]:
    """Return Python fields that need a short settling period before Android opens the URL.

    A value may be deliberately empty for a public Reel (for example a
    caption, audio track, or location), so only the identity/static fields
    that make the handoff meaningful and the two web-readable engagement
    counts are required here.  Android remains the source of view, share,
    and repost counts; they are explicitly outside this decision.
    """
    candidate = record or {}
    missing: list[str] = []
    for field in PYTHON_TO_ANDROID_REQUIRED_FIELDS:
        value = candidate.get(field)
        if field in {"like_count", "comment_count"}:
            present = exact_nonnegative_integer(value) is not None
        elif field in {"video_duration_seconds", "days_since_upload"}:
            present = exact_nonnegative_number(value) is not None
        elif field == "ad":
            present = str(value or "").casefold() in {"true", "false"}
        else:
            present = bool(str(value or "").strip())
        if not present:
            missing.append(field)
    return tuple(missing)


def python_to_android_handoff_delay_seconds(record: dict[str, Any] | None) -> float:
    """Wait three seconds only when Python could not complete its Reel handoff data."""
    return (
        PYTHON_TO_ANDROID_RETRY_DELAY_SECONDS
        if missing_python_to_android_handoff_fields(record)
        else 0.0
    )


def compare_python_and_android_counts(
    python_metrics: dict[str, Any] | None,
    android_metrics: dict[str, Any] | None,
) -> dict[str, dict[str, float | int]]:
    """Return count fields that exceed the tolerated cross-surface difference.

    Only exact integer values observed by both collectors are comparable. A
    count of up to 1,000 permits a 10% difference; a larger count permits 5%.
    The larger of the two values is the denominator, which makes the boundary
    conservative when a count crosses 1,000 between the two reads.
    """
    browser = python_metrics or {}
    android = android_metrics or {}
    mismatches: dict[str, dict[str, float | int]] = {}
    for field in PYTHON_ANDROID_COMPARISON_COUNT_FIELDS:
        python_value = exact_nonnegative_integer(browser.get(field))
        android_value = exact_nonnegative_integer(android.get(field))
        if python_value is None or android_value is None:
            continue
        scale = max(python_value, android_value)
        allowed_ratio = 0.10 if scale <= 1_000 else 0.05
        difference = abs(python_value - android_value)
        ratio = difference / scale if scale else 0.0
        if ratio > allowed_ratio:
            mismatches[field] = {
                "python": python_value,
                "android": android_value,
                "difference": difference,
                "difference_ratio": ratio,
                "allowed_ratio": allowed_ratio,
            }
    return mismatches


def apply_exact_metric_results(
    record: dict[str, Any],
    shortcode: str,
    view_counts: dict[str, int],
    follower_result: dict[str, Any],
) -> dict[str, str]:
    if (
        exact_nonnegative_integer(record.get("like_count")) is None
        and not is_like_count_marked_unavailable(record.get("like_count"))
    ):
        return {"status": "exact_like_count_unavailable", "error": "Exact page integer was unavailable for like_count."}
    if exact_nonnegative_integer(record.get("comment_count")) is None:
        return {"status": "exact_comment_count_unavailable", "error": "Exact page integer was unavailable for comment_count."}
    view_count = exact_nonnegative_integer(view_counts.get(shortcode))
    if view_count is None:
        return {"status": "exact_view_unavailable", "error": "Exact play_count was unavailable for the target Reel."}
    follower_count = exact_nonnegative_integer(follower_result.get("followerCount"))
    if (
        follower_result.get("status") != "success"
        or follower_result.get("sourceField") not in {"follower_count", "edge_followed_by.count", "passive_profile_response.follower_count", "profile_header_text"}
        or follower_count is None
    ):
        return {
            "status": "exact_follower_unavailable",
            "error": str(follower_result.get("error") or "Exact follower_count was unavailable for the target username."),
        }
    record["view_count"] = view_count
    record["follower_count"] = follower_count
    return {"status": "success", "error": ""}


def parse_snapshot_field(field: str) -> dict[str, str] | None:
    match = re.match(r"^(\d+(?:st|nd|rd|th) collect|\+\d+(?:Minute|Hour|Day|Weeks)(?:_\d+)?)_(.+)$", str(field))
    if not match or match.group(2) not in CSV_FIELDS:
        return None
    return {"label": match.group(1), "baseField": match.group(2)}


def snapshot_labels(fields: Iterable[str]) -> list[str]:
    labels: list[str] = []
    for field in fields:
        parsed = parse_snapshot_field(field)
        if parsed and parsed["label"] not in labels:
            labels.append(parsed["label"])
    return labels


def elapsed_snapshot_label(initial_timestamp: Any, current_timestamp: Any) -> str:
    initial = parse_datetime(initial_timestamp)
    current = parse_datetime(current_timestamp)
    minutes = max(1, js_round((current - initial).total_seconds() / 60)) if initial and current else 1
    if minutes < 60:
        return f"+{minutes}Minute"
    hours = max(1, js_round(minutes / 60))
    if hours < 24:
        return f"+{hours}Hour"
    days = max(1, js_round(hours / 24))
    return f"+{days}Day" if days < 7 else f"+{max(1, js_round(days / 7))}Weeks"


def days_since_upload(uploaded_timestamp: Any, collected_timestamp: Any) -> float | str:
    uploaded = parse_datetime(uploaded_timestamp)
    collected = parse_datetime(collected_timestamp)
    if not uploaded or not collected:
        return ""
    elapsed = max(0.0, (collected - uploaded).total_seconds() / 86_400)
    return js_round(elapsed * 100) / 100


def is_within_upload_age_days(record: dict[str, Any], max_upload_age_days: float) -> bool:
    if max_upload_age_days <= 0:
        return True
    uploaded = parse_datetime(record.get("uploaded_at"))
    collected = parse_datetime(record.get("collected_at"))
    return bool(uploaded and collected and max(0, (collected - uploaded).total_seconds()) <= max_upload_age_days * 86_400)


def collection_label(collection_number: int) -> str:
    remainder100 = collection_number % 100
    suffix = "th" if 11 <= remainder100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(collection_number % 10, "th")
    return f"{collection_number}{suffix} collect"


def stored_reel_progress_line(
    stored_count: int,
    max_items: int,
    url: str,
    *,
    progress_offset: int = 0,
) -> str:
    """Format progress for Reels that were actually appended to the store."""
    offset = max(0, progress_offset)
    target = str(max_items + offset) if max_items else "전체"
    return f"[{stored_count + offset}/{target}] {url}"


def latest_field_value(row: dict[str, Any], fields: list[str], base_field: str, stop_before_label: str = "") -> str:
    latest = str(row.get(base_field, "") or "")
    for label in snapshot_labels(fields):
        if label == stop_before_label:
            break
        value = str(row.get(f"{label}_{base_field}", "") or "")
        if value:
            latest = value
    return latest


def choose_snapshot_label(row: dict[str, Any], fields: list[str]) -> str:
    labels = snapshot_labels(fields)
    return next((label for label in labels if not row.get(f"{label}_collected_at")), collection_label(len(labels) + 2))


def add_snapshot_columns(fields: list[str], label: str) -> None:
    for field in RECOLLECT_FIELDS:
        snapshot_field = f"{label}_{field}"
        if snapshot_field not in fields:
            fields.append(snapshot_field)


def latest_collection_timestamp(row: dict[str, Any], fields: list[str]) -> datetime | None:
    values = [row.get("collected_at"), *(row.get(f"{label}_collected_at") for label in snapshot_labels(fields))]
    timestamps = [parsed for value in values if (parsed := parse_datetime(value))]
    return max(timestamps) if timestamps else None


def completed_collection_count(row: dict[str, Any], fields: list[str]) -> int:
    values = [row.get("collected_at"), *(row.get(f"{label}_collected_at") for label in snapshot_labels(fields))]
    return sum(parse_datetime(value) is not None for value in values)


def recollect_cooldown_policy(completed_collections: int) -> dict[str, Any]:
    index = max(0, min(len(RECOLLECT_COOLDOWN_SCHEDULE) - 1, int(completed_collections) - 1))
    return RECOLLECT_COOLDOWN_SCHEDULE[index]


def add_calendar_months(timestamp: datetime, months: int) -> datetime:
    month_index = timestamp.month - 1 + months
    year = timestamp.year + month_index // 12
    month = month_index % 12 + 1
    day = min(timestamp.day, calendar.monthrange(year, month)[1])
    return timestamp.replace(year=year, month=month, day=day)


def next_recollect_available_at(previous_timestamp: Any, completed_collections: int) -> datetime | None:
    previous = previous_timestamp if isinstance(previous_timestamp, datetime) else parse_datetime(previous_timestamp)
    if not previous:
        return None
    policy = recollect_cooldown_policy(completed_collections)
    return add_calendar_months(previous, policy["calendar_months"]) if policy.get("calendar_months") else previous + timedelta(seconds=policy["seconds"])


def collected_record_cooldown(existing: dict[str, Any] | None, fields: list[str], record: dict[str, Any]) -> dict[str, Any] | None:
    if not existing:
        return None
    record_time = parse_datetime(record.get("collected_at"))
    previous = latest_collection_timestamp(existing, fields)
    completed = completed_collection_count(existing, fields)
    next_available = next_recollect_available_at(previous, completed)
    policy = recollect_cooldown_policy(completed)
    if record_time and previous and next_available and previous <= record_time < next_available:
        return {
            "addedRow": False,
            "skipped": True,
            "label": "Cooldown",
            "cooldownLabel": policy["label"],
            "nextCollectionAt": isoformat_utc(next_available),
            "secondsSincePreviousCollection": math.floor((record_time - previous).total_seconds()),
        }
    return None


def integrate_collected_record(
    rows: list[dict[str, Any]],
    fields: list[str],
    record: dict[str, Any],
    *,
    enforce_cooldown: bool = True,
    row_by_url: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    existing = row_by_url.get(str(record.get("url", ""))) if row_by_url is not None else next((row for row in rows if row.get("url") == record.get("url")), None)
    if existing is None:
        initial = {field: record.get(field, "") for field in CSV_FIELDS}
        initial["days_since_upload"] = days_since_upload(initial["uploaded_at"], initial["collected_at"])
        rows.append(initial)
        if row_by_url is not None:
            row_by_url[str(record.get("url", ""))] = initial
        return {"addedRow": True, "label": "Initial"}
    cooldown = collected_record_cooldown(existing, fields, record) if enforce_cooldown else None
    if cooldown:
        return cooldown
    for field in ["user_id", "username"]:
        if not existing.get(field) and record.get(field):
            existing[field] = record[field]
    label = choose_snapshot_label(existing, fields)
    add_snapshot_columns(fields, label)
    for field in RECOLLECT_FIELDS:
        value = days_since_upload(record.get("uploaded_at") or existing.get("uploaded_at"), record.get("collected_at")) if field == "days_since_upload" else record.get(field, "")
        current = str(value if value is not None else "")
        if field in {"collected_at", "days_since_upload"}:
            existing[f"{label}_{field}"] = current
        else:
            existing[f"{label}_{field}"] = current
    return {"addedRow": False, "label": label, "elapsed": elapsed_snapshot_label(existing.get("collected_at"), record.get("collected_at"))}


def write_csv_records(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    selected_fields = fields or CSV_FIELDS
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=selected_fields, extrasaction="ignore", lineterminator="\r\n", quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in selected_fields} for row in rows)
        replace_file_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_field_value(field: str, value: Any) -> Any:
    if value in (None, ""):
        return None
    parsed_snapshot = parse_snapshot_field(field)
    base_field = parsed_snapshot["baseField"] if parsed_snapshot else field
    if base_field == "ad":
        return str(value).strip().lower() == "true"
    if base_field in {
        "collection_number",
        "view_count",
        "like_count",
        "comment_count",
        "share_count",
        "repost_count",
        "saved_count",
        "follower_count",
        *REEL_METRIC_CHANGE_FIELDS,
    }:
        text = str(value).strip().replace(",", "")
        return int(text) if re.fullmatch(r"-?\d+", text) else value
    if base_field in {"days_since_previous", "days_since_upload"}:
        text = str(value).strip()
        return float(text) if re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)", text) else value
    if base_field in {"reaction_rate", "reaction_rate_change"}:
        text = str(value).strip()
        return float(text) if re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)", text) else value
    if base_field == "video_duration_seconds":
        text = str(value).strip()
        if re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", text):
            number = float(text)
            return int(number) if number.is_integer() else number
    return value


def write_json_records(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {field: _json_field_value(field, row.get(field, "")) for field in fields}
        for row in rows
    ]
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        replace_file_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_file_with_retry(source: Path, destination: Path, attempts: int = 8) -> None:
    """Atomically replace a file, tolerating brief Windows scanner locks."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt >= attempts:
                raise
            time.sleep(0.1 * attempt)


def write_reel_xlsx(
    path: Path,
    rows: list[dict[str, Any]],
    fields: list[str],
    *,
    layout: str,
    sheet_name: str = "",
    project_rows: bool = True,
) -> Path:
    from exporters.instagram_collector import _xlsx_project_rows, write_xlsx_workbook

    matrix = [fields, *[[str(row.get(field, "") if row.get(field, "") is not None else "") for field in fields] for row in rows]]
    selected_sheet_name = sheet_name or "reels"
    projected = _xlsx_project_rows(selected_sheet_name, matrix) if project_rows else matrix
    try:
        write_xlsx_workbook(path, [(selected_sheet_name, projected)])
        return path
    except PermissionError:
        updated = path.with_name(f"{path.stem}_updated{path.suffix}")
        write_xlsx_workbook(updated, [(selected_sheet_name, projected)])
        print(f"{path.name} 파일이 열려 있어 최신 결과를 {updated.name}에 저장했습니다.", file=sys.stderr)
        return updated


def write_users_xlsx(data_dir: Path | str) -> Path:
    """Export the active user history to matching public CSV and XLSX files."""
    destination = Path(data_dir).resolve()
    fields, rows = read_csv_objects(ensure_user_history(destination))
    if not fields:
        fields = ["user_id", "username", "biography", "profile_category", "post_count", "follower_count", "following_count", "collected_at"]
    public_fields, public_rows = project_public_records("users", rows, fields)
    write_csv_records(destination / "users.csv", public_rows, public_fields)
    return write_reel_xlsx(
        destination / "users.xlsx",
        public_rows,
        public_fields,
        layout="columns",
        sheet_name="users",
        project_rows=False,
    )


def long_rows_to_wide(rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    wide_fields = ["collection_number", *CSV_FIELDS]
    wide_rows: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("url", "")), []).append(row)
    for group in grouped.values():
        ordered = sorted(
            group,
            key=lambda row: (
                int(parse_metric_count(row.get("collection_number")) or 0),
                parse_datetime(row.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        for row in ordered:
            record = {field: row.get(field, "") for field in CSV_FIELDS}
            integrate_collected_record(wide_rows, wide_fields, record, enforce_cooldown=False)
        if ordered:
            wide_rows[-1]["collection_number"] = len(ordered)
    return wide_fields, wide_rows


def project_public_records(
    sheet_name: str,
    rows: list[dict[str, Any]],
    fields: list[str],
) -> tuple[list[str], list[dict[str, str]]]:
    """Apply the workbook's display projection to every public file format."""
    from exporters.instagram_collector import _xlsx_project_rows

    matrix = [
        fields,
        *[
            [str(row.get(field, "") if row.get(field, "") is not None else "") for field in fields]
            for row in rows
        ],
    ]
    projected = _xlsx_project_rows(sheet_name, matrix)
    projected_fields = projected[0]
    return projected_fields, [
        dict(zip(projected_fields, values)) for values in projected[1:]
    ]


def write_long_output_bundle(
    csv_path: Path,
    rows: list[dict[str, Any]],
    fields: list[str],
    *,
    xlsx_layout: str = "columns",
    output_dir: Path | None = None,
) -> dict[str, Path]:
    del xlsx_layout  # Kept as a compatibility argument for older launch scripts.
    destination = output_dir or (
        csv_path.parent.parent
        if csv_path.parent.name == REEL_HISTORY_DIRECTORY
        else csv_path.parent
    )
    # Preserve one record per collection in every public export.  Recollecting
    # a Reel therefore appends a row instead of generating ``2nd collect_*``
    # snapshot columns.
    public_fields = [*fields, *([] if "reaction_rate" in fields else ["reaction_rate"])]
    public_fields = [
        *public_fields,
        *(field for field in REEL_CHANGE_FIELDS if field not in public_fields),
    ]
    public_source_rows = enrich_reel_collection_rows(rows)
    public_fields, public_rows = project_public_records(
        "reels_rows", public_source_rows, public_fields
    )
    public_csv = destination / f"{PUBLIC_REELS_STEM}.csv"
    public_json = destination / f"{PUBLIC_REELS_STEM}.json"
    public_xlsx = destination / f"{PUBLIC_REELS_STEM}.xlsx"
    write_csv_records(public_csv, public_rows, public_fields)
    write_json_records(public_json, public_rows, public_fields)
    return {
        "csv": public_csv,
        "json": public_json,
        "xlsx": write_reel_xlsx(
            public_xlsx,
            public_rows,
            public_fields,
            layout="rows",
            sheet_name="reels",
            project_rows=False,
        ),
    }


def _xlsx_column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if letters is None:
        return 0
    index = 0
    for character in letters.group(0):
        index = index * 26 + ord(character) - 64
    return index - 1


def _read_xlsx_matrix(workbook: Path) -> list[list[str]]:
    main_namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(workbook) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.findall(f".//{main_namespace}t"))
                for item in shared.findall(f"{main_namespace}si")
            ]
        worksheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for source_row in worksheet.findall(f".//{main_namespace}row"):
        cells: dict[int, str] = {}
        for cell in source_row.findall(f"{main_namespace}c"):
            index = _xlsx_column_index(cell.attrib.get("r", "A1"))
            if cell.attrib.get("t") == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(f".//{main_namespace}t"))
            else:
                value_node = cell.find(f"{main_namespace}v")
                value = value_node.text if value_node is not None and value_node.text else ""
                if cell.attrib.get("t") == "s" and value:
                    value = shared_strings[int(value)]
            cells[index] = value
        if cells:
            rows.append([cells.get(index, "") for index in range(max(cells) + 1)])
    return rows


def _xlsx_reel_value(field: str, value: str) -> str:
    snapshot = parse_snapshot_field(field)
    base_field = snapshot["baseField"] if snapshot else field
    text = str(value or "").strip()
    if not text:
        return ""
    if base_field in {"collected_at", "uploaded_at"} and re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)", text):
        local_value = datetime(1899, 12, 30) + timedelta(days=float(text))
        local_zone = timezone(timedelta(hours=9))
        return isoformat_utc(local_value.replace(tzinfo=local_zone))
    if base_field in {"view_count", "like_count", "comment_count", "share_count", "repost_count", "saved_count", "follower_count"}:
        metric = re.match(r"^(-?[\d,]+)(?:\([+-][\d,]+\))?$", text)
        if metric:
            return metric.group(1).replace(",", "")
    return text


def _reel_records_from_xlsx(workbook: Path) -> list[dict[str, str]]:
    matrix = _read_xlsx_matrix(workbook)
    if not matrix:
        return []
    fields = matrix[0]
    if "url" not in fields:
        raise ValueError(f"{workbook.name} does not contain a reels url column.")
    records: list[dict[str, str]] = []
    labels = snapshot_labels(fields)
    for values in matrix[1:]:
        source = {
            field: _xlsx_reel_value(field, values[index] if index < len(values) else "")
            for index, field in enumerate(fields)
        }
        initial = {field: source.get(field, "") for field in CSV_FIELDS}
        if initial["url"]:
            records.append(initial)
        for label in labels:
            snapshot = dict(initial)
            has_snapshot = False
            for field in CSV_FIELDS:
                value = source.get(f"{label}_{field}", "")
                if value:
                    snapshot[field] = value
                    has_snapshot = True
            if has_snapshot and snapshot["url"] and snapshot["collected_at"]:
                records.append(snapshot)
    return records


def _reel_snapshot_key(record: dict[str, Any]) -> tuple[str, str | int]:
    normalized = normalize_reel_url(record.get("url"))
    url = normalized["url"] if normalized else str(record.get("url", "") or "")
    collected_at = parse_datetime(record.get("collected_at"))
    timestamp = int(collected_at.timestamp()) if collected_at else str(record.get("collected_at", "") or "")
    return url, timestamp


async def reconcile_reel_exports(data_dir: Path | str) -> dict[str, Any]:
    """Merge a temporary Reel workbook into raw history, then rebuild public exports."""
    destination = Path(data_dir).resolve()
    history_path = destination / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    updated_path = destination / "reels_updated.xlsx"
    store = await LongReelStore.create(history_path)
    added_snapshots = 0
    filled_values = 0
    if updated_path.exists():
        existing = {_reel_snapshot_key(row): row for row in store.rows}
        for candidate in _reel_records_from_xlsx(updated_path):
            key = _reel_snapshot_key(candidate)
            current = existing.get(key)
            if current is None:
                integrate_long_collected_record(store.rows, candidate, enforce_cooldown=False)
                existing[key] = store.rows[-1]
                added_snapshots += 1
                continue
            for field in CSV_FIELDS:
                if not current.get(field) and candidate.get(field):
                    current[field] = candidate[field]
                    filled_values += 1
        if added_snapshots or filled_values:
            await asyncio.to_thread(write_csv_records, history_path, store.rows, store.fields)
    outputs = await store.export_outputs()
    users_xlsx = await asyncio.to_thread(write_users_xlsx, destination)
    outputs["users_csv"] = destination / "users.csv"
    outputs["users_xlsx"] = users_xlsx
    if updated_path.exists():
        updated_path.unlink()
    return {
        "addedSnapshots": added_snapshots,
        "filledValues": filled_values,
        "updatedWorkbookRemoved": not updated_path.exists(),
        "outputs": outputs,
    }


def long_collected_record_cooldown(
    rows: list[dict[str, Any]],
    record: dict[str, Any],
) -> dict[str, Any] | None:
    matching = [row for row in rows if row.get("url") == record.get("url") and parse_datetime(row.get("collected_at"))]
    if not matching:
        return None
    previous_row = max(matching, key=lambda row: parse_datetime(row.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc))
    previous = parse_datetime(previous_row.get("collected_at"))
    current = parse_datetime(record.get("collected_at"))
    completed = len(matching)
    next_available = next_recollect_available_at(previous, completed)
    policy = recollect_cooldown_policy(completed)
    if previous and current and next_available and previous <= current < next_available:
        return {
            "addedRow": False,
            "skipped": True,
            "label": "Cooldown",
            "cooldownLabel": policy["label"],
            "nextCollectionAt": isoformat_utc(next_available),
            "secondsSincePreviousCollection": math.floor((current - previous).total_seconds()),
        }
    return None


def integrate_long_collected_record(
    rows: list[dict[str, Any]],
    record: dict[str, Any],
    *,
    enforce_cooldown: bool = True,
) -> dict[str, Any]:
    matching = [row for row in rows if row.get("url") == record.get("url")]
    cooldown = long_collected_record_cooldown(rows, record) if enforce_cooldown else None
    if cooldown:
        return cooldown
    previous = max(
        matching,
        key=lambda row: parse_datetime(row.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc),
        default=None,
    )
    collection_number = len(matching) + 1
    label = "Initial" if collection_number == 1 else collection_label(collection_number)
    current = {field: record.get(field, "") for field in CSV_FIELDS}
    current["days_since_upload"] = days_since_upload(current.get("uploaded_at"), current.get("collected_at"))
    previous_time = parse_datetime(previous.get("collected_at")) if previous else None
    current_time = parse_datetime(current.get("collected_at"))
    elapsed = (
        js_round(max(0.0, (current_time - previous_time).total_seconds() / 86_400) * 100) / 100
        if previous_time and current_time
        else ""
    )
    row = {
        "collection_number": collection_number,
        "days_since_previous": elapsed,
        **current,
        **calculate_reel_derived_fields(current, previous),
    }
    rows.append(row)
    return {
        "addedRow": True,
        "label": label,
        "elapsed": elapsed_snapshot_label(previous.get("collected_at"), current.get("collected_at")) if previous else "",
    }


class LongReelStore:
    def __init__(
        self,
        csv_path: Path,
        flush_record_count: int = REEL_STORE_FLUSH_RECORD_COUNT,
        xlsx_layout: str = "columns",
        disable_recollect_cooldown: bool = False,
    ) -> None:
        self.csv_path = csv_path
        self.flush_record_count = flush_record_count
        self.xlsx_layout = xlsx_layout
        self.disable_recollect_cooldown = disable_recollect_cooldown
        self.fields = list(ROW_COLLECTION_FIELDS)
        self.rows: list[dict[str, Any]] = []
        self.journal_path = Path(f"{csv_path}.pending.jsonl")
        self.dirty = False
        self.pending_record_count = 0
        self.lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        csv_path: Path | str,
        flush_record_count: int = REEL_STORE_FLUSH_RECORD_COUNT,
        xlsx_layout: str = "columns",
        disable_recollect_cooldown: bool = False,
    ) -> "LongReelStore":
        store = cls(Path(csv_path), flush_record_count, xlsx_layout, disable_recollect_cooldown)
        migrated_journal: Path | None = None
        if not store.csv_path.exists() and store.csv_path.name == REEL_HISTORY_FILENAME:
            for legacy_name in LEGACY_REEL_HISTORY_FILENAMES:
                legacy_path = store.csv_path.with_name(legacy_name)
                if not legacy_path.exists() or not legacy_path.stat().st_size:
                    continue
                legacy_fields, legacy_rows = read_csv_objects(legacy_path)
                compatible = all(field in {*ROW_COLLECTION_FIELDS, *LEGACY_REEL_DROPPED_FIELDS} for field in legacy_fields)
                if not compatible:
                    continue
                store.rows = enrich_reel_collection_rows([
                    {field: row.get(field, "") for field in store.fields}
                    for row in legacy_rows
                ])
                await asyncio.to_thread(write_csv_records, store.csv_path, store.rows, store.fields)
                legacy_journal = Path(f"{legacy_path}.pending.jsonl")
                if legacy_journal.exists() and legacy_journal.stat().st_size:
                    store.journal_path.write_text(legacy_journal.read_text(encoding="utf-8"), encoding="utf-8")
                    migrated_journal = legacy_journal
                print(f"잠긴 기존 내부 이력을 새 저장소로 승계했습니다: {len(store.rows)}개")
                break
        if store.csv_path.exists() and store.csv_path.stat().st_size:
            fields, rows = read_csv_objects(store.csv_path)
            # Retired columns are read once and omitted on the next atomic write.
            compatible = all(field in {*ROW_COLLECTION_FIELDS, *LEGACY_REEL_DROPPED_FIELDS} for field in fields)
            if compatible:
                store.rows = enrich_reel_collection_rows([
                    {field: row.get(field, "") for field in store.fields}
                    for row in rows
                ])
            else:
                stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
                legacy = store.csv_path.with_name(f"{store.csv_path.stem}_legacy_{stamp}{store.csv_path.suffix}")
                store.csv_path.replace(legacy)
                print(f"기존 형식 CSV 보관: {legacy}")
        await store._recover_journal()
        if migrated_journal is not None:
            migrated_journal.unlink(missing_ok=True)
        return store

    async def _recover_journal(self) -> None:
        if not self.journal_path.exists() or self.journal_path.stat().st_size == 0:
            return
        invalid: list[str] = []
        recovered = 0
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                result = integrate_long_collected_record(self.rows, json.loads(line))
                recovered += int(not result.get("skipped"))
            except (ValueError, TypeError, json.JSONDecodeError):
                invalid.append(line)
        if recovered:
            await asyncio.to_thread(write_csv_records, self.csv_path, self.rows, self.fields)
            print(f"비정상 종료 전 임시 저장 릴스 복구 완료: {recovered}개")
        if invalid:
            corrupt = Path(f"{self.journal_path}.corrupt_{utc_now().strftime('%Y%m%dT%H%M%SZ')}")
            self.journal_path.replace(corrupt)
            print(f"손상된 임시 저장 줄 {len(invalid)}개를 보관했습니다: {corrupt}", file=sys.stderr)
        else:
            self.journal_path.unlink(missing_ok=True)

    async def append(self, record: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            cooldown = None if self.disable_recollect_cooldown else long_collected_record_cooldown(self.rows, record)
            if cooldown:
                return cooldown
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                file.flush()
            result = integrate_long_collected_record(self.rows, record, enforce_cooldown=False)
            self.dirty = True
            self.pending_record_count += 1
            if self.pending_record_count >= self.flush_record_count:
                await self._flush_locked(False)
            return result

    async def enrich_latest_snapshot(self, record: dict[str, Any]) -> bool:
        """Fill Android-owned fields on the browser row just appended for this URL.

        The first journal entry deliberately contains browser identity/static
        fields.  Android enrichment then updates that same in-memory snapshot
        rather than appending a second collection row for one Reel visit.
        """
        target_url = str(record.get("url", ""))
        target_collected_at = str(record.get("collected_at", ""))
        async with self.lock:
            for row in reversed(self.rows):
                if str(row.get("url", "")) != target_url or str(row.get("collected_at", "")) != target_collected_at:
                    continue
                row.update({field: record.get(field, "") for field in CSV_FIELDS})
                self.rows = enrich_reel_collection_rows(self.rows)
                self.dirty = True
                return True
        return False

    async def _flush_locked(self, force: bool) -> bool:
        if not self.dirty or (not force and self.pending_record_count < self.flush_record_count):
            return False
        saved = self.pending_record_count
        self.rows = await asyncio.to_thread(
            _write_history_preserving_external_android_metrics,
            self.csv_path,
            self.rows,
            self.fields,
        )
        self.journal_path.unlink(missing_ok=True)
        self.dirty = False
        self.pending_record_count = 0
        print(f"{'남은 릴스' if force else f'릴스 {self.flush_record_count}개 체크포인트'} 저장 완료: {saved}개")
        return True

    async def flush(self) -> bool:
        async with self.lock:
            return await self._flush_locked(True)

    async def export_outputs(self) -> dict[str, Path]:
        async with self.lock:
            self.rows, outputs = await asyncio.to_thread(
                _export_history_with_external_android_metrics,
                self.csv_path,
                self.rows,
                self.fields,
                self.xlsx_layout,
            )
            return outputs

    async def merge_external_android_metrics(self) -> int:
        """Refresh this process's rows after the detached worker updates history."""
        async with self.lock:
            if not self.csv_path.exists() or not self.csv_path.stat().st_size:
                return 0
            _fields, disk_rows = await asyncio.to_thread(read_csv_objects, self.csv_path)
            self.rows, changed = _merge_external_android_metrics(self.rows, disk_rows)
            return changed

    def stats(self) -> dict[str, Any]:
        return {"rows": len(self.rows), "pending": self.pending_record_count, "journalPath": str(self.journal_path)}


@dataclass(frozen=True)
class AndroidMetricHandoff:
    """A browser snapshot waiting for the single Android UI worker."""

    record: dict[str, Any]
    missing_python_fields: tuple[str, ...] = ()
    delay_seconds: float = 0.0


class AndroidMetricPipeline:
    """Enrich saved browser snapshots without blocking subsequent browser captures.

    Android UIAutomator can operate only one app surface at a time, so this
    pipeline deliberately has one worker.  Python can nevertheless continue
    discovering and saving the next Reel while that worker opens the prior
    Reel URL and writes its metrics back to the matching snapshot.
    """

    def __init__(
        self,
        *,
        enricher: AndroidReelMetricsEnricher,
        store: LongReelStore,
        on_result: Callable[[dict[str, Any], AndroidMetricResult], Awaitable[None]],
        metrics_required: bool = False,
        data_dir: Path | str | None = None,
    ) -> None:
        self.enricher = enricher
        self.store = store
        self.on_result = on_result
        self.metrics_required = metrics_required
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else None
        self.queue: asyncio.Queue[AndroidMetricHandoff | None] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None
        self.processing = 0
        self.queued = 0
        self.deferred = 0
        self._closed = False
        self._required_error: RuntimeError | None = None
        self.consecutive_failures = 0

    @property
    def backlog(self) -> int:
        return self.queue.qsize() + self.processing

    def start(self) -> None:
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())

    def enqueue(
        self,
        record: dict[str, Any],
        *,
        missing_python_fields: Sequence[str] = (),
        delay_seconds: float = 0.0,
    ) -> None:
        if self._closed:
            raise RuntimeError("Android metric pipeline is already closed.")
        self.start()
        normalized_delay = max(0.0, float(delay_seconds))
        self.queued += 1
        self.deferred += int(normalized_delay > 0)
        self.queue.put_nowait(
            AndroidMetricHandoff(
                record=dict(record),
                missing_python_fields=tuple(missing_python_fields),
                delay_seconds=normalized_delay,
            )
        )

    async def _run(self) -> None:
        while True:
            handoff = await self.queue.get()
            try:
                if handoff is None:
                    return
                self.processing += 1
                if handoff.delay_seconds:
                    missing = ", ".join(handoff.missing_python_fields)
                    print(
                        "Python handoff incomplete "
                        f"({missing}); Android URL collection will start in {handoff.delay_seconds:g}s."
                    )
                    await asyncio.sleep(handoff.delay_seconds)
                attempts = 0
                result = AndroidMetricResult(status="unavailable", error="Android retry limit exhausted.")
                while attempts < ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL:
                    attempts += 1
                    try:
                        result = await asyncio.to_thread(
                            self.enricher.enrich,
                            str(handoff.record.get("url", "")),
                        )
                    except Exception as error:
                        result = AndroidMetricResult(status="unavailable", error=str(error)[:500])
                    if result.status == "collected":
                        mismatches = compare_python_and_android_counts(handoff.record, result.metrics)
                        if not mismatches:
                            break
                        result = AndroidMetricResult(
                            status="unavailable",
                            error=(
                                "Python/Android count mismatch: "
                                + ", ".join(sorted(mismatches))
                            ),
                        )
                    if attempts < ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL:
                        await asyncio.sleep(0.1)
                try:
                    enriched = merge_android_metrics(handoff.record, result)
                    snapshot_updated = await self.store.enrich_latest_snapshot(enriched)
                except Exception as error:
                    snapshot_updated = False
                    result = AndroidMetricResult(status="unavailable", error=str(error)[:500])
                if not snapshot_updated:
                    result = AndroidMetricResult(
                        status="unavailable",
                        error="The browser Reel snapshot disappeared before Android metric enrichment.",
                    )
                try:
                    await self.on_result(handoff.record, result)
                except Exception as error:
                    # A reporting failure must not strand the remaining
                    # Android queue items after Python has moved on.
                    print(f"Android metric status reporting failed: {error}", file=sys.stderr)
                if result.status == "collected":
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures += 1
                if self.consecutive_failures >= COLLECTION_MAX_CONSECUTIVE_FAILURES:
                    reason = f"{COLLECTION_MAX_CONSECUTIVE_FAILURES} consecutive Android Reel metric jobs failed."
                    if self.data_dir is not None:
                        request_collection_stop(
                            self.data_dir,
                            source="android",
                            reason=reason,
                            consecutive_failures=self.consecutive_failures,
                            url=str(handoff.record.get("url", "")),
                        )
                    self._required_error = RuntimeError(reason)
                    while not self.queue.empty():
                        discarded = self.queue.get_nowait()
                        self.queue.task_done()
                        if discarded is None:
                            break
                    return
            finally:
                if handoff is not None:
                    self.processing -= 1
                self.queue.task_done()

    async def close(self) -> None:
        if self._closed:
            if self._required_error is not None:
                raise self._required_error
            return
        self.start()
        await self.queue.join()
        await self.queue.put(None)
        assert self.worker is not None
        await self.worker
        self._closed = True
        if self._required_error is not None:
            raise self._required_error


def process_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class AtomicProcessLock:
    """A small cross-process lock for one JSON/CSV writer at a time."""

    def __init__(self, path: Path, *, wait_seconds: float = 0.0) -> None:
        self.path = path
        self.wait_seconds = max(0.0, wait_seconds)
        self.acquired = False

    def acquire(self) -> bool:
        deadline = time.monotonic() + self.wait_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump({"pid": os.getpid(), "started_at": isoformat_utc()}, file)
                    file.write("\n")
                self.acquired = True
                return True
            except FileExistsError:
                try:
                    owner = json.loads(self.path.read_text(encoding="utf-8"))
                    owner_pid = int(owner.get("pid", 0))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    owner_pid = 0
                if not process_is_alive(owner_pid):
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            if int(owner.get("pid", 0)) == os.getpid():
                self.path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self.path.unlink(missing_ok=True)
        finally:
            self.acquired = False


def android_metric_queue_root(data_dir: Path | str) -> Path:
    return Path(data_dir).resolve() / REEL_HISTORY_DIRECTORY / ANDROID_METRIC_QUEUE_DIRECTORY


def collection_stop_path(data_dir: Path | str) -> Path:
    """Path for the durable stop signal shared by browser and Android workers."""
    return Path(data_dir).resolve() / REEL_HISTORY_DIRECTORY / COLLECTION_STOP_FILENAME


def read_collection_stop_request(data_dir: Path | str) -> dict[str, Any] | None:
    value = _read_android_metric_job(collection_stop_path(data_dir))
    return value if value and value.get("stop_requested") is True else None


def request_collection_stop(
    data_dir: Path | str,
    *,
    source: str,
    reason: str,
    consecutive_failures: int,
    url: str = "",
) -> dict[str, Any]:
    """Persist the first failure-limit stop so both independent collectors stop."""
    destination = Path(data_dir).resolve()
    existing = read_collection_stop_request(destination)
    if existing is not None:
        return existing
    payload = {
        "stop_requested": True,
        "requested_at": isoformat_utc(),
        "source": source,
        "reason": reason[:500],
        "consecutive_failures": max(0, int(consecutive_failures)),
        "url": url,
    }
    write_json_atomic(collection_stop_path(destination), payload)
    append_collection_log(
        destination,
        source,
        "collection_stopped_failure_limit",
        stop_source=source,
        requested_at=payload["requested_at"],
        reason=payload["reason"],
        consecutive_failures=payload["consecutive_failures"],
        url=url,
    )
    return payload


def clear_collection_stop_request(data_dir: Path | str) -> None:
    """A new user-started collector run is allowed to begin fresh."""
    collection_stop_path(data_dir).unlink(missing_ok=True)


def _android_metric_queue_paths(data_dir: Path | str) -> dict[str, Path]:
    root = android_metric_queue_root(data_dir)
    return {
        "root": root,
        "pending": root / "pending",
        "working": root / "working",
        "completed": root / "completed",
        "hashtag_completed": root / "hashtag_completed",
        "worker_lock": root / "worker.lock.json",
        "history_lock": root / "history.lock.json",
        "status": root / "status.json",
    }


def android_metric_queue_counts(data_dir: Path | str) -> dict[str, int]:
    paths = _android_metric_queue_paths(data_dir)
    return {
        name: len(list(paths[name].glob("*.json"))) if paths[name].exists() else 0
        for name in ("pending", "working", "completed")
    }


def enqueue_android_metric_job(
    data_dir: Path | str,
    record: dict[str, Any],
    *,
    missing_python_fields: Sequence[str] = (),
    delay_seconds: float = 0.0,
    adb_path: Path | str | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = 0.35,
) -> str:
    """Atomically persist a URL for the independent Android worker."""
    url = str(record.get("url", "")).strip()
    collected_at = str(record.get("collected_at", "")).strip()
    if not url or not collected_at:
        raise ValueError("Android metric jobs require a Reel URL and collected_at timestamp.")
    paths = _android_metric_queue_paths(data_dir)
    paths["pending"].mkdir(parents=True, exist_ok=True)
    job_id = f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}"
    payload = {
        "job_id": job_id,
        "queued_at": isoformat_utc(),
        "target": {"url": url, "collected_at": collected_at},
        "python_metrics": {
            field: record.get(field, "")
            for field in PYTHON_ANDROID_COMPARISON_COUNT_FIELDS
        },
        "missing_python_fields": list(missing_python_fields),
        "delay_seconds": max(0.0, float(delay_seconds)),
        "android": {
            "adb_path": str(adb_path) if adb_path else "",
            "device_id": str(device_id or ""),
            "ui_delay_seconds": max(0.1, float(ui_delay_seconds)),
        },
    }
    write_json_atomic(paths["pending"] / f"{job_id}.json", payload)
    return job_id


def enqueue_android_hashtag_post_count_job(
    data_dir: Path | str,
    hashtags: Sequence[str],
    *,
    adb_path: Path | str | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = 0.35,
) -> str:
    """Queue Android search-result tag totals alongside browser candidate discovery."""
    normalized = [str(hashtag).strip().lstrip("#") for hashtag in hashtags if str(hashtag).strip().lstrip("#")]
    if not normalized:
        raise ValueError("Android hashtag jobs require at least one hashtag.")
    paths = _android_metric_queue_paths(data_dir)
    paths["pending"].mkdir(parents=True, exist_ok=True)
    job_id = f"hashtags-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}"
    payload = {
        "job_id": job_id,
        "kind": "related_hashtag_post_counts",
        "queued_at": isoformat_utc(),
        "hashtags": normalized,
        "android": {
            "adb_path": str(adb_path) if adb_path else "",
            "device_id": str(device_id or ""),
            "ui_delay_seconds": max(0.1, float(ui_delay_seconds)),
        },
    }
    write_json_atomic(paths["pending"] / f"{job_id}.json", payload)
    return job_id


def enqueue_android_idle_hashtag_post_count_jobs(
    data_dir: Path | str,
    hashtags: Sequence[str],
    *,
    adb_path: Path | str | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = 0.35,
) -> list[str]:
    """Queue one exact Tag post-total lookup per hashtag at idle priority.

    A single long-running Tag job would prevent freshly discovered Reel URLs
    from reaching Android. Independent one-tag jobs let the worker return to
    a Reel as soon as one search result has been read.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for value in hashtags:
        hashtag = str(value).strip().lstrip("#")
        key = hashtag.casefold()
        if not hashtag or key in seen:
            continue
        seen.add(key)
        normalized.append(hashtag)
    if not normalized:
        return []
    paths = _android_metric_queue_paths(data_dir)
    paths["pending"].mkdir(parents=True, exist_ok=True)
    job_ids: list[str] = []
    for hashtag in normalized:
        job_id = f"idle-hashtag-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}"
        payload = {
            "job_id": job_id,
            "kind": "hashtag_post_count",
            "queued_at": isoformat_utc(),
            "hashtags": [hashtag],
            "android": {
                "adb_path": str(adb_path) if adb_path else "",
                "device_id": str(device_id or ""),
                "ui_delay_seconds": max(0.1, float(ui_delay_seconds)),
            },
        }
        write_json_atomic(paths["pending"] / f"{job_id}.json", payload)
        job_ids.append(job_id)
    return job_ids


async def wait_for_android_hashtag_post_count_job(
    data_dir: Path | str,
    job_id: str,
    stop_event: asyncio.Event,
) -> dict[str, Any] | None:
    """Wait only for the related-tag search job before opening candidate Reels."""
    completion = _android_metric_queue_paths(data_dir)["hashtag_completed"] / f"{job_id}.json"
    while not stop_event.is_set():
        result = _read_android_metric_job(completion)
        if result is not None:
            completion.unlink(missing_ok=True)
            return result
        await asyncio.sleep(0.5)
    return None


def _read_android_metric_job(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _recover_android_metric_working_jobs(paths: dict[str, Path]) -> None:
    paths["pending"].mkdir(parents=True, exist_ok=True)
    paths["working"].mkdir(parents=True, exist_ok=True)
    for working in paths["working"].glob("*.json"):
        destination = paths["pending"] / working.name
        if destination.exists():
            working.unlink(missing_ok=True)
            continue
        try:
            os.replace(working, destination)
        except FileNotFoundError:
            pass


def _claim_android_metric_job(paths: dict[str, Path]) -> tuple[Path, dict[str, Any]] | None:
    paths["pending"].mkdir(parents=True, exist_ok=True)
    paths["working"].mkdir(parents=True, exist_ok=True)

    def job_priority(path: Path) -> tuple[int, str]:
        job = _read_android_metric_job(path) or {}
        kind = str(job.get("kind") or "")
        # The related-tag job is awaited before candidate Reel collection.
        # Exact one-tag jobs are best-effort idle work and must yield to every
        # actual Reel metric job.
        priority = (
            0 if kind == "related_hashtag_post_counts"
            else 2 if kind == "hashtag_post_count"
            else 1
        )
        return priority, path.name

    for pending in sorted(
        paths["pending"].glob("*.json"),
        key=job_priority,
    ):
        working = paths["working"] / pending.name
        try:
            os.replace(pending, working)
        except FileNotFoundError:
            continue
        job = _read_android_metric_job(working)
        if job is not None:
            return working, job
        working.unlink(missing_ok=True)
    return None


def _write_android_metric_completion(
    paths: dict[str, Path],
    working_path: Path,
    job: dict[str, Any],
    result: AndroidMetricResult,
) -> None:
    paths["completed"].mkdir(parents=True, exist_ok=True)
    target = job.get("target") if isinstance(job.get("target"), dict) else {}
    payload = {
        "job_id": str(job.get("job_id", working_path.stem)),
        "completed_at": isoformat_utc(),
        "attempts": max(0, int(job.get("attempts", 0) or 0)),
        "target": {
            "url": str(target.get("url", "")),
            "collected_at": str(target.get("collected_at", "")),
        },
        "metrics": dict(result.metrics),
        "audio_name": result.audio_name,
        "like_count_private": result.like_count_private,
        "status": result.status,
        "error": result.error[:500],
    }
    write_json_atomic(paths["completed"] / working_path.name, payload)
    working_path.unlink(missing_ok=True)


def _merge_external_android_metrics(
    local_rows: list[dict[str, Any]],
    external_rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    external_by_target = {
        (str(row.get("url", "")), str(row.get("collected_at", ""))): row
        for row in external_rows
    }
    changed = 0
    for row in local_rows:
        external = external_by_target.get((str(row.get("url", "")), str(row.get("collected_at", ""))))
        if external is None:
            continue
        for field in ANDROID_METRIC_FIELDS:
            value = external.get(field, "")
            if value in (None, "") or str(row.get(field, "")) == str(value):
                continue
            row[field] = value
            changed += 1
    return enrich_reel_collection_rows(local_rows), changed


def _history_data_dir(csv_path: Path) -> Path:
    return csv_path.parent.parent if csv_path.parent.name == REEL_HISTORY_DIRECTORY else csv_path.parent


def _write_history_preserving_external_android_metrics(
    csv_path: Path,
    local_rows: list[dict[str, Any]],
    fields: list[str],
) -> list[dict[str, Any]]:
    paths = _android_metric_queue_paths(_history_data_dir(csv_path))
    lock = AtomicProcessLock(paths["history_lock"], wait_seconds=30)
    if not lock.acquire():
        raise RuntimeError("Timed out waiting to write the Reel history while Android metrics were being applied.")
    try:
        disk_rows: list[dict[str, Any]] = []
        if csv_path.exists() and csv_path.stat().st_size:
            _disk_fields, disk_rows = read_csv_objects(csv_path)
        merged, _changed = _merge_external_android_metrics(local_rows, disk_rows)
        write_csv_records(csv_path, merged, fields)
        return merged
    finally:
        lock.release()


def _export_history_with_external_android_metrics(
    csv_path: Path,
    local_rows: list[dict[str, Any]],
    fields: list[str],
    xlsx_layout: str,
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    paths = _android_metric_queue_paths(_history_data_dir(csv_path))
    lock = AtomicProcessLock(paths["history_lock"], wait_seconds=30)
    if not lock.acquire():
        raise RuntimeError("Timed out waiting to export Reel history while Android metrics were being applied.")
    try:
        disk_rows: list[dict[str, Any]] = []
        if csv_path.exists() and csv_path.stat().st_size:
            _disk_fields, disk_rows = read_csv_objects(csv_path)
        merged, _changed = _merge_external_android_metrics(local_rows, disk_rows)
        return merged, write_long_output_bundle(csv_path, merged, fields, xlsx_layout=xlsx_layout)
    finally:
        lock.release()


def apply_completed_android_metric_jobs(
    data_dir: Path | str,
    *,
    export_outputs: bool = True,
) -> dict[str, int]:
    """Merge completed app metrics into history without racing browser writes."""
    destination = Path(data_dir).resolve()
    paths = _android_metric_queue_paths(destination)
    completed_paths = sorted(paths["completed"].glob("*.json")) if paths["completed"].exists() else []
    if not completed_paths:
        return {"applied": 0, "updated": 0, "pending": 0}
    history_path = destination / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    if not history_path.exists() or not history_path.stat().st_size:
        return {"applied": 0, "updated": 0, "pending": len(completed_paths)}
    lock = AtomicProcessLock(paths["history_lock"], wait_seconds=30)
    if not lock.acquire():
        return {"applied": 0, "updated": 0, "pending": len(completed_paths)}
    try:
        fields, rows = read_csv_objects(history_path)
        target_rows = {
            (str(row.get("url", "")), str(row.get("collected_at", ""))): row
            for row in rows
        }
        applied_paths: list[Path] = []
        updated = 0
        for completed in completed_paths:
            result = _read_android_metric_job(completed)
            target = result.get("target") if isinstance(result, dict) and isinstance(result.get("target"), dict) else {}
            row = target_rows.get((str(target.get("url", "")), str(target.get("collected_at", ""))))
            if row is None or result is None:
                continue
            metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
            for field in ANDROID_METRIC_FIELDS[:-1]:
                if field not in metrics or row.get(field) == metrics[field]:
                    continue
                row[field] = metrics[field]
                updated += 1
            audio_name = str(result.get("audio_name", "") or "")
            if audio_name and row.get("audio_name") != audio_name:
                row["audio_name"] = audio_name
                updated += 1
            if result.get("like_count_private") is True and "like_count" not in metrics and row.get("like_count") != UNAVAILABLE_LIKE_COUNT_MARKER:
                row["like_count"] = UNAVAILABLE_LIKE_COUNT_MARKER
                updated += 1
            applied_paths.append(completed)
        if applied_paths:
            rows = enrich_reel_collection_rows(rows)
            write_csv_records(history_path, rows, fields)
            if export_outputs:
                write_long_output_bundle(history_path, rows, fields)
            for completed in applied_paths:
                completed.unlink(missing_ok=True)
        return {
            "applied": len(applied_paths),
            "updated": updated,
            "pending": len(completed_paths) - len(applied_paths),
        }
    finally:
        lock.release()


def _write_android_metric_worker_status(paths: dict[str, Path], **patch: Any) -> None:
    current = _read_android_metric_job(paths["status"]) or {}
    current.update(patch)
    current["updated_at"] = isoformat_utc()
    write_json_atomic(paths["status"], current)


def python_metrics_for_android_job(data_dir: Path | str, job: dict[str, Any]) -> dict[str, Any]:
    """Use the queued browser snapshot, with history fallback for old jobs."""
    queued = job.get("python_metrics")
    if isinstance(queued, dict):
        return {field: queued.get(field, "") for field in PYTHON_ANDROID_COMPARISON_COUNT_FIELDS}
    target = job.get("target") if isinstance(job.get("target"), dict) else {}
    url = str(target.get("url", ""))
    collected_at = str(target.get("collected_at", ""))
    history_path = Path(data_dir).resolve() / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
    if not url or not history_path.exists() or not history_path.stat().st_size:
        return {}
    _fields, rows = read_csv_objects(history_path)
    for row in reversed(rows):
        if str(row.get("url", "")) == url and str(row.get("collected_at", "")) == collected_at:
            return {field: row.get(field, "") for field in PYTHON_ANDROID_COMPARISON_COUNT_FIELDS}
    return {}


def run_android_metric_worker(
    data_dir: Path | str,
    *,
    adb_path: Path | str | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = 0.35,
    idle_seconds: float = ANDROID_METRIC_QUEUE_IDLE_SECONDS,
) -> int:
    """Drain the durable Android queue; safe to relaunch after any interruption."""
    destination = Path(data_dir).resolve()
    stop_request = read_collection_stop_request(destination)
    if stop_request is not None:
        append_collection_log(
            destination,
            "android",
            "worker_not_started",
            reason=stop_request.get("reason", "collection failure limit reached"),
        )
        return 0
    paths = _android_metric_queue_paths(destination)
    worker_lock = AtomicProcessLock(paths["worker_lock"])
    if not worker_lock.acquire():
        append_collection_log(destination, "android", "worker_not_started", reason="another worker is already running")
        return 0
    _recover_android_metric_working_jobs(paths)
    enricher = AndroidReelMetricsEnricher(
        adb_path=Path(adb_path) if adb_path else None,
        device_id=device_id,
        ui_delay_seconds=ui_delay_seconds,
    )
    processed = collected = unavailable = consecutive_failures = 0
    idle_started = time.monotonic()
    _write_android_metric_worker_status(paths, state="running", pid=os.getpid(), processed=0, collected=0, unavailable=0)
    append_collection_log(
        destination,
        "android",
        "worker_started",
        pid=os.getpid(),
        device_id=device_id or "default",
    )
    try:
        while True:
            claimed = _claim_android_metric_job(paths)
            if claimed is None:
                merge = apply_completed_android_metric_jobs(destination)
                counts = android_metric_queue_counts(destination)
                _write_android_metric_worker_status(
                    paths,
                    state="idle",
                    processed=processed,
                    collected=collected,
                    unavailable=unavailable,
                    consecutive_failures=consecutive_failures,
                    **counts,
                    completed_applied=merge["applied"],
                    completed_updated=merge["updated"],
                    completed_unmatched=merge["pending"],
                )
                if not counts["pending"] and not counts["working"] and time.monotonic() - idle_started >= max(0.5, idle_seconds):
                    return 0
                time.sleep(0.5)
                continue
            idle_started = time.monotonic()
            working_path, job = claimed
            tag_job_kind = str(job.get("kind") or "")
            if tag_job_kind in {"related_hashtag_post_counts", "hashtag_post_count"}:
                hashtags = job.get("hashtags") if isinstance(job.get("hashtags"), list) else []
                is_idle_exact_tag_job = tag_job_kind == "hashtag_post_count"
                hashtag_status = "collected"
                hashtag_error = ""
                append_collection_log(
                    destination,
                    "android",
                    "idle_hashtag_post_search_started" if is_idle_exact_tag_job else "related_hashtag_search_started",
                    job_id=job.get("job_id", working_path.stem),
                    hashtags=hashtags,
                )
                try:
                    collect_tags = (
                        enricher.collect_hashtag_post_counts
                        if is_idle_exact_tag_job
                        else enricher.collect_related_hashtag_post_counts
                    )
                    hashtag_rows = collect_tags([str(value) for value in hashtags])
                    write_hashtag_post_counts(destination, hashtag_rows)
                    collected += sum(row.get("status") == "collected" for row in hashtag_rows)
                    unavailable += sum(row.get("status") != "collected" for row in hashtag_rows)
                    if not any(row.get("status") == "collected" for row in hashtag_rows):
                        hashtag_status = "unavailable"
                        failed_row = next((row for row in hashtag_rows if row.get("error")), {})
                        hashtag_error = str(failed_row.get("error", "No exact hashtag post count was visible."))[:500]
                    for row in hashtag_rows:
                        append_collection_log(
                            destination,
                            "android",
                            "idle_hashtag_post_collected" if is_idle_exact_tag_job else "related_hashtag_collected",
                            query_hashtag=row.get("query_hashtag", ""),
                            hashtag=row.get("hashtag", ""),
                            post_count=row.get("post_count", ""),
                            status=row.get("status", ""),
                            error=row.get("error", ""),
                        )
                except Exception as error:
                    unavailable += len(hashtags)
                    hashtag_status = "unavailable"
                    hashtag_error = str(error)[:500]
                    append_collection_log(
                        destination,
                        "android",
                        "idle_hashtag_post_search_unavailable" if is_idle_exact_tag_job else "related_hashtag_search_unavailable",
                        job_id=job.get("job_id", working_path.stem),
                        hashtags=hashtags,
                        error=hashtag_error,
                    )
                    print(f"Android hashtag post-count worker failed: {error}", file=sys.stderr)
                paths["hashtag_completed"].mkdir(parents=True, exist_ok=True)
                write_json_atomic(
                    paths["hashtag_completed"] / working_path.name,
                    {
                        "job_id": str(job.get("job_id", working_path.stem)),
                        "completed_at": isoformat_utc(),
                        "status": hashtag_status,
                        "error": hashtag_error,
                    },
                )
                working_path.unlink(missing_ok=True)
                processed += 1
                continue
            delay = max(0.0, float(job.get("delay_seconds", 0.0) or 0.0))
            if delay:
                time.sleep(delay)
            target = job.get("target") if isinstance(job.get("target"), dict) else {}
            try:
                attempts = max(0, int(job.get("attempts", 0) or 0))
            except (TypeError, ValueError):
                attempts = 0
            python_metrics = python_metrics_for_android_job(destination, job)
            append_collection_log(
                destination,
                "android",
                "reel_metric_started",
                job_id=job.get("job_id", working_path.stem),
                url=target.get("url", ""),
                delayed_seconds=delay,
                previous_attempts=attempts,
            )
            result = AndroidMetricResult(
                status="unavailable",
                error=f"Android retry limit ({ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL}) was already exhausted.",
            )
            while attempts < ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL:
                attempts += 1
                job["attempts"] = attempts
                write_json_atomic(working_path, job)
                try:
                    result = enricher.enrich(str(target.get("url", "")))
                except Exception as error:
                    result = AndroidMetricResult(status="unavailable", error=str(error)[:500])
                if result.status == "collected":
                    mismatches = compare_python_and_android_counts(python_metrics, result.metrics)
                    if not mismatches:
                        break
                    append_collection_log(
                        destination,
                        "android",
                        "reel_metric_mismatch",
                        job_id=job.get("job_id", working_path.stem),
                        url=target.get("url", ""),
                        attempt=attempts,
                        maximum_attempts=ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL,
                        mismatches=mismatches,
                    )
                    result = AndroidMetricResult(
                        status="unavailable",
                        error=(
                            "Python/Android count mismatch: "
                            + ", ".join(sorted(mismatches))
                        ),
                    )
                append_collection_log(
                    destination,
                    "android",
                    "reel_metric_retry",
                    job_id=job.get("job_id", working_path.stem),
                    url=target.get("url", ""),
                    attempt=attempts,
                    maximum_attempts=ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL,
                    error=result.error,
                )
                if attempts < ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL:
                    time.sleep(max(0.1, float(ui_delay_seconds)))
            _write_android_metric_completion(paths, working_path, job, result)
            append_collection_log(
                destination,
                "android",
                "reel_metric_collected" if result.status == "collected" else "reel_metric_unavailable",
                job_id=job.get("job_id", working_path.stem),
                url=target.get("url", ""),
                status=result.status,
                metrics=result.metrics,
                audio_name=result.audio_name,
                error=result.error,
                attempts=attempts,
            )
            processed += 1
            collected += int(result.status == "collected")
            unavailable += int(result.status != "collected")
            if result.status == "collected":
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            if processed % ANDROID_METRIC_QUEUE_EXPORT_BATCH_SIZE == 0:
                apply_completed_android_metric_jobs(destination)
            if consecutive_failures >= COLLECTION_MAX_CONSECUTIVE_FAILURES:
                stop_request = request_collection_stop(
                    destination,
                    source="android",
                    reason=(
                        f"{COLLECTION_MAX_CONSECUTIVE_FAILURES} consecutive Android Reel metric jobs failed."
                    ),
                    consecutive_failures=consecutive_failures,
                    url=str(target.get("url", "")),
                )
                _write_android_metric_worker_status(
                    paths,
                    state="stopped",
                    processed=processed,
                    collected=collected,
                    unavailable=unavailable,
                    consecutive_failures=consecutive_failures,
                    stop_reason=stop_request["reason"],
                )
                return 2
    finally:
        try:
            merge = apply_completed_android_metric_jobs(destination)
            counts = android_metric_queue_counts(destination)
            _write_android_metric_worker_status(
                paths,
                state="stopped",
                processed=processed,
                collected=collected,
                unavailable=unavailable,
                consecutive_failures=consecutive_failures,
                **counts,
                completed_applied=merge["applied"],
                completed_updated=merge["updated"],
                completed_unmatched=merge["pending"],
            )
            append_collection_log(
                destination,
                "android",
                "worker_stopped",
                processed=processed,
                collected=collected,
                unavailable=unavailable,
                consecutive_failures=consecutive_failures,
                completed_applied=merge["applied"],
                completed_unmatched=merge["pending"],
            )
        finally:
            worker_lock.release()


def start_android_metric_worker(
    data_dir: Path | str,
    *,
    adb_path: Path | str | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = 0.35,
) -> bool:
    """Start one detached worker when this data directory has no live worker."""
    stop_request = read_collection_stop_request(data_dir)
    if stop_request is not None:
        append_collection_log(
            data_dir,
            "android",
            "worker_not_started",
            reason=stop_request.get("reason", "collection failure limit reached"),
        )
        return False
    paths = _android_metric_queue_paths(data_dir)
    owner = _read_android_metric_job(paths["worker_lock"])
    if owner is not None and process_is_alive(int(owner.get("pid", 0) or 0)):
        return False
    launcher = PYTHON_VERSION_ROOT / "scripts" / "instagram_reels_python.py"
    command = [
        sys.executable,
        str(launcher),
        "android-worker",
        "--data-dir",
        str(Path(data_dir).resolve()),
        "--android-ui-delay-seconds",
        str(max(0.1, float(ui_delay_seconds))),
    ]
    if adb_path:
        command.extend(["--android-adb-path", str(adb_path)])
    if device_id:
        command.extend(["--android-device-id", str(device_id)])
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_path = collection_log_path(data_dir, "android")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    append_collection_log(data_dir, "android", "worker_launch_requested", command=command)
    with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
        subprocess.Popen(
            command,
            cwd=PYTHON_VERSION_ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    return True


class CollectorLock:
    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / "collector.lock.json"
        self.acquired = False

    async def acquire(self) -> "CollectorLock":
        self.data_dir.mkdir(parents=True, exist_ok=True)
        content = {"pid": os.getpid(), "started_at": isoformat_utc(), "data_dir": str(self.data_dir)}
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(content, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    existing = {}
                if existing.get("pid") and process_is_alive(int(existing["pid"])):
                    raise RuntimeError(f"Another collector is already using this data directory (PID {existing['pid']}).")
                self.path.unlink(missing_ok=True)
        raise RuntimeError(f"Could not acquire collector lock: {self.path}")

    async def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if current.get("pid") == os.getpid():
                self.path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False


class CollectorStatusReporter:
    def __init__(self, data_dir: Path | str, options: argparse.Namespace) -> None:
        self.destination = Path(data_dir) / "collector_status.json"
        now = isoformat_utc()
        self.status: dict[str, Any] = {
            "state": "starting", "pid": os.getpid(), "started_at": now, "updated_at": now,
            "target_items": options.max_items, "captured": 0, "duplicates": 0,
            "missing": 0, "filtered": 0, "cooldown_skipped": 0,
            "page_recycles": 0, "recovery_failures": 0, "last_reel_url": "", "last_error": "",
        }
        self.last_write = 0.0
        self.lock = asyncio.Lock()

    async def update(self, patch: dict[str, Any], force: bool = False) -> None:
        self.status.update(patch)
        if not force and time.monotonic() - self.last_write < REEL_STATUS_WRITE_INTERVAL_SECONDS:
            return
        async with self.lock:
            self.last_write = time.monotonic()
            self.status["updated_at"] = isoformat_utc()
            await asyncio.to_thread(write_json_atomic, self.destination, self.status)

    async def finish(self, state: str, patch: dict[str, Any] | None = None) -> None:
        self.status.update(patch or {})
        self.status.update({"state": state, "finished_at": isoformat_utc()})
        await self.update({}, force=True)


def write_json_atomic(destination: Path, value: Any) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        replace_file_with_retry(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def merge_follower_data_into_rows(reels: list[dict[str, Any]], users_path: Path) -> int:
    """Fill follower counts in memory so later exports cannot overwrite the merge."""
    if not users_path.exists():
        return 0
    user_fields, users = read_csv_objects(users_path)
    by_id = {row["user_id"]: row for row in users if row.get("user_id")}
    by_username = {row["username"].lower(): row for row in users if row.get("username")}
    latest_by_url: dict[str, dict[str, Any]] = {}
    for reel in reels:
        url = str(reel.get("url", ""))
        if not url:
            continue
        current = latest_by_url.get(url)
        if current is None or (parse_datetime(reel.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc)) > (parse_datetime(current.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc)):
            latest_by_url[url] = reel
    changed = 0
    for reel in reels:
        user = by_id.get(reel.get("user_id", "")) or by_username.get(str(reel.get("username", "")).lower())
        if not user:
            continue
        latest_count = latest_field_value(user, user_fields, "follower_count")
        previous = str(reel.get("follower_count", "") or "")
        is_latest_for_reel = latest_by_url.get(str(reel.get("url", ""))) is reel
        if latest_count and (not previous or is_latest_for_reel):
            reel["follower_count"] = latest_count
        changed += int(str(reel.get("follower_count", "") or "") != previous)
    return changed


def merge_follower_data_into_long_reels(csv_path: Path, users_path: Path) -> int:
    if not csv_path.exists() or not users_path.exists():
        return 0
    fields, reels = read_csv_objects(csv_path)
    output_fields = [field for field in fields if field not in LEGACY_REEL_DROPPED_FIELDS]
    changed = merge_follower_data_into_rows(reels, users_path)
    if changed or output_fields != fields:
        write_csv_records(csv_path, reels, output_fields)
    return changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Instagram Reels and follower counts with a logged-in browser.")
    parser.add_argument("--start-url", default="https://www.instagram.com/reels/")
    parser.add_argument("--max-items", type=int, default=50)
    parser.add_argument("--progress-offset", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--direct-reel-info-wait-seconds", type=float, default=3)
    parser.add_argument("--exact-metric-attempts", type=int, default=EXACT_METRIC_MAX_ATTEMPTS)
    parser.add_argument("--exact-metric-retry-delay-seconds", type=float, default=EXACT_METRIC_RETRY_DELAY_SECONDS)
    parser.add_argument(
        "--hashtag-candidates-per-keyword",
        type=int,
        default=0,
        help="Maximum Reel URL candidates to inspect for each hashtag (0 uses the automatic limit).",
    )
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--no-login", action="store_true", help="Use a temporary anonymous browser without a saved profile.")
    parser.add_argument("--followers-only", action="store_true")
    parser.add_argument("--follower-interval-seconds", type=float, default=8)
    parser.add_argument("--max-upload-age-days", type=float, default=0)
    parser.add_argument("--followers-after-reels", action="store_true")
    parser.add_argument("--page-recycle-items", type=int, default=REEL_PAGE_RECYCLE_ITEM_COUNT)
    parser.add_argument("--checkpoint-items", type=int, default=REEL_STORE_FLUSH_RECORD_COUNT)
    parser.add_argument("--transition-timeout-seconds", type=float, default=REEL_TRANSITION_TIMEOUT_SECONDS)
    parser.add_argument("--direct-concurrency", type=int, default=DIRECT_REEL_CONCURRENCY)
    parser.add_argument("--hashtag-query", default="")
    parser.add_argument(
        "--android-idle-hashtag-query",
        default="",
        help=(
            "Hashtags whose exact Tags post totals Android collects one at a time "
            "while no Reel metric job is waiting. Defaults to --hashtag-query."
        ),
    )
    parser.set_defaults(android_metrics=True)
    parser.add_argument(
        "--android-metrics",
        dest="android_metrics",
        action="store_true",
        help="After browser discovery, open each saved Reel URL in Android and enrich app-only metrics (default).",
    )
    parser.add_argument(
        "--no-android-metrics",
        dest="android_metrics",
        action="store_false",
        help="Use browser-only collection without Android app metric enrichment.",
    )
    parser.add_argument("--android-adb-path", type=Path, help="Android SDK platform-tools adb.exe path.")
    parser.add_argument("--android-device-id", help="ADB serial when more than one Android device is online.")
    parser.add_argument("--android-ui-delay-seconds", type=float, default=0.35)
    parser.add_argument(
        "--android-metrics-required",
        action="store_true",
        help="Stop instead of saving a Reel when Android app metric enrichment is unavailable.",
    )
    parser.add_argument("--urls-file", type=Path)
    parser.add_argument("--data-dir", type=Path, default=PYTHON_VERSION_ROOT / "data_web")
    parser.add_argument("--profile-dir", type=Path, default=PYTHON_VERSION_ROOT / ".instagram_browser_profile")
    parser.set_defaults(storage_layout="history")
    parser.add_argument("--output-stem", default="")
    parser.add_argument("--xlsx-layout", choices=["rows", "columns", "both"], default="columns", help=argparse.SUPPRESS)
    parser.add_argument("--new-urls-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--disable-recollect-cooldown", action="store_true", help=argparse.SUPPRESS)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    options = build_parser().parse_args(argv)
    options.data_dir = options.data_dir.resolve()
    options.profile_dir = options.profile_dir.resolve()
    options.urls_file = options.urls_file.resolve() if options.urls_file else None
    options.android_adb_path = options.android_adb_path.resolve() if options.android_adb_path else None
    if not options.output_stem:
        options.output_stem = PUBLIC_REELS_STEM
    if not re.fullmatch(r"[A-Za-z0-9_-]+", options.output_stem):
        build_parser().error("--output-stem may contain only letters, numbers, underscores, and hyphens.")
    try:
        options.hashtags = parse_hashtag_query(options.hashtag_query)
        options.android_idle_hashtags = parse_hashtag_query(options.android_idle_hashtag_query)
    except ValueError as error:
        build_parser().error(str(error))
    if not options.android_idle_hashtags:
        options.android_idle_hashtags = list(options.hashtags)
    if options.max_items < 0:
        build_parser().error("--max-items must be a non-negative integer.")
    if options.progress_offset < 0:
        build_parser().error("--progress-offset must be a non-negative integer.")
    if options.interval_seconds < 1:
        build_parser().error("--interval-seconds must be at least 1.")
    if not math.isfinite(options.direct_reel_info_wait_seconds) or options.direct_reel_info_wait_seconds < 0:
        build_parser().error("--direct-reel-info-wait-seconds must be 0 or greater.")
    if not 1 <= options.exact_metric_attempts <= 5:
        build_parser().error("--exact-metric-attempts must be an integer between 1 and 5.")
    if not math.isfinite(options.exact_metric_retry_delay_seconds) or options.exact_metric_retry_delay_seconds < 0:
        build_parser().error("--exact-metric-retry-delay-seconds must be 0 or greater.")
    if options.hashtag_candidates_per_keyword < 0:
        build_parser().error("--hashtag-candidates-per-keyword must be 0 or greater.")
    if options.manual and options.background:
        build_parser().error("--manual cannot be combined with --background.")
    if options.follower_interval_seconds < 1:
        build_parser().error("--follower-interval-seconds must be at least 1.")
    if options.max_upload_age_days < 0:
        build_parser().error("--max-upload-age-days must be 0 or greater.")
    if options.page_recycle_items < 0:
        build_parser().error("--page-recycle-items must be a non-negative integer.")
    if options.checkpoint_items < 1:
        build_parser().error("--checkpoint-items must be a positive integer.")
    if not 0.5 <= options.transition_timeout_seconds <= 30:
        build_parser().error("--transition-timeout-seconds must be between 0.5 and 30.")
    if not 1 <= options.direct_concurrency <= 4:
        build_parser().error("--direct-concurrency must be an integer between 1 and 4.")
    if not math.isfinite(options.android_ui_delay_seconds) or not 0.1 <= options.android_ui_delay_seconds <= 5:
        build_parser().error("--android-ui-delay-seconds must be between 0.1 and 5.")
    # Exact view enrichment owns one authenticated creator-Reels page. Serial
    # refresh prevents concurrent navigations from associating a play_count
    # response with the wrong target shortcode.
    options.direct_concurrency = 1
    parsed_start = urlparse(options.start_url)
    if parsed_start.scheme != "https" or not parsed_start.hostname or not parsed_start.hostname.endswith("instagram.com"):
        build_parser().error("--start-url must be an https://www.instagram.com/ URL.")
    return options


def load_reel_urls(file_path: Path) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for line in file_path.read_text(encoding="utf-8-sig").splitlines():
        normalized = normalize_reel_url(line.strip())
        if normalized and normalized["url"] not in seen:
            seen.add(normalized["url"])
            urls.append(normalized["url"])
    if not urls:
        raise RuntimeError(f"No Instagram Reel URLs were found in {file_path}")
    return urls


class CrawlerAccessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def can_skip_anonymous_refresh_access_error(error: CrawlerAccessError, *, no_login: bool) -> bool:
    """Only login walls are skippable during an anonymous URL refresh.

    Rate limits and checkpoints must stop the whole run: continuing after those
    signals would create additional requests. A login wall, on the other hand,
    is specific to the current Reel and does not prove every queued public URL
    is unavailable.
    """
    return bool(no_login and error.code == "login_required")


def _response_status(response: Any) -> int | None:
    status = getattr(response, "status", None)
    return status() if callable(status) else status


@dataclass
class InstagramRateLimitState:
    """Shared stop signal for every page in one browser context."""

    limited: bool = False
    response_url: str = ""
    retry_after_seconds: float | None = None

    def observe(self, response: Any) -> None:
        if _response_status(response) != 429:
            return
        url = str(getattr(response, "url", "") or "")
        hostname = urlparse(url).hostname
        if not hostname or not hostname.endswith("instagram.com"):
            return
        self.limited = True
        self.response_url = url
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("retry-after", "") if hasattr(headers, "get") else ""
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = -1
        self.retry_after_seconds = delay if math.isfinite(delay) and delay >= 0 else None

    def raise_if_limited(self) -> None:
        if not self.limited:
            return
        retry_hint = (
            f" Retry-After: {self.retry_after_seconds:g} seconds."
            if self.retry_after_seconds is not None
            else ""
        )
        raise CrawlerAccessError(
            "rate_limited",
            f"Instagram returned HTTP 429 for {self.response_url or 'a page response'}.{retry_hint}",
        )


_CONTEXT_RATE_LIMIT_STATES: dict[int, InstagramRateLimitState] = {}
_PAGE_RATE_LIMIT_STATES: dict[int, InstagramRateLimitState] = {}


def rate_limit_state_for_context(context: Any) -> InstagramRateLimitState:
    state = getattr(context, "_instagram_collector_rate_limit_state", None)
    if isinstance(state, InstagramRateLimitState):
        return state
    state = InstagramRateLimitState()
    try:
        setattr(context, "_instagram_collector_rate_limit_state", state)
        return state
    except Exception:
        pass
    key = id(context)
    return _CONTEXT_RATE_LIMIT_STATES.setdefault(key, state)


def rate_limit_state_for_page(page: Any) -> InstagramRateLimitState | None:
    state = getattr(page, "_instagram_collector_rate_limit_state", None)
    return state if isinstance(state, InstagramRateLimitState) else _PAGE_RATE_LIMIT_STATES.get(id(page))


def assert_instagram_page_access(page: Any, response: Any = None, *, allow_login: bool = False) -> None:
    if _response_status(response) == 429:
        raise CrawlerAccessError("rate_limited", "Instagram returned HTTP 429.")
    current_url = page.url
    if not allow_login and re.search(r"/accounts/login", current_url, re.I):
        raise CrawlerAccessError("login_required", "Instagram login is required.")
    if not allow_login and re.search(r"/(?:challenge|checkpoint)/", current_url, re.I):
        raise CrawlerAccessError("challenge_required", "Instagram requested an account check.")


async def navigate_with_retries(page: Any, url: str, *, attempts: int = 3, allow_login: bool = False) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            state = rate_limit_state_for_page(page)
            if state is not None:
                state.raise_if_limited()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            assert_instagram_page_access(page, response, allow_login=allow_login)
            return response
        except CrawlerAccessError:
            raise
        except Exception as error:
            last_error = error
            if attempt < attempts:
                await page.wait_for_timeout(attempt * 1_000)
    raise last_error or RuntimeError(f"Failed to open {url}")


def attach_reel_metadata_collector(
    page: Any,
    reel_metadata: dict[str, dict[str, Any]],
    rate_limit_state: InstagramRateLimitState | None = None,
) -> None:
    if rate_limit_state is not None:
        try:
            setattr(page, "_instagram_collector_rate_limit_state", rate_limit_state)
        except Exception:
            _PAGE_RATE_LIMIT_STATES[id(page)] = rate_limit_state

    def on_response(response: Any) -> None:
        if rate_limit_state is not None:
            rate_limit_state.observe(response)
        asyncio.create_task(_collect_reel_metadata_from_response(response, reel_metadata))

    page.on("response", on_response)


async def collect_embedded_reel_metadata(
    page: Any,
    reel_metadata: dict[str, dict[str, Any]],
    *,
    include_follower_count: bool = False,
) -> None:
    try:
        embedded = await page.locator('script[type="application/json"]').all_text_contents()
    except Exception:
        return
    for raw in embedded:
        try:
            collect_reel_metadata(
                json.loads(raw),
                reel_metadata,
                include_follower_count=include_follower_count,
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            pass


async def create_collection_page(
    context: Any,
    url: str,
    reel_metadata: dict[str, dict[str, Any]],
    *,
    allow_login: bool = False,
    keep_open_on_navigation_failure: bool = False,
    rate_limit_state: InstagramRateLimitState | None = None,
) -> Any:
    # A persistent Chromium context normally starts with one blank tab.  In
    # some Windows sessions Chrome rejects Target.createTarget for a new tab
    # even though that initial tab is healthy.  Reuse it before asking the
    # browser to create another target, so startup does not fail before any
    # Instagram request has been made.
    page = None
    try:
        existing_pages = list(getattr(context, "pages", []) or [])
        for candidate in existing_pages:
            is_closed = getattr(candidate, "is_closed", None)
            if not callable(is_closed) or not is_closed():
                page = candidate
                break
    except Exception:
        page = None
    if page is None:
        try:
            page = await context.new_page()
        except Exception:
            # A Chromium process can create its initial target slightly after
            # launch. Check once more before reporting a startup failure.
            try:
                existing_pages = list(getattr(context, "pages", []) or [])
                page = next(
                    (
                        candidate
                        for candidate in existing_pages
                        if not callable(getattr(candidate, "is_closed", None))
                        or not candidate.is_closed()
                    ),
                    None,
                )
            except Exception:
                page = None
            if page is None:
                raise
    attach_reel_metadata_collector(page, reel_metadata, rate_limit_state)
    try:
        await navigate_with_retries(page, url, allow_login=allow_login)
        await page.wait_for_timeout(500)
        return page
    except Exception as error:
        if keep_open_on_navigation_failure:
            print(f"Initial Instagram page could not be opened. The browser will stay open for manual login: {error}", file=sys.stderr)
            return page
        await page.close()
        raise


async def recycle_collection_page(
    context: Any,
    page: Any,
    url: str,
    reel_metadata: dict[str, dict[str, Any]],
    rate_limit_state: InstagramRateLimitState | None = None,
) -> Any:
    if page:
        try:
            await page.close()
        except Exception:
            pass
    reel_metadata.clear()
    return await create_collection_page(context, url, reel_metadata, rate_limit_state=rate_limit_state)


def hashtag_candidate_limit(
    max_items: int,
    hashtag_count: int,
    candidates_per_keyword: int = 0,
) -> int:
    """Return zero for exhaustive discovery, otherwise inspect 50 candidates per hashtag."""
    if candidates_per_keyword > 0:
        return int(candidates_per_keyword)
    if max_items == 0:
        return 0
    balanced_target = math.ceil(max(1, max_items) / max(1, hashtag_count))
    return max(50, balanced_target * 4)


async def collect_hashtag_reel_urls(
    page: Any,
    hashtags: list[str],
    max_items: int,
    reel_metadata: dict[str, dict[str, Any]] | None = None,
    should_stop: Callable[[], bool] | None = None,
    candidates_per_keyword: int = 0,
    rate_limit_state: InstagramRateLimitState | None = None,
) -> list[str]:
    groups: list[list[str]] = []
    metadata = reel_metadata if reel_metadata is not None else {}
    per_hashtag_limit = hashtag_candidate_limit(
        max_items,
        len(hashtags),
        candidates_per_keyword,
    )
    for hashtag_index, hashtag in enumerate(hashtags, start=1):
        active_rate_limit_state = rate_limit_state or rate_limit_state_for_page(page)
        if active_rate_limit_state is not None:
            active_rate_limit_state.raise_if_limited()
        if should_stop and should_stop():
            return []
        await page.goto(hashtag_page_url(hashtag), wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(HASHTAG_GRID_INITIAL_LOAD_MILLISECONDS)
        if active_rate_limit_state is not None:
            active_rate_limit_state.raise_if_limited()
        if should_stop and should_stop():
            return []
        if re.search(r"/accounts/login", page.url, re.I):
            raise CrawlerAccessError("login_required", "Instagram login is required for hashtag collection.")
        if re.search(r"/(?:challenge|checkpoint)/", page.url, re.I):
            raise CrawlerAccessError("challenge_required", "Instagram requested an account check during hashtag collection.")
        urls: list[str] = []
        seen: set[str] = set()
        unchanged_attempts = 0
        for _ in range(HASHTAG_GRID_MAX_SCROLL_ATTEMPTS):
            if active_rate_limit_state is not None:
                active_rate_limit_state.raise_if_limited()
            if should_stop and should_stop():
                return []
            if per_hashtag_limit and len(urls) >= per_hashtag_limit:
                break
            cards = await page.eval_on_selector_all(
                'a[href*="/reel/"], a[href*="/reels/"], a[href*="/p/"]',
                r"""elements => {
                  const visible = element => {
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.left < innerWidth
                      && rect.bottom > 0 && rect.top < innerHeight;
                  };
                  const isGridCard = element => {
                    const rect = element.getBoundingClientRect();
                    return Boolean(element.closest('main'))
                      && !element.closest('header, nav, footer, [role="navigation"]')
                      && rect.width >= 20 && rect.height >= 20;
                  };
                  const isReel = element => {
                    const href = element.href || '';
                    if (/\/reels?\//i.test(href)) return true;
                    const labels = [
                      element.getAttribute('aria-label') || '',
                      element.getAttribute('title') || '',
                      ...[...element.querySelectorAll('[aria-label], [title]')].map(child =>
                        `${child.getAttribute('aria-label') || ''} ${child.getAttribute('title') || ''}`
                      )
                    ].join(' ');
                    return Boolean(element.querySelector('video')) || /릴스|\breels?\b/i.test(labels);
                  };
                  return elements.filter(isReel).map(element => {
                    const rect = element.getBoundingClientRect();
                    return {
                      href: element.href,
                      visible: visible(element),
                      gridCard: isGridCard(element),
                      bounds: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height },
                      viewportWidth: innerWidth,
                      viewportHeight: innerHeight,
                    };
                  });
                }""",
            )
            before = len(urls)
            for url in visible_search_grid_reel_urls(cards):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
                    if per_hashtag_limit and len(urls) >= per_hashtag_limit:
                        break
            await collect_embedded_reel_metadata(page, metadata)
            if should_stop and should_stop():
                return []
            unchanged_attempts = unchanged_attempts + 1 if len(urls) == before else 0
            if unchanged_attempts >= HASHTAG_GRID_MAX_UNCHANGED_ATTEMPTS or (per_hashtag_limit and len(urls) >= per_hashtag_limit):
                break
            await page.mouse.wheel(0, 1_200)
            await page.wait_for_timeout(HASHTAG_GRID_SCROLL_SETTLE_MILLISECONDS)
            if active_rate_limit_state is not None:
                active_rate_limit_state.raise_if_limited()
        print(f"[Hashtag {hashtag_index}/{len(hashtags)}] #{hashtag} -> 릴스 후보 {len(urls)}개")
        groups.append(urls)
    combined: list[str] = []
    combined_seen: set[str] = set()
    # Keep every discovered candidate from each keyword.  This preserves the
    # requested 50-candidate pool per keyword instead of truncating the mixed
    # list back to a much smaller global cap.
    maximum_candidates = per_hashtag_limit * len(hashtags) if max_items else None
    index = 0
    while maximum_candidates is None or len(combined) < maximum_candidates:
        found = False
        for group in groups:
            if index >= len(group):
                continue
            found = True
            url = group[index]
            if url not in combined_seen:
                combined_seen.add(url)
                combined.append(url)
                if maximum_candidates is not None and len(combined) >= maximum_candidates:
                    break
        if not found:
            break
        index += 1
    if not combined:
        raise CrawlerAccessError(
            "hashtag_reels_not_found",
            "해시태그 검색 결과에서 릴스 URL을 찾지 못했습니다. 검색 결과 화면을 릴스 피드로 처리하지 않고 수집을 중단합니다.",
        )
    return combined


def unattempted_hashtag_urls(urls: Iterable[str], attempted_urls: set[str]) -> list[str]:
    return [url for url in urls if url not in attempted_urls]


def request_stop_threadsafe(
    loop: asyncio.AbstractEventLoop,
    request_stop: Callable[[str], bool],
    source: str,
) -> None:
    loop.call_soon_threadsafe(request_stop, source)


async def wait_for_stop_or_timeout(stop_event: asyncio.Event, timeout_seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout_seconds)
        return True
    except asyncio.TimeoutError:
        return False


def prefilter_hashtag_reel_urls(
    urls: list[str],
    metadata: dict[str, dict[str, Any]],
    existing_rows: list[dict[str, Any]],
    max_upload_age_days: float,
    collected_at: str | None = None,
    required_hashtags: list[str] | None = None,
) -> dict[str, Any]:
    """Remove known-expired/cooldown candidates before opening Reel pages."""
    timestamp = collected_at or isoformat_utc()
    del required_hashtags
    existing_by_url: dict[str, list[dict[str, Any]]] = {}
    for row in existing_rows:
        url = str(row.get("url", ""))
        if url:
            existing_by_url.setdefault(url, []).append(row)

    kept: list[tuple[str, datetime | None, int]] = []
    cooldown_skipped = upload_age_skipped = age_known = age_unknown = 0
    for index, url in enumerate(urls):
        matching = existing_by_url.get(url, [])
        if matching and long_collected_record_cooldown(matching, {"url": url, "collected_at": timestamp}):
            cooldown_skipped += 1
            continue

        uploaded_at = ""
        if matching:
            latest = max(
                matching,
                key=lambda row: parse_datetime(row.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc),
            )
            uploaded_at = str(latest.get("uploaded_at", "") or "")
        uploaded = parse_datetime(uploaded_at)
        if uploaded:
            age_known += 1
            if max_upload_age_days > 0 and not is_within_upload_age_days(
                {"uploaded_at": uploaded_at, "collected_at": timestamp},
                max_upload_age_days,
            ):
                upload_age_skipped += 1
                continue
        else:
            age_unknown += 1
        kept.append((url, uploaded, index))

    kept.sort(
        key=lambda item: (
            item[1] is not None,
            item[1].timestamp() if item[1] is not None else float("-inf"),
            -item[2],
        ),
        reverse=True,
    )
    return {
        "urls": [url for url, _uploaded, _index in kept],
        "total": len(urls),
        "cooldownSkipped": cooldown_skipped,
        "uploadAgeSkipped": upload_age_skipped,
        "ageKnown": age_known,
        "ageUnknown": age_unknown,
    }


async def expand_visible_caption(page: Any) -> bool:
    before_url = page.url
    expanded = await page.evaluate(
        """patterns => {
          const more = new RegExp(patterns.more, 'i');
          const profile = new RegExp(patterns.profile, 'i');
          const optionMenu = new RegExp(patterns.optionMenu, 'i');
          const visible = element => {
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight;
          };
          const controlText = element => [
            element?.innerText || element?.textContent || '',
            element?.getAttribute?.('aria-label') || '', element?.getAttribute?.('title') || '',
            element?.getAttribute?.('aria-haspopup') || '',
            element?.querySelector?.('[aria-label]')?.getAttribute('aria-label') || '',
            element?.querySelector?.('title')?.textContent || ''
          ].join(' ').replace(/\\s+/g, ' ').trim();
          const isMenuTrigger = element => {
            const expanded = element?.getAttribute?.('aria-expanded');
            return element?.getAttribute?.('aria-haspopup') === 'menu'
              || expanded === 'true'
              || optionMenu.test(controlText(element));
          };
          const videos = [...document.querySelectorAll('video')].filter(visible);
          const video = videos.sort((a, b) => {
            const distance = element => {
              const rect = element.getBoundingClientRect();
              return Math.hypot(rect.left + rect.width / 2 - innerWidth / 2, rect.top + rect.height / 2 - innerHeight / 2);
            };
            return distance(a) - distance(b);
          })[0];
          const videoRect = video?.getBoundingClientRect();
          const candidates = [...document.querySelectorAll('button, [role="button"], span')]
            .filter(visible)
            .map(element => {
              const clickable = element.closest('button, [role="button"]') || element;
              const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ');
              const all = `${text} ${clickable.innerText || ''} ${clickable.getAttribute?.('aria-label') || ''} ${clickable.getAttribute?.('title') || ''}`;
              return { element, clickable, text, all };
            })
            .filter(item => more.test(item.text) && !profile.test(item.all) && !isMenuTrigger(item.clickable)
              && !item.clickable.querySelector?.('svg') && !item.clickable.closest('a[href]'))
            .filter(item => {
              if (!videoRect) return true;
              const rect = item.element.getBoundingClientRect();
              const centerY = rect.top + rect.height / 2;
              return centerY >= videoRect.top + videoRect.height * .35 && centerY <= videoRect.bottom + 120;
            });
          if (!candidates.length) return false;
          candidates[0].clickable.click();
          return true;
        }""",
        {
            "more": r"^(?:(?:…|\.\.\.)\s*)?(?:더\s*보기|more)$",
            "profile": PROFILE_INFO_TEXT_PATTERN.pattern,
            "optionMenu": r"(?:옵션|options?|more\s+options?|menu|메뉴|report|신고|about\s+this\s+account|계정\s*정보)",
        },
    )
    if expanded:
        await page.wait_for_timeout(400)
        if is_instagram_reels_surface(before_url) and not is_instagram_reels_surface(page.url):
            print("캡션 확장 중 릴스를 벗어나 원래 화면으로 복귀합니다.", file=sys.stderr)
            await page.goto(before_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(500)
            return False
    return bool(expanded)


EXTRACT_VISIBLE_REEL_SCRIPT = r"""() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.left < innerWidth && rect.bottom > 0 && rect.top < innerHeight;
  };
  const textOf = element => (element?.innerText || element?.textContent || '').trim();
  const centerDistance = (element, reference = null) => {
    const rect = element.getBoundingClientRect();
    const x = reference ? reference.left + reference.width / 2 : innerWidth / 2;
    const y = reference ? reference.top + reference.height / 2 : innerHeight / 2;
    return Math.hypot(rect.left + rect.width / 2 - x, rect.top + rect.height / 2 - y);
  };
  const video = [...document.querySelectorAll('video')].filter(visible)
    .sort((a, b) => centerDistance(a) - centerDistance(b))[0] || null;
  const videoRect = video?.getBoundingClientRect() || null;
  const videoDurationSeconds = Number.isFinite(video?.duration) && video.duration >= 0 ? video.duration : null;
  const reelLinks = [...document.querySelectorAll('a[href*="/reel/"], a[href*="/reels/"]')]
    .filter(visible).sort((a, b) => centerDistance(a, videoRect) - centerDistance(b, videoRect));
  const currentMatch = location.pathname.match(/^\/reels?\/([A-Za-z0-9_-]+)\/?/i);
  const activeLink = reelLinks.find(link => currentMatch && link.getAttribute('href')?.includes(`/${currentMatch[1]}/`)) || reelLinks[0] || null;
  const controls = [...document.querySelectorAll('button, [role="button"], svg[aria-label], [title]')].filter(visible);
  const labelOf = element => [
    element.getAttribute?.('aria-label') || '', element.getAttribute?.('title') || '',
    element.querySelector?.('[aria-label]')?.getAttribute('aria-label') || '',
    element.querySelector?.('title')?.textContent || ''
  ].join(' ').toLowerCase();
  const findControls = terms => controls.filter(element => terms.some(term => labelOf(element).includes(term)))
    .sort((a, b) => centerDistance(a, videoRect) - centerDistance(b, videoRect));
  const likeControls = findControls(['좋아요', 'like', 'unlike']);
  const commentControls = findControls(['댓글', 'comment']);
  const repostControls = findControls(['리포스트', 'repost']);
  const viewControls = findControls(['조회수', 'views', 'plays']);
  let scope = activeLink?.closest('article') || video?.closest('article') || video?.parentElement?.parentElement || document.querySelector('main') || document.body;
  for (let depth = 0; depth < 3 && scope.parentElement; depth++) {
    const rect = scope.parentElement.getBoundingClientRect();
    if (rect.height > innerHeight * 1.7 || rect.width > innerWidth * .92) break;
    scope = scope.parentElement;
  }
  const metricToken = value => {
    const matches = String(value || '').match(/\d+(?:[.,]\d+)*(?:\s*(?:천|만|억|[KMB]))?/gi) || [];
    return matches.sort((a, b) => b.length - a.length)[0]?.replace(/\s+/g, '') || '';
  };
  const exactMetricToken = value => {
    const text = String(value || '').replace(/\u00a0/g, ' ');
    const matches = [...text.matchAll(/(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?![\d.,\s]*(?:천|만|억|[KMB]))/gi)]
      .map(match => match[0]);
    return matches.sort((a, b) => b.length - a.length)[0] || '';
  };
  const metricSources = element => [
    element?.getAttribute?.('aria-label') || '', element?.getAttribute?.('title') || '',
    element?.querySelector?.('[aria-label]')?.getAttribute('aria-label') || '',
    element?.querySelector?.('title')?.textContent || '', textOf(element),
  ];
  const metricFrom = candidates => {
    let compactToken = '';
    for (const control of candidates) {
      let node = control;
      for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
        for (const source of metricSources(node)) {
          const exact = exactMetricToken(source);
          if (exact) return exact;
          const token = metricToken(source);
          if (!compactToken && token) compactToken = token;
        }
      }
    }
    return compactToken;
  };
  const viewLine = textOf(document.querySelector('main')).split(/\n+/).find(line =>
    /(?:조회수|views?|plays?)/i.test(line) && metricToken(line)
  ) || '';
  const skip = new Set(['about', 'accounts', 'direct', 'explore', 'reel', 'reels', 'stories']);
  const profileLinks = root => [...root.querySelectorAll('a[href^="/"]')].filter(visible).map(element => ({
    element, parts: (element.getAttribute('href') || '').split('/').filter(Boolean)
  })).filter(item => item.parts.length === 1 && !skip.has(item.parts[0].toLowerCase()))
    .sort((a, b) => centerDistance(a.element, videoRect) - centerDistance(b.element, videoRect));
  // On the desktop Reel layout, the author block can be a sibling of the
  // video article rather than its child. Limit the fallback to ``main`` so
  // the logged-in account link in Instagram's left sidebar is never selected.
  const profiles = profileLinks(scope);
  if (!profiles.length) profiles.push(...profileLinks(document.querySelector('main') || scope));
  const username = profiles[0]?.parts[0] || '';
  const audioAnchor = [...scope.querySelectorAll('a[href*="/audio/"]')].filter(visible)
    .sort((a, b) => centerDistance(a, videoRect) - centerDistance(b, videoRect))[0];
  const lines = textOf(scope).split(/\n+/).map(line => line.trim()).filter(Boolean);
  const audioLine = lines.find(line => /(?:♬|🎵|원본\s*오디오|original\s*audio)/i.test(line)) || '';
  const audioName = (textOf(audioAnchor) || audioLine).replace(/^[\s♬🎵♫·•-]+/, '').trim();
  const locationAnchor = [...scope.querySelectorAll('a[href*="/explore/locations/"]')].filter(visible)[0];
  const locationName = textOf(locationAnchor).replace(/\s+/g, ' ').trim();
  const adPattern = /^(?:광고|후원됨|sponsored|paid\s+partnership(?:\s+with\s+.+)?)$/i;
  const ad = [...scope.querySelectorAll('span, a, button, [role="button"]')].filter(visible).some(element =>
    [textOf(element), element.getAttribute?.('aria-label') || '', element.getAttribute?.('title') || '']
      .some(value => adPattern.test(value.trim().replace(/\s+/g, ' '))))
    || Boolean(scope.querySelector('[data-ad-id], [data-ad-preview], a[href*="/ads/"]'));
  const timeCandidates = [...scope.querySelectorAll('time'), ...document.querySelectorAll('main time')]
    .filter((element, index, all) => visible(element) && all.indexOf(element) === index)
    .sort((a, b) => centerDistance(a, videoRect) - centerDistance(b, videoRect));
  const timeElement = timeCandidates[0] || null;
  const uploadedAt = timeElement?.getAttribute('datetime') || timeElement?.getAttribute('title') || textOf(timeElement);
  const uploadedText = textOf(timeElement);
  const excluded = /^(?:팔로우|follow|좋아요|likes?|댓글|comments?|리포스트|reposts?|공유|share|send|저장|save|옵션|options?)$/i;
  const clean = value => value.split(/\n+/).map(line => line.trim().replace(/(?:…|\.\.\.)?\s*(?:더\s*보기|more)$/i, '').trim())
    .filter(Boolean).filter(line => line !== username && line !== audioName && line !== uploadedText && line !== locationName)
    .filter(line => !(username && line.startsWith(username) && /(?:팔로우|follow)/i.test(line)))
    .filter(line => !(audioName && line.includes(audioName))).filter(line => !/(?:♬|🎵|원본\s*오디오|original\s*audio)/i.test(line))
    .filter(line => !excluded.test(line) && !adPattern.test(line) && !/^\d+(?:[.,]\d+)*(?:천|만|억|[KMB])?$/i.test(line))
    .join(' ').replace(/\s+/g, ' ').trim();
  const captions = [...scope.querySelectorAll('span')].filter(visible).map(element => ({ element, text: clean(textOf(element)) }))
    .filter(item => item.text && item.text.length <= 10000)
    .sort((a, b) => b.text.length + (b.text.includes('#') ? 2000 : 0) - a.text.length - (a.text.includes('#') ? 2000 : 0));
  return {
    currentUrl: location.href, activeHref: activeLink?.href || '', username, title: captions[0]?.text || '',
    hashtagTexts: [...scope.querySelectorAll('a[href*="/explore/tags/"]')].filter(visible).map(textOf).filter(text => text.startsWith('#')),
    audioName, locationName, ad, uploadedAt, videoDurationSeconds,
    viewText: metricFrom(viewControls) || exactMetricToken(viewLine) || metricToken(viewLine),
    likeText: metricFrom(likeControls), likeControlPresent: likeControls.length > 0,
    commentText: metricFrom(commentControls), repostText: metricFrom(repostControls)
  };
}"""


async def extract_visible_reel(page: Any) -> dict[str, Any] | None:
    browser_data = await page.evaluate(EXTRACT_VISIBLE_REEL_SCRIPT)
    normalized = normalize_reel_url(browser_data.get("currentUrl")) or normalize_reel_url(browser_data.get("activeHref"))
    return {**browser_data, **normalized} if normalized else None


def exact_visible_reel_count(value: Any) -> int | None:
    """Accept only a fully rendered integer, never a compact metric label."""
    text = str(value or "").replace("\u00a0", " ").strip()
    if not re.fullmatch(r"\d+|\d{1,3}(?:,\d{3})+", text):
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def metadata_from_visible_reel(record: dict[str, Any] | None) -> dict[str, Any]:
    """Use exact count labels already rendered for the active Reel as page data."""
    visible = record or {}
    view_count = exact_visible_reel_count(visible.get("viewText"))
    return {
        "userId": "",
        "username": str(visible.get("username") or "").strip().lstrip("@"),
        "caption": "",
        "audioName": "",
        "locationName": "",
        "ad": False,
        "uploadedAt": "",
        "videoDurationSeconds": exact_nonnegative_number(visible.get("videoDurationSeconds")),
        "viewCount": view_count,
        "viewSourceField": "visible_dom" if view_count is not None else None,
        "followerCount": None,
        "followerSourceField": None,
        "likeCount": exact_visible_reel_count(visible.get("likeText")),
        "commentCount": exact_visible_reel_count(visible.get("commentText")),
        "repostCount": exact_visible_reel_count(visible.get("repostText")),
        "isReel": True,
    }


async def read_active_reel_identity(page: Any) -> dict[str, str] | None:
    browser_data = await page.evaluate(
        """() => {
          const visible = element => { const r = element.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight; };
          const activeHref = [...document.querySelectorAll('a[href*="/reel/"], a[href*="/reels/"]')]
            .filter(visible).map(element => { const r = element.getBoundingClientRect(); return { href: element.href, distance: Math.abs(r.top + r.height / 2 - innerHeight / 2) }; })
            .sort((a, b) => a.distance - b.distance)[0]?.href || '';
          return { currentUrl: location.href, activeHref };
        }"""
    )
    return normalize_reel_url(browser_data.get("activeHref")) or normalize_reel_url(browser_data.get("currentUrl"))


async def wait_for_active_reel_change(page: Any, previous_shortcode: str, timeout_milliseconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_milliseconds / 1000
    latest: dict[str, str] | None = None
    while time.monotonic() < deadline:
        try:
            latest = await read_active_reel_identity(page)
        except Exception:
            latest = None
        if latest and latest.get("shortcode") and latest["shortcode"] != previous_shortcode:
            await page.wait_for_timeout(REEL_TRANSITION_SETTLE_MILLISECONDS)
            return {"changed": True, **latest}
        await page.wait_for_timeout(REEL_TRANSITION_POLL_MILLISECONDS)
    return {"changed": False, **(latest or {})}


async def advance_to_next_reel(page: Any, previous_shortcode: str, timeout_milliseconds: float) -> dict[str, Any]:
    previous = previous_shortcode
    if not previous:
        identity = await read_active_reel_identity(page)
        previous = identity.get("shortcode", "") if identity else ""
    # A caption/menu control can retain focus after a Reel is inspected.  Escape
    # closes that popover, and wheel scrolling is delivered to the Reel feed rather
    # than moving the selection inside Instagram's options menu.
    await page.keyboard.press("Escape")
    await page.mouse.wheel(0, 900)
    transition = await wait_for_active_reel_change(page, previous, timeout_milliseconds)
    if transition["changed"]:
        return transition
    await page.keyboard.press("ArrowDown")
    return await wait_for_active_reel_change(page, previous, max(500, timeout_milliseconds / 2))


def next_transition_stall_state(previous_count: Any, changed: bool) -> dict[str, Any]:
    consecutive = 0 if changed else max(0, int(previous_count or 0)) + 1
    return {"consecutive": consecutive, "shouldRecycle": consecutive >= REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD}


async def wait_for_reel_metadata(shortcode: str, reel_metadata: dict[str, dict[str, Any]], timeout_milliseconds: int = PASSIVE_RESPONSE_METADATA_TIMEOUT_MILLISECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_milliseconds / 1000
    while time.monotonic() < deadline:
        if shortcode in reel_metadata:
            return reel_metadata[shortcode]
        await asyncio.sleep(0.1)
    return reel_metadata.get(shortcode, {})


def has_complete_page_reel_metrics(shortcode: str, metadata: dict[str, Any] | None) -> bool:
    """Return whether page data contains every exact metric required by this collector."""
    return bool(
        has_exact_engagement_metadata(metadata)
        and exact_view_counts_from_metadata(shortcode, metadata)
        and exact_follower_result_from_metadata(metadata) is not None
    )


async def wait_for_complete_page_reel_metadata(
    shortcode: str,
    reel_metadata: dict[str, dict[str, Any]],
    timeout_milliseconds: int = PASSIVE_RESPONSE_METADATA_TIMEOUT_MILLISECONDS,
) -> dict[str, Any]:
    """Wait only for responses the current page has already initiated itself."""
    deadline = time.monotonic() + timeout_milliseconds / 1_000
    latest = reel_metadata.get(shortcode, {})
    while time.monotonic() < deadline:
        latest = reel_metadata.get(shortcode, latest)
        if has_complete_page_reel_metrics(shortcode, latest):
            return latest
        await asyncio.sleep(0.1)
    return reel_metadata.get(shortcode, latest)


async def open_reel_from_creator_profile(page: Any, username: str, shortcode: str) -> dict[str, Any]:
    """Inspect a target profile-grid card and open it only when its view label is compact.

    Navigating directly to ``/reel/<shortcode>/`` is redirected by Instagram
    to the plural route in some sessions. If the grid card itself renders a
    full integer (for example ``6591``), that DOM value is already exact. A
    compact label (such as ``1.5만``) instead requires clicking the card so the
    normal UI response exposes its raw ``play_count``.
    """
    result: dict[str, Any] = {"found": False, "opened": False, "viewText": ""}
    normalized_username = str(username or "").strip().lstrip("@")
    target = str(shortcode or "").strip()
    if not (
        INSTAGRAM_USERNAME_PATTERN.fullmatch(normalized_username)
        and re.fullmatch(r"[A-Za-z0-9_-]+", target)
    ):
        return result
    await navigate_with_retries(
        page,
        f"https://www.instagram.com/{quote(normalized_username, safe='')}/reels/",
    )
    await page.wait_for_timeout(PROFILE_REEL_VIEW_SETTLE_MILLISECONDS)
    try:
        attempts = max(1, int(PROFILE_REEL_VIEW_SCROLL_ATTEMPTS))
        for attempt in range(attempts):
            card = await page.evaluate(
                r"""shortcode => {
              const visible = element => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const target = [...document.querySelectorAll('a[href]')].find(anchor => {
                try {
                  const path = new URL(anchor.href, location.href).pathname;
                  // Profile grids use /<username>/reel/<shortcode>/ rather
                  // than the standalone /reel/<shortcode>/ permalink.
                  return new RegExp(`^/(?:[A-Za-z0-9._]{1,30}/)?(?:reels?|p)/${shortcode}/?$`, 'i').test(path) && visible(anchor);
                } catch (_) {
                  return false;
                }
              });
              if (!target) return {found: false, viewText: '', href: ''};
              target.scrollIntoView({block: 'center', inline: 'nearest'});
              const text = [
                target.getAttribute('aria-label') || '', target.getAttribute('title') || '',
                target.innerText || '', target.textContent || ''
              ].join(' ').replace(/\u00a0/g, ' ');
              const exact = [...text.matchAll(/(?<![\d.,/])(?:\d{1,3}(?:,\d{3})+|\d+)(?![\d.,/\s]*(?:천|만|억|[KMB]))/gi)]
                .map(match => match[0]).sort((a, b) => b.length - a.length)[0] || '';
              return {found: true, viewText: exact, href: target.href};
            }""",
                target,
            )
            if not isinstance(card, dict):
                card = result
            if card.get("found"):
                result = {**result, **card}
            if result.get("found") and exact_visible_reel_count(result.get("viewText")) is not None:
                return result
            if result.get("found") and result.get("href"):
                # Scheduling the DOM click after this evaluation returns avoids
                # treating Instagram's immediate navigation as an execution-
                # context error. The click remains a normal UI action, not a
                # direct media-info request.
                scheduled = await page.evaluate(
                    r"""href => {
                      const target = [...document.querySelectorAll('a[href]')]
                        .find(anchor => anchor.href === href);
                      if (!target) return false;
                      setTimeout(() => target.click(), 0);
                      return true;
                    }""",
                    str(result["href"]),
                )
                if scheduled:
                    result["opened"] = True
                else:
                    result["href"] = ""
            if result.get("opened"):
                await page.wait_for_timeout(PROFILE_REEL_VIEW_SETTLE_MILLISECONDS)
                return result
            if attempt + 1 < attempts:
                await page.mouse.wheel(0, 1_500)
                await page.wait_for_timeout(PROFILE_REEL_VIEW_SCROLL_MILLISECONDS)
    except Exception:
        return result
    return result


async def resolve_page_first_reel_metadata(
    page: Any,
    record: dict[str, Any],
    passive_response_metadata: dict[str, dict[str, Any]],
    timeout_milliseconds: int = PASSIVE_RESPONSE_METADATA_TIMEOUT_MILLISECONDS,
    *,
    require_complete_metrics: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Prefer active-page embedded/DOM data before using passive responses.

    After 20 seconds without a complete response, open the Reel through its
    creator's profile grid and retry passive observation. This uses normal
    browser navigation and clicks only; it never calls the direct media-info
    API.  Hybrid collection sets ``require_complete_metrics`` to false because
    the Android app owns the engagement metrics; it therefore waits briefly
    only for browser identity/static metadata and never enters the profile
    Reel grid just to obtain a web play count.
    """
    shortcode = str(record.get("shortcode") or "")
    embedded_metadata: dict[str, dict[str, Any]] = {}
    await collect_embedded_reel_metadata(page, embedded_metadata, include_follower_count=True)
    page_metadata = merge_reel_metadata(
        embedded_metadata.get(shortcode),
        metadata_from_visible_reel(record),
    )
    if not require_complete_metrics:
        # Passive metadata may contain the creator id, upload time, or full
        # caption a moment after the visible Reel appears.  Do not wait for
        # engagement metrics: Android is intentionally the source for those
        # fields in this pipeline.
        passive_metadata = await wait_for_reel_metadata(
            shortcode,
            passive_response_metadata,
            min(max(0, timeout_milliseconds), 750),
        )
        return merge_reel_metadata(page_metadata, passive_metadata), bool(passive_metadata)
    if has_complete_page_reel_metrics(shortcode, page_metadata):
        return page_metadata, False
    passive_metadata = await wait_for_complete_page_reel_metadata(
        shortcode,
        passive_response_metadata,
        timeout_milliseconds,
    )
    resolved_metadata = merge_reel_metadata(page_metadata, passive_metadata)
    if has_complete_page_reel_metrics(shortcode, resolved_metadata) or timeout_milliseconds <= 0:
        return resolved_metadata, True

    # A target Reel's JSON owner is authoritative. The rendered author link
    # is used only when that response has not arrived yet; never fall back to
    # a generic document/sidebar profile link.
    username = str(resolved_metadata.get("username") or "").strip().lstrip("@")
    if not INSTAGRAM_USERNAME_PATTERN.fullmatch(username):
        try:
            refreshed_record = await extract_visible_reel(page)
            username = str((refreshed_record or {}).get("username") or "").strip().lstrip("@")
        except Exception:
            username = ""
    if not INSTAGRAM_USERNAME_PATTERN.fullmatch(username):
        username = str(record.get("username") or "").strip().lstrip("@")
    print(
        "필수 지표 응답이 "
        f"{timeout_milliseconds / 1_000:g}초 내 도착하지 않아 작성자 프로필의 릴스 탭에서 해당 영상을 연 뒤 "
        f"{PASSIVE_RESPONSE_EXTENDED_WAIT_MILLISECONDS / 1_000:g}초 더 기다립니다: {shortcode}"
    )
    if INSTAGRAM_USERNAME_PATTERN.fullmatch(username):
        print(f"작성자 프로필 릴스 탭으로 이동: https://www.instagram.com/{username}/reels/")
    else:
        print(f"작성자 username을 찾지 못해 프로필 릴스 탭으로 이동할 수 없습니다: {shortcode}")
    profile_card = await open_reel_from_creator_profile(page, username, shortcode)
    profile_view_count = exact_visible_reel_count(profile_card.get("viewText"))
    profile_metadata = {
        "viewCount": profile_view_count,
        "viewSourceField": "profile_grid_dom" if profile_view_count is not None else None,
    }
    if profile_view_count is not None:
        print(f"프로필 릴스 카드의 정확 조회수를 사용합니다: {shortcode} = {profile_view_count}")
    elif not profile_card.get("opened"):
        print(f"작성자 프로필에서 대상 릴 카드를 찾지 못해 현재 페이지 응답을 계속 관찰합니다: {shortcode}")
    detail_embedded_metadata: dict[str, dict[str, Any]] = {}
    await collect_embedded_reel_metadata(page, detail_embedded_metadata, include_follower_count=True)
    detail_page_metadata = merge_reel_metadata(
        merge_reel_metadata(resolved_metadata, profile_metadata),
        detail_embedded_metadata.get(shortcode),
    )
    if has_complete_page_reel_metrics(shortcode, detail_page_metadata):
        return detail_page_metadata, True
    detail_passive_metadata = await wait_for_complete_page_reel_metadata(
        shortcode,
        passive_response_metadata,
        PASSIVE_RESPONSE_EXTENDED_WAIT_MILLISECONDS,
    )
    return merge_reel_metadata(detail_page_metadata, detail_passive_metadata), True


async def request_initial_reel_info_metadata(
    page: Any,
    shortcode: str,
    diagnostic: dict[str, str] | None = None,
    *,
    settle_milliseconds: int = 0,
) -> dict[str, Any]:
    """Wait for the initial Reel detail page to settle before its direct API lookup."""
    await page.wait_for_timeout(max(0, int(settle_milliseconds)))
    return await request_reel_info_metadata(page, shortcode, diagnostic)


async def request_reel_info_metadata(
    page: Any,
    shortcode: str,
    diagnostic: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not DIRECT_REEL_INFO_REQUESTS_ENABLED:
        _set_direct_reel_info_diagnostic(
            diagnostic,
            "disabled",
            "Direct Reel-info requests are disabled; use passive page responses instead.",
        )
        return {}
    target = str(shortcode or "").strip()
    media_id = shortcode_to_media_id(target)
    if not media_id:
        _set_direct_reel_info_diagnostic(
            diagnostic,
            "invalid_shortcode",
            "Direct Reel info could not derive a media ID from the shortcode.",
        )
        return {}
    try:
        endpoint = await asyncio.wait_for(
            page.evaluate(
                DIRECT_REEL_INFO_REQUEST_SCRIPT.replace(
                    "__FETCH_TIMEOUT_MILLISECONDS__",
                    str(DIRECT_REEL_INFO_FETCH_TIMEOUT_MILLISECONDS),
                ),
                media_id,
            ),
            timeout=DIRECT_REEL_INFO_TIMEOUT_SECONDS,
        )
        endpoint_status = int(endpoint.get("status") or 0)
        if endpoint_status != 200:
            _set_direct_reel_info_diagnostic(diagnostic, "http_error", f"Direct Reel info returned HTTP {endpoint_status}.")
            return {}
        media = endpoint.get("media") if isinstance(endpoint.get("media"), dict) else None
        if media is None and "text" in endpoint:
            payload = _parse_direct_reel_info_payload(endpoint.get("text"))
            items = payload.get("items") if payload else None
            media = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
        if media is None:
            content_type = str(endpoint.get("contentType") or "").casefold()
            status = "html_response" if "text/html" in content_type else "invalid_json"
            _set_direct_reel_info_diagnostic(
                diagnostic,
                status,
                _direct_reel_info_invalid_json_error(endpoint),
            )
            return {}
        observed_shortcode = str(media.get("code") or media.get("shortcode") or "").strip()
        observed_media_id = str(media.get("pk") or media.get("id") or "").strip()
        if (observed_shortcode and observed_shortcode != target) or (
            not observed_shortcode and observed_media_id != media_id
        ):
            _set_direct_reel_info_diagnostic(diagnostic, "identity_mismatch", "Direct Reel info did not identify the requested Reel.")
            return {}
        _set_direct_reel_info_diagnostic(diagnostic, "success")
        return metadata_from_media(media, include_follower_count=True) if media else {}
    except asyncio.TimeoutError:
        _set_direct_reel_info_diagnostic(diagnostic, "timeout", f"Direct Reel info timed out after {DIRECT_REEL_INFO_TIMEOUT_SECONDS:g} seconds.")
        return {}
    except Exception as error:
        message = str(error).strip()
        if "abort" in message.casefold():
            _set_direct_reel_info_diagnostic(
                diagnostic,
                "timeout",
                f"Direct Reel info was aborted after {DIRECT_REEL_INFO_FETCH_TIMEOUT_MILLISECONDS / 1_000:g} seconds.",
            )
        else:
            _set_direct_reel_info_diagnostic(
                diagnostic,
                "request_error",
                f"Direct Reel info request failed: {message[:300] or type(error).__name__}",
            )
        return {}


async def request_reel_info_metadata_from_reel_page(
    page: Any,
    shortcode: str,
    diagnostic: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Retry the direct media endpoint from a freshly navigated Reel page.

    Instagram occasionally returns a non-JSON body from the media-info
    endpoint even though it reports HTTP 200.  Reopening the target Reel in
    the dedicated background page refreshes the page session before the retry,
    without disturbing the collector's active discovery tab.
    """
    target = str(shortcode or "").strip()
    if not shortcode_to_media_id(target):
        _set_direct_reel_info_diagnostic(
            diagnostic,
            "invalid_shortcode",
            "Direct Reel info could not validate the shortcode for a fresh-page retry.",
        )
        return {}
    try:
        await navigate_with_retries(
            page,
            f"https://www.instagram.com/reels/{quote(target, safe='')}/",
            attempts=2,
        )
        await page.wait_for_timeout(DIRECT_REEL_INFO_FRESH_PAGE_SETTLE_MILLISECONDS)
    except asyncio.CancelledError:
        raise
    except CrawlerAccessError as error:
        if error.code == "rate_limited":
            _set_direct_reel_info_diagnostic(diagnostic, "http_error", "Direct Reel fallback page returned HTTP 429.")
        else:
            _set_direct_reel_info_diagnostic(
                diagnostic,
                "request_error",
                f"Direct Reel fallback page was unavailable: {str(error)[:300]}",
            )
        return {}
    except Exception as error:
        _set_direct_reel_info_diagnostic(
            diagnostic,
            "request_error",
            f"Direct Reel fallback page could not be opened: {str(error)[:300] or type(error).__name__}",
        )
        return {}
    return await request_reel_info_metadata(page, target, diagnostic)


async def read_reel_detail_metadata(
    page: Any,
    shortcode: str,
    existing_metadata: dict[str, Any] | None = None,
    *,
    require_exact_view: bool = False,
) -> dict[str, Any]:
    """Wait for a Reel detail response to fill missing exact metric integers."""
    target = str(shortcode or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", target):
        return dict(existing_metadata or {})
    metadata: dict[str, dict[str, Any]] = {target: dict(existing_metadata or {})}
    response_tasks: set[asyncio.Task[None]] = set()

    def on_response(response: Any) -> None:
        task = asyncio.create_task(_collect_reel_metadata_from_response(response, metadata))
        response_tasks.add(task)
        task.add_done_callback(response_tasks.discard)

    def current_metadata() -> dict[str, Any]:
        return metadata.get(target, {})

    def has_required_metadata() -> bool:
        candidate = current_metadata()
        return has_exact_engagement_metadata(candidate) and (
            not require_exact_view or bool(exact_view_counts_from_metadata(target, candidate))
        )

    listener_attached = False
    try:
        page.on("response", on_response)
        listener_attached = True
        for page_attempt in range(EXACT_REEL_DETAIL_PAGE_ATTEMPTS):
            try:
                response = await page.goto(
                    f"https://www.instagram.com/reels/{quote(target, safe='')}/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if page_attempt + 1 >= EXACT_REEL_DETAIL_PAGE_ATTEMPTS:
                    return current_metadata()
                await _cancel_pending_response_tasks(response_tasks)
                await page.wait_for_timeout(EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS * (page_attempt + 1))
                continue
            if _response_status(response) == 429 or re.search(r"/(?:accounts/login|challenge|checkpoint)/", page.url, re.I):
                return current_metadata()
            await page.wait_for_timeout(EXACT_REEL_DETAIL_SETTLE_MILLISECONDS)
            # The target payload can be hydrated into an application/json
            # script without a separate GraphQL response. This is especially
            # common for older Reels whose play_count is far down the creator
            # profile and therefore expensive or impossible to reach by scroll.
            await collect_embedded_reel_metadata(page, metadata)
            deadline = time.monotonic() + EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS / 1000
            while time.monotonic() < deadline:
                if has_required_metadata():
                    return current_metadata()
                await page.wait_for_timeout(EXACT_REEL_DETAIL_POLL_MILLISECONDS)
            if page_attempt + 1 < EXACT_REEL_DETAIL_PAGE_ATTEMPTS:
                await _cancel_pending_response_tasks(response_tasks)
                await page.wait_for_timeout(EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS * (page_attempt + 1))
        return current_metadata()
    except asyncio.CancelledError:
        raise
    except Exception:
        return current_metadata()
    finally:
        if listener_attached:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
        await _cancel_pending_response_tasks(response_tasks)


async def read_profile_reel_view_counts(page: Any, username: str, shortcodes: set[str]) -> dict[str, int]:
    """Read exact play_count integers from the creator Reels GraphQL response."""
    normalized_username = str(username or "").strip().lstrip("@")
    targets = {str(shortcode).strip() for shortcode in shortcodes if str(shortcode).strip()}
    if not INSTAGRAM_USERNAME_PATTERN.fullmatch(normalized_username) or not targets:
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    response_tasks: set[asyncio.Task[None]] = set()

    def on_response(response: Any) -> None:
        task = asyncio.create_task(_collect_reel_metadata_from_response(response, metadata))
        response_tasks.add(task)
        task.add_done_callback(response_tasks.discard)

    def exact_target_counts() -> dict[str, int]:
        found: dict[str, int] = {}
        for shortcode in targets:
            candidate = metadata.get(shortcode, {})
            count = exact_nonnegative_integer(candidate.get("viewCount"))
            if count is not None and candidate.get("viewSourceField") == "play_count":
                found[shortcode] = count
        return found

    listener_attached = False
    try:
        page.on("response", on_response)
        listener_attached = True
        found: dict[str, int] = {}
        for page_attempt in range(PROFILE_REEL_VIEW_PAGE_ATTEMPTS):
            try:
                response = await page.goto(
                    f"https://www.instagram.com/{quote(normalized_username, safe='')}/reels/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if page_attempt + 1 >= PROFILE_REEL_VIEW_PAGE_ATTEMPTS:
                    return found
                await _cancel_pending_response_tasks(response_tasks)
                await page.wait_for_timeout(PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS)
                continue
            if _response_status(response) == 429 or re.search(r"/(?:accounts/login|challenge|checkpoint)/", page.url, re.I):
                return {}
            await page.wait_for_timeout(PROFILE_REEL_VIEW_SETTLE_MILLISECONDS)
            # Some account pages hydrate their first Reels payload directly
            # into an application/json script instead of emitting a separate
            # GraphQL response.  It contains the same raw play_count value.
            await collect_embedded_reel_metadata(page, metadata)
            for attempt in range(PROFILE_REEL_VIEW_SCROLL_ATTEMPTS):
                found = exact_target_counts()
                if len(found) == len(targets):
                    return found
                if attempt + 1 < PROFILE_REEL_VIEW_SCROLL_ATTEMPTS:
                    await page.mouse.wheel(0, 1_500)
                    await page.wait_for_timeout(PROFILE_REEL_VIEW_SCROLL_MILLISECONDS)
                    await collect_embedded_reel_metadata(page, metadata)
            found = exact_target_counts()
            if len(found) == len(targets):
                return found
            if page_attempt + 1 < PROFILE_REEL_VIEW_PAGE_ATTEMPTS:
                await _cancel_pending_response_tasks(response_tasks)
                await page.wait_for_timeout(PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS)
        return found
    except asyncio.CancelledError:
        raise
    except Exception:
        return {}
    finally:
        if listener_attached:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
        await _cancel_pending_response_tasks(response_tasks)


async def resolve_exact_reel_metrics(
    active_page: Any,
    shortcode: str,
    existing_metadata: dict[str, Any] | None,
    fallback_page_factory: Callable[[], Awaitable[Any]],
    *,
    direct_metadata: dict[str, Any] | None = None,
    direct_diagnostic: dict[str, str] | None = None,
    fallback_username: str = "",
    max_direct_attempts: int = EXACT_METRIC_MAX_ATTEMPTS,
    retry_delay_seconds: float = EXACT_METRIC_RETRY_DELAY_SECONDS,
) -> tuple[dict[str, Any], dict[str, int]]:
    initial_diagnostic = direct_diagnostic if direct_diagnostic is not None else {}
    last_direct_diagnostic = initial_diagnostic
    observed_direct = (
        await request_reel_info_metadata(active_page, shortcode, initial_diagnostic)
        if direct_metadata is None
        else direct_metadata
    )
    metadata = merge_direct_reel_metadata(
        existing_metadata,
        observed_direct,
    )

    fallback_page: Any = None

    async def get_fallback_page() -> Any:
        nonlocal fallback_page
        is_closed = getattr(fallback_page, "is_closed", None)
        if fallback_page is None or (callable(is_closed) and is_closed()):
            fallback_page = await fallback_page_factory()
        return fallback_page

    def sync_direct_diagnostic() -> None:
        _set_direct_reel_info_diagnostic(
            direct_diagnostic,
            str(last_direct_diagnostic.get("status") or ""),
            str(last_direct_diagnostic.get("error") or ""),
        )

    # The first media-info response can arrive before Instagram has exposed
    # every raw metric, or can intermittently be a non-JSON HTTP-200 body.
    # Each retry re-navigates a dedicated background page first; repeated
    # fetches on the active discovery page were prone to repeating
    # that malformed response. Access denials and rate limits are not retried.
    attempts = max(1, min(5, int(max_direct_attempts)))
    retry_delay_milliseconds = max(0, round(float(retry_delay_seconds) * 1_000))
    for attempt in range(1, attempts):
        if is_direct_reel_info_access_denied(last_direct_diagnostic):
            break
        if has_exact_engagement_metadata(metadata) and exact_view_counts_from_metadata(shortcode, metadata):
            break
        retry_page = await get_fallback_page()
        if retry_delay_milliseconds:
            try:
                await retry_page.wait_for_timeout(retry_delay_milliseconds * attempt)
            except asyncio.CancelledError:
                raise
            except Exception:
                fallback_page = None
                continue
        retry_diagnostic: dict[str, str] = {}
        retry_metadata = await request_reel_info_metadata_from_reel_page(
            retry_page,
            shortcode,
            retry_diagnostic,
        )
        last_direct_diagnostic = retry_diagnostic
        metadata = merge_direct_reel_metadata(metadata, retry_metadata)
        # An HTML response from the active discovery page gets one fresh-page
        # retry. If the dedicated Reel page returns HTML too, further calls in
        # the same session are unlikely to help and only increase rate-limit
        # pressure, so continue with response/embedded/profile fallbacks.
        if is_direct_reel_info_access_denied(retry_diagnostic) or is_direct_reel_info_html_response(retry_diagnostic):
            break
    if is_direct_reel_info_access_denied(last_direct_diagnostic):
        sync_direct_diagnostic()
        return metadata, exact_view_counts_from_metadata(shortcode, metadata)

    view_counts = exact_view_counts_from_metadata(shortcode, metadata)
    if not has_exact_engagement_metadata(metadata) or not view_counts:
        metadata = await read_reel_detail_metadata(
            await get_fallback_page(),
            shortcode,
            metadata,
            require_exact_view=not bool(view_counts),
        )
    view_counts = view_counts or exact_view_counts_from_metadata(shortcode, metadata)
    username = str(metadata.get("username") or fallback_username or "").strip().lstrip("@")
    if not view_counts and INSTAGRAM_USERNAME_PATTERN.fullmatch(username):
        view_counts = await read_profile_reel_view_counts(
            await get_fallback_page(),
            username,
            {shortcode},
        )
    sync_direct_diagnostic()
    return metadata, view_counts


async def enrich_missing_profile_reel_view_counts(
    page: Any,
    rows: list[dict[str, Any]],
    target_shortcodes: set[str] | None = None,
) -> int:
    targets_by_username: dict[str, set[str]] = {}
    for row in rows:
        if parse_metric_count(row.get("view_count")) != "":
            continue
        normalized = normalize_reel_url(row.get("url"))
        if target_shortcodes is not None and (not normalized or normalized["shortcode"] not in target_shortcodes):
            continue
        username = str(row.get("username", "")).strip().lstrip("@")
        if normalized and INSTAGRAM_USERNAME_PATTERN.fullmatch(username):
            targets_by_username.setdefault(username, set()).add(normalized["shortcode"])
    changed = 0
    for username, shortcodes in targets_by_username.items():
        found = await read_profile_reel_view_counts(page, username, shortcodes)
        for row in rows:
            normalized = normalize_reel_url(row.get("url"))
            if str(row.get("username", "")).strip().lstrip("@").casefold() == username.casefold() and normalized and normalized["shortcode"] in found:
                row["view_count"] = found[normalized["shortcode"]]
                changed += 1
    return changed


async def request_web_follower_count(page: Any, username: str) -> dict[str, Any]:
    """Use a profile page's own GraphQL responses before falling back to DOM."""
    normalized = str(username or "").strip().lstrip("@")
    if not INSTAGRAM_USERNAME_PATTERN.fullmatch(normalized):
        return {"status": "profile_unavailable", "error": "Invalid Instagram username.", "source": "instagram_web"}
    passive_snapshot: dict[str, Any] = {}
    response_tasks: set[asyncio.Task[None]] = set()
    listener_attached = False

    def on_response(observed: Any) -> None:
        response_tasks.add(asyncio.create_task(
            _collect_profile_snapshot_from_response(observed, normalized, passive_snapshot)
        ))

    try:
        add_listener = getattr(page, "on", None)
        if callable(add_listener):
            add_listener("response", on_response)
            listener_attached = True
        response = await page.goto(f"https://www.instagram.com/{quote(normalized, safe='')}/", wait_until="domcontentloaded", timeout=30_000)
        if _response_status(response) == 429:
            return {"status": "rate_limited", "error": "Instagram returned HTTP 429.", "source": "instagram_web"}
        await page.wait_for_timeout(FOLLOWER_PROFILE_SETTLE_MILLISECONDS)
        if re.search(r"/accounts/login", page.url, re.I):
            return {"status": "login_required", "error": "Instagram login is required.", "source": "instagram_web"}
        if re.search(r"/(?:challenge|checkpoint)/", page.url, re.I):
            return {"status": "challenge_required", "error": "Instagram requested an account check.", "source": "instagram_web"}
        if response_tasks:
            await asyncio.wait(response_tasks, timeout=0.5)
        passive_count = exact_nonnegative_integer(passive_snapshot.get("followerCount"))
        passive_post_count = exact_nonnegative_integer(passive_snapshot.get("postCount"))
        passive_following_count = exact_nonnegative_integer(passive_snapshot.get("followingCount"))
        visible_follower_count = visible_post_count = visible_following_count = None
        visible_category = ""
        if passive_count is None or passive_post_count is None or passive_following_count is None:
            snapshot = await page.evaluate(
                """() => {
                  const profile = document.querySelector('main header') || document.querySelector('header') || document.querySelector('main');
                  const visible = node => {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const profileText = profile?.innerText || '';
                  const profileTexts = profile ? [...new Set([...profile.querySelectorAll('span, a, div')]
                    .filter(visible)
                    .map(node => String(node.innerText || '').trim())
                    .filter(text => text && text.length <= 120))] : [];
                  return {profileText, profileTexts};
                }""",
                normalized,
            )
            if not isinstance(snapshot, dict):
                snapshot = {}
            visible_profile_text = [snapshot.get("profileText", ""), *(snapshot.get("profileTexts") or [])]
            visible_follower_count = exact_visible_profile_follower_count(visible_profile_text)
            visible_post_count = exact_visible_profile_post_count(visible_profile_text)
            visible_following_count = exact_visible_profile_following_count(visible_profile_text)
            visible_category = profile_category_from_visible_text(visible_profile_text)
        follower_count = passive_count if passive_count is not None else visible_follower_count
        post_count = passive_post_count if passive_post_count is not None else visible_post_count
        following_count = passive_following_count if passive_following_count is not None else visible_following_count
        profile_result = {
            "biography": str(passive_snapshot.get("biography") or ""),
            "profile_category": str(passive_snapshot.get("profile_category") or "") or visible_category,
        }
        if post_count is not None:
            profile_result["postCount"] = post_count
        if following_count is not None:
            profile_result["followingCount"] = following_count
        if follower_count is None:
            return {
                "status": "web_unavailable",
                "error": "The rendered profile did not expose an exact follower count.",
                "source": "instagram_web",
                **profile_result,
            }
        source_field = "passive_profile_response.follower_count" if passive_count is not None else "profile_header_text"
        return {**follower_count_success(follower_count, source_field), **profile_result}
    except Exception as error:
        return {"status": "web_error", "error": str(error)[:500], "source": "instagram_web"}
    finally:
        if listener_attached:
            remove_listener = getattr(page, "remove_listener", None)
            if callable(remove_listener):
                try:
                    remove_listener("response", on_response)
                except Exception:
                    pass
        await _cancel_pending_response_tasks(response_tasks)


async def request_anonymous_follower_count(page: Any, username: str) -> dict[str, Any]:
    """Retry only transient anonymous web failures; access denials cannot be fixed by retrying."""
    result: dict[str, Any] = {"status": "web_error", "error": "Follower lookup did not run.", "source": "instagram_web"}
    for attempt in range(ANONYMOUS_FOLLOWER_MAX_ATTEMPTS):
        result = await request_web_follower_count(page, username)
        if result.get("status") != "web_error" or attempt + 1 >= ANONYMOUS_FOLLOWER_MAX_ATTEMPTS:
            return result
        await asyncio.sleep(ANONYMOUS_FOLLOWER_RETRY_SECONDS)
    return result


def follower_lookup_delay_seconds(result: dict[str, Any], fallback_seconds: float) -> float:
    return FOLLOWER_SUCCESS_INTERVAL_SECONDS if result.get("status") == "success" and parse_metric_count(result.get("followerCount")) != "" else fallback_seconds


class SequentialWebFollowerLookup:
    def __init__(
        self,
        page: Any,
        interval_seconds: float = 8,
        *,
        max_attempts: int = FOLLOWER_WEB_MAX_ATTEMPTS,
        retry_delay_seconds: float = FOLLOWER_WEB_RETRY_DELAY_SECONDS,
    ) -> None:
        self.page = page
        self.context = page.context
        self.interval_seconds = interval_seconds
        self.max_attempts = max(1, min(5, int(max_attempts)))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.completed_since_recycle = 0
        self.wait_before_next = 0.0
        self.lock = asyncio.Lock()

    async def _replace_page(self) -> None:
        previous = self.page
        self.page = await self.context.new_page()
        try:
            await previous.close()
        except Exception:
            pass

    async def __call__(self, payload: dict[str, str]) -> dict[str, Any]:
        async with self.lock:
            if self.wait_before_next:
                await asyncio.sleep(self.wait_before_next)
            if self.page.is_closed():
                try:
                    await self._replace_page()
                except Exception as error:
                    return {"status": "web_error", "error": f"Follower page recovery failed: {str(error)[:400]}", "source": "instagram_web"}
            result: dict[str, Any] = {
                "status": "web_error",
                "error": "Follower lookup did not run.",
                "source": "instagram_web",
            }
            for attempt in range(self.max_attempts):
                result = await request_web_follower_count(self.page, payload["username"])
                if result.get("status") != "web_error" or attempt + 1 >= self.max_attempts:
                    break
                try:
                    if self.retry_delay_seconds:
                        await asyncio.sleep(self.retry_delay_seconds * (attempt + 1))
                    await self._replace_page()
                except Exception as error:
                    result = {"status": "web_error", "error": f"Follower page retry failed: {str(error)[:400]}", "source": "instagram_web"}
                    break
            self.completed_since_recycle += 1
            if self.completed_since_recycle >= FOLLOWER_PAGE_RECYCLE_LOOKUP_COUNT and result["status"] not in {"rate_limited", "login_required", "challenge_required"}:
                try:
                    await self._replace_page()
                    self.completed_since_recycle = 0
                except Exception:
                    pass
            self.wait_before_next = follower_lookup_delay_seconds(result, self.interval_seconds)
            return result


@dataclass
class FollowerRuntime:
    browser: Any
    context: Any
    page: Any


async def create_background_follower_runtime(chromium: Any, source_context: Any, executable_path: str) -> FollowerRuntime:
    """Create the isolated headless runtime used only for follower enrichment."""
    storage_state = await source_context.storage_state()
    browser = await chromium.launch(executable_path=executable_path, headless=True)
    context = await browser.new_context(storage_state=storage_state, viewport={"width": 1440, "height": 1000})
    return FollowerRuntime(browser, context, await context.new_page())


def locate_browser_executable() -> str:
    configured = os.environ.get("INSTAGRAM_BROWSER_EXECUTABLE", "").strip()
    candidates = [
        configured,
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("A supported Chrome or Edge executable was not found.")


def load_playwright() -> Callable[[], Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:
        raise RuntimeError(
            "Python Playwright is required. Install it with: python -m pip install playwright"
        ) from error
    return async_playwright


async def async_input(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def wait_for_login_confirmation(prompt: str, stop_event: asyncio.Event) -> bool:
    """Wait for Enter without letting a Ctrl+C leave the collector blocked on input."""
    print(prompt, end="", flush=True)
    if os.name == "nt":
        import msvcrt

        while not stop_event.is_set():
            if msvcrt.kbhit() and msvcrt.getwch() in {"\r", "\n"}:
                print()
                return True
            await asyncio.sleep(0.05)
        return False

    input_task = asyncio.create_task(asyncio.to_thread(input))
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({input_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if input_task in done:
        input_task.result()
        return True
    return False


async def safe_close(value: Any) -> None:
    if value is None:
        return
    try:
        await value.close()
    except Exception:
        pass


async def launch_collection_context(
    chromium: Any,
    executable_path: str,
    options: argparse.Namespace,
) -> tuple[Any, Any]:
    # --background must be genuinely headless. Exact Reel metrics are parsed
    # inside the page context, so they do not require a visible Edge window.
    browser_args = [] if options.background else ["--start-maximized"]
    viewport = {"width": 1440, "height": 1000} if options.background else None
    if options.no_login:
        browser = await chromium.launch(
            executable_path=executable_path,
            headless=options.background,
            args=browser_args,
        )
        context = await browser.new_context(viewport=viewport)
        return browser, context
    options.profile_dir.mkdir(parents=True, exist_ok=True)
    context = await chromium.launch_persistent_context(
        str(options.profile_dir),
        executable_path=executable_path,
        headless=options.background,
        viewport=viewport,
        args=browser_args,
    )
    # Keep Chromium's first startup tab.  Closing every restored page here
    # forces create_collection_page() to call Target.createTarget later, which
    # fails in some Windows Chrome sessions before a visible tab can open.
    # Extra restored tabs are not part of this collection and can be closed.
    for restored_page in list(context.pages)[1:]:
        await safe_close(restored_page)
    return None, context


def initial_collection_page_url(start_url: str, *, background: bool, no_login: bool) -> str:
    """Keep every collection mode on its requested Instagram surface."""
    del background, no_login
    return start_url


async def run_collector(
    options: argparse.Namespace,
    *,
    external_stop_event: asyncio.Event | None = None,
    register_signal_handler: bool = True,
    shared_context: Any | None = None,
    shared_browser: Any | None = None,
    shared_playwright_runtime: Any | None = None,
    enable_stop_input: bool = True,
) -> int:
    stop_requested = False
    stop_event = external_stop_event or asyncio.Event()
    event_loop = asyncio.get_running_loop()
    interrupt_count = 0
    status_reporter: CollectorStatusReporter | None = None
    reel_store: LongReelStore | None = None
    collector_lock: CollectorLock | None = None
    browser: Any = None
    context: Any = None
    page: Any = None
    follower_runtime: FollowerRuntime | None = None
    view_page: Any = None
    playwright_runtime: Any = None
    follower_enricher: FollowerEnricher | None = None
    android_enricher: AndroidReelMetricsEnricher | None = None
    android_pipeline: AndroidMetricPipeline | None = None
    detached_android_metrics = False
    rate_limit_state: InstagramRateLimitState | None = None
    consecutive_web_data_failures = 0
    aborted_by_failure_limit = False
    exit_code = 0
    owns_collection_context = shared_context is None

    def request_graceful_stop(source: str) -> bool:
        nonlocal stop_requested
        if stop_requested:
            return False
        stop_requested = True
        stop_event.set()
        print(
            f"{source}: 새 릴스 수집을 멈춥니다. 대기 중인 팔로워 조회 후 CSV/XLSX 저장을 진행합니다.",
            file=sys.stderr,
        )
        return True

    def handle_interrupt(_signum: int, _frame: Any) -> None:
        nonlocal interrupt_count
        interrupt_count += 1
        if interrupt_count == 1:
            request_graceful_stop("중지 요청")
            print("강제 종료하려면 Ctrl+C를 한 번 더 누르세요.", file=sys.stderr)
        else:
            os._exit(130)

    previous_sigint: Any = None
    stop_watcher: asyncio.Task[None] | None = None
    if register_signal_handler:
        previous_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, handle_interrupt)
    elif external_stop_event is not None:
        async def watch_for_external_stop() -> None:
            await external_stop_event.wait()
            request_graceful_stop("중지 요청")

        stop_watcher = asyncio.create_task(watch_for_external_stop())
    if enable_stop_input and options.background and sys.stdin.isatty():
        def read_stop_input() -> None:
            try:
                if sys.stdin.readline().strip():
                    request_stop_threadsafe(event_loop, request_graceful_stop, "입력 감지")
            except Exception:
                pass

        threading.Thread(target=read_stop_input, daemon=True).start()
        print("릴스 수집만 멈추고 저장하려면 q 같은 글자를 입력한 뒤 Enter를 누르세요.")

    try:
        options.data_dir.mkdir(parents=True, exist_ok=True)
        collector_lock = await CollectorLock(options.data_dir).acquire()
        # A stop marker belongs to the previous run. Starting the collector
        # explicitly is the operator's acknowledgement to try again.
        await asyncio.to_thread(clear_collection_stop_request, options.data_dir)
        status_reporter = CollectorStatusReporter(options.data_dir, options)
        append_collection_log(
            options.data_dir,
            "python",
            "collector_started",
            mode="followers" if options.followers_only else "reels",
            max_items=options.max_items,
            hashtags=options.hashtags,
            android_metrics=options.android_metrics,
        )

        async def update_status(patch: dict[str, Any], force: bool = False) -> None:
            try:
                await status_reporter.update(patch, force)
            except Exception as error:
                print(f"상태 파일 저장 실패: {error}", file=sys.stderr)

        async def record_web_collection_failure(reason: str, url: str = "") -> bool:
            nonlocal consecutive_web_data_failures, aborted_by_failure_limit
            consecutive_web_data_failures += 1
            append_collection_log(
                options.data_dir,
                "python",
                "reel_collection_failure",
                consecutive_failures=consecutive_web_data_failures,
                maximum_failures=COLLECTION_MAX_CONSECUTIVE_FAILURES,
                url=url,
                reason=reason,
            )
            if consecutive_web_data_failures < COLLECTION_MAX_CONSECUTIVE_FAILURES:
                return False
            aborted_by_failure_limit = True
            stop = await asyncio.to_thread(
                request_collection_stop,
                options.data_dir,
                source="python",
                reason=f"{COLLECTION_MAX_CONSECUTIVE_FAILURES} consecutive browser Reel collection failures.",
                consecutive_failures=consecutive_web_data_failures,
                url=url,
            )
            request_graceful_stop("웹 연속 수집 실패 한도")
            await update_status(
                {
                    "state": "stopped",
                    "last_error": stop["reason"],
                    "consecutive_web_failures": consecutive_web_data_failures,
                },
                True,
            )
            return True

        def record_web_collection_success() -> None:
            nonlocal consecutive_web_data_failures
            consecutive_web_data_failures = 0

        async def stop_if_shared_failure_limit_reached() -> bool:
            nonlocal aborted_by_failure_limit
            stop = await asyncio.to_thread(read_collection_stop_request, options.data_dir)
            if stop is None:
                return False
            aborted_by_failure_limit = True
            request_graceful_stop(f"{stop.get('source', 'collection')} 연속 수집 실패 한도")
            await update_status(
                {
                    "state": "stopped",
                    "last_error": str(stop.get("reason", "collection failure limit reached")),
                    "failure_source": stop.get("source", ""),
                    "consecutive_failures": stop.get("consecutive_failures", 0),
                },
                True,
            )
            return True

        await update_status({"state": "followers" if options.followers_only else "collecting"}, True)
        csv_path = options.data_dir / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
        refresh_urls = load_reel_urls(options.urls_file) if not options.followers_only and options.urls_file else []
        anonymous_refresh = bool(options.no_login and refresh_urls)
        hybrid_android_metrics = bool(options.android_metrics and not anonymous_refresh)
        detached_android_metrics = hybrid_android_metrics and not options.android_metrics_required
        start_url = refresh_urls[0] if refresh_urls else (hashtag_page_url(options.hashtags[0]) if options.hashtags else options.start_url)
        if not options.followers_only:
            reel_store = await LongReelStore.create(
                csv_path,
                options.checkpoint_items,
                options.xlsx_layout,
                options.disable_recollect_cooldown,
            )
            await asyncio.to_thread(apply_completed_android_metric_jobs, options.data_dir)
            await reel_store.merge_external_android_metrics()
        executable_path = locate_browser_executable()
        if owns_collection_context:
            async_playwright = load_playwright()
            playwright_runtime = await async_playwright().start()
            chromium = playwright_runtime.chromium
            browser, context = await launch_collection_context(chromium, executable_path, options)
        else:
            if shared_playwright_runtime is None:
                raise RuntimeError("A shared Playwright runtime is required with a shared collection context.")
            playwright_runtime = shared_playwright_runtime
            chromium = playwright_runtime.chromium
            browser = shared_browser
            context = shared_context
        rate_limit_state = rate_limit_state_for_context(context)

        async def ensure_follower_runtime() -> SequentialWebFollowerLookup:
            nonlocal follower_runtime
            if follower_runtime is None:
                follower_runtime = await create_background_follower_runtime(chromium, context, executable_path)
            lookup = SequentialWebFollowerLookup(
                follower_runtime.page,
                options.follower_interval_seconds,
                max_attempts=options.exact_metric_attempts,
                retry_delay_seconds=options.exact_metric_retry_delay_seconds,
            )
            if follower_enricher is not None:
                follower_enricher.set_lookup_impl(lookup)
            return lookup

        async def ensure_view_page() -> Any:
            nonlocal view_page
            is_closed = getattr(view_page, "is_closed", None)
            if view_page is None or (callable(is_closed) and is_closed()):
                view_page = await context.new_page()
            return view_page

        async def start_follower_enricher(defer_runtime: bool = False) -> FollowerEnricher:
            nonlocal follower_enricher

            async def deferred_lookup(_payload: dict[str, str]) -> dict[str, Any]:
                raise RuntimeError("Follower lookup runtime has not started yet.")

            lookup: Any = deferred_lookup if defer_runtime else await ensure_follower_runtime()

            def report_follower(progress: dict[str, Any]) -> None:
                if progress["status"] == "success":
                    outcome = f"{int(progress['followerCount']):,}"
                else:
                    error_suffix = f" ({progress['error']})" if progress["error"] else ""
                    outcome = f"{progress['status']}{error_suffix}"
                completed = options.progress_offset + int(progress["completed"])
                queued = options.progress_offset + int(progress["queued"])
                print(f"[Follower {completed}/{queued}] @{progress['username']} -> {outcome}")
                asyncio.create_task(update_status({
                    "follower_completed": progress["completed"],
                    "follower_queued": progress["queued"],
                    "follower_last_status": progress["status"],
                    "follower_last_username": progress["username"],
                }))

            follower_enricher = FollowerEnricher(
                data_dir=options.data_dir,
                concurrency=1,
                lookup_impl=lookup,
                source="instagram_web",
                on_progress=report_follower,
            )
            await follower_enricher.ready()
            return follower_enricher

        if options.followers_only:
            follower_enricher = await start_follower_enricher()
            if owns_collection_context:
                await safe_close(context)
                context = None
            queued = await follower_enricher.enqueue_all_exact()
            print(f"Follower web lookups queued: {queued}")
            stats = await follower_enricher.drain()
            print(f"Follower web lookups finished: success={stats['success']} unavailable={stats['unavailable']} failed={stats['failed']}")
            if stats["stopStatus"]:
                print(f"Follower web lookup stopped ({stats['stopStatus']}): {stats['stopError']}", file=sys.stderr)
                exit_code = 2
            merged = await asyncio.to_thread(
                merge_follower_data_into_long_reels,
                csv_path,
                ensure_user_history(options.data_dir),
            )
            print(f"Follower data merged into {csv_path.name}: {merged}")
            users_xlsx = await asyncio.to_thread(write_users_xlsx, options.data_dir)
            print(f"사용자 정보 저장 완료: {users_xlsx}")
            if csv_path.exists():
                output_fields, output_rows = read_csv_objects(csv_path)
                await asyncio.to_thread(
                    write_long_output_bundle,
                    csv_path,
                    output_rows,
                    output_fields,
                    xlsx_layout=options.xlsx_layout,
                )
            await safe_close(follower_runtime.browser if follower_runtime else None)
            follower_runtime = None
            await status_reporter.finish("completed_with_errors" if stats["stopStatus"] else "completed", {"follower_success": stats["success"], "follower_failed": stats["failed"]})
            return exit_code

        # users.xlsx is owned exclusively by the Python profile collector.
        # Start it for every logged-in Reel run so new Reel authors are added
        # to the Python user history and exported after collection. The
        # optional after-reels mode records authors during Reel collection and
        # postpones their web profile lookups until the Reel loop has ended.
        # Anonymous browser sessions cannot read profile snapshots, so retain
        # their existing no-login behavior instead of writing false failures.
        if not options.no_login:
            follower_enricher = await start_follower_enricher(
                defer_runtime=options.followers_after_reels,
            )

        seen: set[str] = set()
        exact_follower_cache: dict[str, dict[str, Any]] = {}
        reel_metadata: dict[str, dict[str, Any]] = {}
        initial_page_url = initial_collection_page_url(
            start_url,
            background=options.background,
            no_login=options.no_login,
        )
        if options.no_login and refresh_urls:
            # Do not make the first saved URL a gate for the entire anonymous
            # refresh. Each URL is checked in the direct-refresh loop so a
            # login-walled Reel can be skipped and the remaining public URLs
            # can still be collected.
            page = await context.new_page()
            attach_reel_metadata_collector(page, reel_metadata, rate_limit_state)
            print("로그인 프로필을 사용하지 않는 임시 브라우저로 공개 릴스를 URL별 확인합니다.")
        else:
            try:
                page = await create_collection_page(
                    context,
                    initial_page_url,
                    reel_metadata,
                    allow_login=not options.background and not options.no_login,
                    keep_open_on_navigation_failure=not options.background and not options.no_login,
                    rate_limit_state=rate_limit_state,
                )
            except CrawlerAccessError as error:
                if options.no_login and error.code == "login_required":
                    raise CrawlerAccessError(
                        "anonymous_access_blocked",
                        "Instagram requires login for this request. Anonymous collection can only continue for public Reel pages that Instagram exposes without login.",
                    ) from error
                raise
        if options.no_login and not refresh_urls:
            expected_surface = is_instagram_hashtag_surface(page.url) if options.hashtags else is_instagram_reels_surface(page.url)
            if not expected_surface:
                raise CrawlerAccessError(
                    "anonymous_access_blocked",
                    "Instagram did not expose a public Reel page without login. Retry later or use the logged-in Python collector.",
                )
            print("로그인 프로필을 사용하지 않는 임시 브라우저로 공개 릴스 수집을 시작합니다.")
        elif options.background:
            expected_surface = is_instagram_hashtag_surface(page.url) if options.hashtags else is_instagram_reels_surface(page.url)
            if not expected_surface:
                raise CrawlerAccessError("login_required", "Background mode needs a saved Instagram login. Run without --background, sign in once, and retry.")
            print("저장된 Instagram 브라우저 프로필로 창 없이 백그라운드 수집을 시작했습니다.")
        else:
            print("브라우저에서 Instagram에 로그인하고 릴스 화면을 연 뒤 이 창으로 돌아오세요.")
            if not await wait_for_login_confirmation("준비가 끝났으면 Enter를 누르세요: ", stop_event):
                request_graceful_stop("중지 요청")
                await status_reporter.finish("stopped", {"last_error": ""})
                return exit_code
            assert_instagram_page_access(page)
            expected_surface = is_instagram_hashtag_surface(page.url) if options.hashtags else is_instagram_reels_surface(page.url)
            if not expected_surface and not refresh_urls:
                await navigate_with_retries(page, start_url)
        if anonymous_refresh:
            print("무로그인 재수집: 기존 정적 정보는 보존하고, 새 공개 지표만 추가합니다.")
        else:
            print("릴스 페이지의 embedded/DOM 데이터와 같은 페이지의 수동 응답 관찰만 사용합니다.")

        captured = duplicate_count = missing_count = filtered_count = 0
        android_metrics_collected = android_metrics_unavailable = android_handoff_deferred = 0
        cooldown_skipped_count = page_recycle_count = transition_stall_count = recovery_failure_count = 0
        collected_shortcodes: set[str] = set()
        next_delay_seconds = options.interval_seconds
        if hybrid_android_metrics:
            android_enricher = AndroidReelMetricsEnricher(
                adb_path=options.android_adb_path,
                device_id=options.android_device_id,
                ui_delay_seconds=options.android_ui_delay_seconds,
            )
            print(
                "Android 준비: Instagram 앱에 로그인하고 에뮬레이터 잠금을 해제하세요. "
                "홈·검색·릴스 중 어느 화면에서 시작해도 URL로 자동 이동합니다."
            )
        def progress_patch(last_reel_url: str = "") -> dict[str, Any]:
            patch = {
                "captured": captured, "duplicates": duplicate_count, "missing": missing_count,
                "filtered": filtered_count, "cooldown_skipped": cooldown_skipped_count,
                "page_recycles": page_recycle_count, "transition_stalls": transition_stall_count,
                "recovery_failures": recovery_failure_count,
                "android_metrics_collected": android_metrics_collected,
                "android_metrics_unavailable": android_metrics_unavailable,
                "android_handoff_deferred": android_handoff_deferred,
                "android_metrics_pending": (
                    android_pipeline.backlog
                    if android_pipeline is not None
                    else sum(android_metric_queue_counts(options.data_dir).values())
                ),
            }
            if last_reel_url:
                patch["last_reel_url"] = last_reel_url
            return patch

        async def report_android_metric_result(
            browser_record: dict[str, Any],
            android_result: AndroidMetricResult,
        ) -> None:
            nonlocal android_metrics_collected, android_metrics_unavailable
            if android_result.status == "collected":
                android_metrics_collected += 1
            else:
                android_metrics_unavailable += 1
                print(
                    f"Android metrics unavailable: {browser_record['url']} ({android_result.error})",
                    file=sys.stderr,
                )
            rendered = " | ".join(
                f"{field}={android_result.display_value(field)}"
                for field in ("like_count", "view_count", "comment_count", "share_count", "repost_count", "saved_count", "audio_name")
            )
            print(f"[ANDROID] metrics | {rendered}")
            await asyncio.to_thread(
                append_collection_log,
                options.data_dir,
                "android",
                "reel_metric_collected" if android_result.status == "collected" else "reel_metric_unavailable",
                url=browser_record.get("url", ""),
                status=android_result.status,
                metrics=android_result.metrics,
                audio_name=android_result.audio_name,
                error=android_result.error,
            )
            await update_status(progress_patch(str(browser_record.get("url", ""))))

        if hybrid_android_metrics and options.android_metrics_required:
            if android_enricher is None or reel_store is None:
                raise RuntimeError("Android metric pipeline was not initialized.")
            android_pipeline = AndroidMetricPipeline(
                enricher=android_enricher,
                store=reel_store,
                on_result=report_android_metric_result,
                metrics_required=options.android_metrics_required,
                data_dir=options.data_dir,
            )
            android_pipeline.start()

        idle_hashtag_post_jobs_enqueued = False

        async def queue_idle_hashtag_post_counts() -> None:
            """Use an otherwise idle Android worker for exact Tag post totals."""
            nonlocal idle_hashtag_post_jobs_enqueued
            if (
                idle_hashtag_post_jobs_enqueued
                or not detached_android_metrics
                or not options.android_idle_hashtags
                or rate_limit_state.limited
            ):
                return
            job_ids = await asyncio.to_thread(
                enqueue_android_idle_hashtag_post_count_jobs,
                options.data_dir,
                options.android_idle_hashtags,
                adb_path=options.android_adb_path,
                device_id=options.android_device_id,
                ui_delay_seconds=options.android_ui_delay_seconds,
            )
            if not job_ids:
                return
            idle_hashtag_post_jobs_enqueued = True
            await asyncio.to_thread(
                start_android_metric_worker,
                options.data_dir,
                adb_path=options.android_adb_path,
                device_id=options.android_device_id,
                ui_delay_seconds=options.android_ui_delay_seconds,
            )
            await asyncio.to_thread(
                append_collection_log,
                options.data_dir,
                "android",
                "idle_hashtag_post_jobs_queued",
                hashtags=options.android_idle_hashtags,
                job_count=len(job_ids),
            )
            print(
                "[ANDROID] Reel metric queue is idle; queued exact Tags post totals: "
                f"{len(job_ids)} hashtag(s)."
            )

        async def log_python_reel(
            collected: dict[str, Any],
            *,
            android_status: str = "",
            android_job_id: str = "",
        ) -> None:
            await asyncio.to_thread(
                append_collection_log,
                options.data_dir,
                "python",
                "reel_saved",
                url=collected.get("url", ""),
                user_id=collected.get("user_id", ""),
                username=collected.get("username", ""),
                title=collected.get("title", ""),
                hashtags=collected.get("hashtags", ""),
                uploaded_at=collected.get("uploaded_at", ""),
                video_duration_seconds=collected.get("video_duration_seconds", ""),
                metrics={field: collected.get(field, "") for field in CSV_FIELDS if field.endswith("_count")},
                android_status=android_status,
                android_job_id=android_job_id,
            )

        async def store_hybrid_reel(
            record: dict[str, Any],
            collected: dict[str, Any],
        ) -> dict[str, Any]:
            """Persist Python-visible fields and hand the same URL to Android."""
            nonlocal next_delay_seconds, android_handoff_deferred
            if reel_store is None:
                raise RuntimeError("Reel store was not initialized.")
            handoff_missing = missing_python_to_android_handoff_fields(collected)
            handoff_delay = python_to_android_handoff_delay_seconds(collected)
            if handoff_delay:
                android_handoff_deferred += 1
            # Python does not wait for Android.  This is also used for
            # hashtag-grid candidates, where opening a second web detail page
            # would only increase the request rate.
            next_delay_seconds = REEL_SUCCESS_INTERVAL_SECONDS
            stored = await reel_store.append(collected)
            seen.add(record["shortcode"])
            if stored.get("skipped"):
                return {
                    **record,
                    "snapshotLabel": stored["label"],
                    "cooldownSkipped": True,
                    "cooldownLabel": stored.get("cooldownLabel", ""),
                    "nextCollectionAt": stored.get("nextCollectionAt", ""),
                    "collectionComplete": True,
                }
            collected_shortcodes.add(record["shortcode"])
            if follower_enricher is not None and (
                str(collected.get("user_id", "")).strip()
                or str(collected.get("username", "")).strip()
            ):
                await follower_enricher.track_user(
                    user_id=str(collected["user_id"]),
                    username=str(collected["username"]),
                    seen_at=str(collected["collected_at"]),
                    enqueue=not options.followers_after_reels,
                )
            if options.android_metrics_required:
                if android_pipeline is None:
                    raise RuntimeError("Android metric pipeline was not initialized.")
                android_pipeline.enqueue(
                    collected,
                    missing_python_fields=handoff_missing,
                    delay_seconds=handoff_delay,
                )
                android_status = "queued"
                android_job_id = ""
            else:
                android_job_id = await asyncio.to_thread(
                    enqueue_android_metric_job,
                    options.data_dir,
                    collected,
                    missing_python_fields=handoff_missing,
                    delay_seconds=handoff_delay,
                    adb_path=options.android_adb_path,
                    device_id=options.android_device_id,
                    ui_delay_seconds=options.android_ui_delay_seconds,
                )
                await asyncio.to_thread(
                    start_android_metric_worker,
                    options.data_dir,
                    adb_path=options.android_adb_path,
                    device_id=options.android_device_id,
                    ui_delay_seconds=options.android_ui_delay_seconds,
                )
                android_status = "background_queued"
            await log_python_reel(
                collected,
                android_status=android_status,
                android_job_id=android_job_id,
            )
            return {
                **record,
                "snapshotLabel": stored["label"],
                "cooldownSkipped": bool(stored.get("skipped")),
                "cooldownLabel": stored.get("cooldownLabel", ""),
                "nextCollectionAt": stored.get("nextCollectionAt", ""),
                "collectionComplete": True,
                "likeCountUnavailable": False,
                "androidMetricStatus": android_status,
            }

        async def capture_hashtag_grid_candidate(url: str) -> dict[str, Any] | None:
            """Save a hashtag-grid candidate without opening its detail URL."""
            if rate_limit_state is not None:
                rate_limit_state.raise_if_limited()
            normalized = normalize_reel_url(url)
            if normalized is None:
                return None
            shortcode = normalized["shortcode"]
            response_metadata = reel_metadata.get(shortcode, {})
            record = build_hashtag_grid_candidate_record(normalized["url"], response_metadata)
            if record is None:
                return None
            if shortcode in seen:
                return {**record, "duplicateInRun": True}
            collected = build_collected_record(record, response_metadata)
            reel_metadata.pop(shortcode, None)
            if options.max_upload_age_days > 0 and not parse_datetime(collected.get("uploaded_at")):
                seen.add(shortcode)
                return {**record, "uploadDateUnavailable": True}
            if not is_within_upload_age_days(collected, options.max_upload_age_days):
                seen.add(shortcode)
                return {
                    **record,
                    "uploadAgeFilteredOut": True,
                    "uploadAgeDays": collected["days_since_upload"],
                }
            return await store_hybrid_reel(record, collected)

        async def capture_current_reel(
            target_page: Any = None,
            target_metadata: dict[str, dict[str, Any]] | None = None,
            metadata_timeout_milliseconds: int = PASSIVE_RESPONSE_METADATA_TIMEOUT_MILLISECONDS,
        ) -> dict[str, Any] | None:
            nonlocal next_delay_seconds, android_metrics_collected, android_metrics_unavailable, android_handoff_deferred
            active_page = target_page or page
            metadata = target_metadata if target_metadata is not None else reel_metadata
            next_delay_seconds = options.interval_seconds
            if rate_limit_state is not None:
                rate_limit_state.raise_if_limited()
            await expand_visible_caption(active_page)
            # Instagram occasionally exposes the options button to the caption
            # expander as a hidden "more" label. Escape closes that transient
            # menu before metadata extraction and before the final Reel is left on screen.
            await active_page.keyboard.press("Escape")
            record = await extract_visible_reel(active_page)
            if not record:
                return None
            if record["shortcode"] in seen:
                next_delay_seconds = REEL_SUCCESS_INTERVAL_SECONDS
                return {**record, "duplicateInRun": True}
            response_metadata, used_passive_response = await resolve_page_first_reel_metadata(
                active_page,
                record,
                metadata,
                metadata_timeout_milliseconds,
                require_complete_metrics=not hybrid_android_metrics,
            )
            if rate_limit_state is not None:
                rate_limit_state.raise_if_limited()
            collected = build_collected_record(record, response_metadata)
            metadata.pop(record["shortcode"], None)
            like_count_unavailable = should_mark_like_count_unavailable(
                record,
                response_metadata,
            )
            if like_count_unavailable and not hybrid_android_metrics:
                collected["like_count"] = UNAVAILABLE_LIKE_COUNT_MARKER
            if options.max_upload_age_days > 0 and not parse_datetime(collected.get("uploaded_at")):
                seen.add(record["shortcode"])
                return {**record, "uploadDateUnavailable": True}
            if not is_within_upload_age_days(collected, options.max_upload_age_days):
                seen.add(record["shortcode"])
                return {**record, "uploadAgeFilteredOut": True, "uploadAgeDays": collected["days_since_upload"]}

            if hybrid_android_metrics:
                return await store_hybrid_reel(record, collected)

            exact_views = exact_view_counts_from_metadata(record["shortcode"], response_metadata)
            if anonymous_refresh:
                anonymous_probe = build_anonymous_refresh_from_history(reel_store.rows, collected)
                if anonymous_probe is None:
                    seen.add(record["shortcode"])
                    return {
                        **record,
                        "exactMetricUnavailable": True,
                        "exactMetricStatus": "anonymous_refresh_history_unavailable",
                        "exactMetricError": "A previous Reel history row is required to preserve static metadata during anonymous refresh.",
                    }
                username = str(anonymous_probe.get("username") or "").strip().lstrip("@")
                follower_key = str(anonymous_probe.get("user_id") or "") or username.casefold()
                follower_result = exact_follower_result_from_metadata(response_metadata)
                if follower_result is not None and follower_key:
                    exact_follower_cache[follower_key] = follower_result
                elif follower_key in exact_follower_cache:
                    follower_result = exact_follower_cache[follower_key]
                else:
                    follower_result = {
                        "status": "profile_unavailable",
                        "error": "Exact follower_count was not present in the current Reel page or its responses.",
                        "source": "instagram_page",
                    }
                anonymous_collected = build_anonymous_refresh_from_exact_metrics(
                    reel_store.rows,
                    collected,
                    record["shortcode"],
                    exact_views,
                    follower_result,
                )
                if anonymous_collected is None:
                    seen.add(record["shortcode"])
                    return {
                        **record,
                        "exactMetricUnavailable": True,
                        "exactMetricStatus": "anonymous_refresh_history_unavailable",
                        "exactMetricError": "A previous Reel history row is required to preserve static metadata during anonymous refresh.",
                    }
                next_delay_seconds = REEL_SUCCESS_INTERVAL_SECONDS
                stored = await reel_store.append(anonymous_collected)
                if not stored.get("skipped"):
                    collected_shortcodes.add(record["shortcode"])
                    await log_python_reel(anonymous_collected)
                seen.add(record["shortcode"])
                public_metric_fields = ["view_count", "comment_count"]
                return {
                    **record,
                    "snapshotLabel": stored["label"],
                    "cooldownSkipped": bool(stored.get("skipped")),
                    "cooldownLabel": stored.get("cooldownLabel", ""),
                    "nextCollectionAt": stored.get("nextCollectionAt", ""),
                    "collectionComplete": (
                        all(exact_nonnegative_integer(anonymous_collected.get(field)) is not None for field in public_metric_fields)
                        and (
                            exact_nonnegative_integer(anonymous_collected.get("like_count")) is not None
                            or is_like_count_marked_unavailable(anonymous_collected.get("like_count"))
                        )
                    ),
                    "likeCountUnavailable": like_count_unavailable,
                }
            missing_engagement_field = next(
                (
                    field
                    for field in ["comment_count"]
                    if exact_nonnegative_integer(collected.get(field)) is None
                ),
                "",
            )
            if missing_engagement_field:
                seen.add(record["shortcode"])
                return {
                    **record,
                    "exactMetricUnavailable": True,
                    "exactMetricStatus": f"exact_{missing_engagement_field}_unavailable",
                    "exactMetricError": (
                        f"Exact {missing_engagement_field} was not present in the embedded/DOM page data"
                        + (" or the page's passive JSON responses." if used_passive_response else ".")
                    ),
                }
            follower_payload = {"user_id": str(collected["user_id"]), "username": str(collected["username"]), "seen_at": str(collected["collected_at"])}
            follower_key = follower_payload["user_id"] or follower_payload["username"].casefold()
            follower_result = exact_follower_result_from_metadata(response_metadata)
            if follower_result is not None and follower_key:
                exact_follower_cache[follower_key] = follower_result
            elif follower_key in exact_follower_cache:
                follower_result = exact_follower_cache[follower_key]
            else:
                follower_result = {
                    "status": "profile_unavailable",
                    "error": "Exact follower_count was not present in the current Reel page or its responses.",
                    "source": "instagram_page",
                }
            exact_result = apply_exact_metric_results(
                collected,
                record["shortcode"],
                exact_views,
                follower_result,
            )
            if exact_result["status"] != "success":
                seen.add(record["shortcode"])
                return {
                    **record,
                    "exactMetricUnavailable": True,
                    "exactMetricStatus": exact_result["status"],
                    "exactMetricError": exact_result["error"],
                }
            if not has_complete_reel_core_data(collected):
                seen.add(record["shortcode"])
                return {
                    **record,
                    "exactMetricUnavailable": True,
                    "exactMetricStatus": "exact_core_data_unavailable",
                    "exactMetricError": "Required exact page fields or Reel identity fields were unavailable.",
                }
            if follower_enricher is not None:
                await follower_enricher.track_user(**follower_payload)
            next_delay_seconds = REEL_SUCCESS_INTERVAL_SECONDS
            stored = await reel_store.append(collected)
            if not stored.get("skipped"):
                collected_shortcodes.add(record["shortcode"])
                await log_python_reel(collected)
            seen.add(record["shortcode"])
            return {
                **record,
                "snapshotLabel": stored["label"],
                "cooldownSkipped": bool(stored.get("skipped")),
                "cooldownLabel": stored.get("cooldownLabel", ""),
                "nextCollectionAt": stored.get("nextCollectionAt", ""),
                "collectionComplete": has_complete_reel_core_data(collected),
                "likeCountUnavailable": like_count_unavailable,
            }

        direct_urls = refresh_urls

        async def report_direct_result(index: int, url: str, record: dict[str, Any] | None) -> None:
            nonlocal captured, duplicate_count, missing_count, filtered_count, cooldown_skipped_count
            if record and record.get("duplicateInRun"):
                duplicate_count += 1
                print(f"실행 중 중복 건너뜀: {record['url']}")
            elif record and record.get("uploadAgeFilteredOut"):
                filtered_count += 1
                age = "unknown" if record.get("uploadAgeDays") == "" else f"{record.get('uploadAgeDays')}day"
                print(f"Upload age skipped ({age}, max {options.max_upload_age_days}day): {url}")
            elif record and record.get("uploadDateUnavailable"):
                filtered_count += 1
                print(f"업로드 날짜 미확인으로 저장 안 함: {url}")
            elif record and record.get("exactMetricUnavailable"):
                missing_count += 1
                print(
                    f"정확 지표 누락으로 저장 안 함: {url} "
                    f"({record.get('exactMetricStatus')}: {record.get('exactMetricError')})",
                    file=sys.stderr,
                )
                await record_web_collection_failure(
                    str(record.get("exactMetricError", "Required Reel data was unavailable.")),
                    str(record.get("url", url)),
                )
            elif record and record.get("cooldownSkipped"):
                cooldown_skipped_count += 1
                print(f"{record.get('cooldownLabel') or '재수집 간격'} 이내 중복 건너뜀: {record['url']}")
            elif record:
                captured += 1
                record_web_collection_success()
                print(f"[PYTHON] {stored_reel_progress_line(captured, options.max_items, record['url'], progress_offset=options.progress_offset)}")
            else:
                missing_count += 1
                print(f"수집 실패: {url}", file=sys.stderr)
                await record_web_collection_failure("No Reel record could be extracted.", url)
            await update_status(progress_patch(record.get("url", "") if record else ""))

        async def report_direct_error(index: int, url: str, error: Exception) -> None:
            nonlocal missing_count
            missing_count += 1
            print(f"수집 실패: {url} ({error})", file=sys.stderr)
            await record_web_collection_failure(str(error), url)
            await update_status({**progress_patch(url), "last_error": str(error)[:500]})

        async def report_anonymous_login_skip(index: int, url: str, error: CrawlerAccessError) -> None:
            nonlocal missing_count
            missing_count += 1
            print(f"무로그인 접근 불가로 건너뜀: {url} ({error})", file=sys.stderr)
            await update_status({**progress_patch(url), "last_error": str(error)[:500]})

        if options.hashtags:
            attempted_hashtag_urls: set[str] = set()

            async def discover_hashtag_urls() -> list[str]:
                nonlocal filtered_count, cooldown_skipped_count
                android_tag_job_id = ""
                android_tag_task: asyncio.Task[list[dict[str, object]]] | None = None
                if hybrid_android_metrics:
                    if detached_android_metrics:
                        android_tag_job_id = await asyncio.to_thread(
                            enqueue_android_hashtag_post_count_job,
                            options.data_dir,
                            options.hashtags,
                            adb_path=options.android_adb_path,
                            device_id=options.android_device_id,
                            ui_delay_seconds=options.android_ui_delay_seconds,
                        )
                        await asyncio.to_thread(
                            start_android_metric_worker,
                            options.data_dir,
                            adb_path=options.android_adb_path,
                            device_id=options.android_device_id,
                            ui_delay_seconds=options.android_ui_delay_seconds,
                        )
                        print(f"[ANDROID] related hashtag collection started: {len(options.hashtags)} query tag(s).")
                    elif android_enricher is not None:
                        android_tag_task = asyncio.create_task(
                            asyncio.to_thread(
                                android_enricher.collect_related_hashtag_post_counts,
                                options.hashtags,
                            )
                        )
                try:
                    discovered = await collect_hashtag_reel_urls(
                        page,
                        options.hashtags,
                        options.max_items,
                        reel_metadata,
                        should_stop=lambda: stop_requested,
                        candidates_per_keyword=options.hashtag_candidates_per_keyword,
                        rate_limit_state=rate_limit_state,
                    )
                except CrawlerAccessError as error:
                    if options.max_items == 0 and error.code == "hashtag_reels_not_found":
                        print("해시태그에서 새 릴스 후보를 찾지 못했습니다.")
                        return []
                    raise
                if android_tag_job_id:
                    tag_result = await wait_for_android_hashtag_post_count_job(
                        options.data_dir,
                        android_tag_job_id,
                        stop_event,
                    )
                    if tag_result is None:
                        return []
                    print(
                        "[ANDROID] related hashtag collection finished: "
                        f"{tag_result.get('status', 'unavailable')} (hashtags.xlsx/json/csv)"
                    )
                elif android_tag_task is not None:
                    hashtag_rows = await android_tag_task
                    hashtag_paths = await asyncio.to_thread(write_hashtag_post_counts, options.data_dir, hashtag_rows)
                    for hashtag_row in hashtag_rows:
                        await asyncio.to_thread(
                            append_collection_log,
                            options.data_dir,
                            "android",
                            "related_hashtag_collected",
                            query_hashtag=hashtag_row.get("query_hashtag", ""),
                            hashtag=hashtag_row.get("hashtag", ""),
                            post_count=hashtag_row.get("post_count", ""),
                            status=hashtag_row.get("status", ""),
                            error=hashtag_row.get("error", ""),
                        )
                    collected_tag_count = sum(row.get("status") == "collected" for row in hashtag_rows)
                    print(
                        "[ANDROID] related hashtag collection finished: "
                        f"{collected_tag_count}/{len(hashtag_rows)} ({hashtag_paths['csv'].name})"
                    )
                prefiltered = prefilter_hashtag_reel_urls(
                    discovered,
                    reel_metadata,
                    reel_store.rows,
                    options.max_upload_age_days,
                    required_hashtags=options.hashtags,
                )
                filtered_count += prefiltered["uploadAgeSkipped"]
                cooldown_skipped_count += prefiltered["cooldownSkipped"]
                candidates = prefiltered["urls"]
                if options.new_urls_only:
                    candidates = filter_new_urls(candidates, reel_store.rows)
                candidates = unattempted_hashtag_urls(candidates, attempted_hashtag_urls)
                if not candidates:
                    await queue_idle_hashtag_post_counts()
                print(
                    "후보 사전 필터: "
                    f"전체={prefiltered['total']}, "
                    f"재수집 대기={prefiltered['cooldownSkipped']}, "
                    f"기존 이력 날짜 기준 기간 초과={prefiltered['uploadAgeSkipped']}, "
                    f"날짜 확인을 위해 상세 검사={prefiltered['ageUnknown']}, "
                    f"새 상세 페이지 후보={len(candidates)}"
                )
                return candidates

            while not stop_requested and (options.max_items == 0 or captured < options.max_items):
                if await stop_if_shared_failure_limit_reached():
                    break
                hashtag_urls = await discover_hashtag_urls()
                captured_before_candidate_batch = captured
                if hybrid_android_metrics and hashtag_urls:
                    print(
                        "[PYTHON] Hashtag candidates use already-rendered grid HTML; "
                        "no Reel detail URLs will be opened."
                    )
                for index, url in enumerate(hashtag_urls):
                    if (
                        stop_requested
                        or await stop_if_shared_failure_limit_reached()
                        or (options.max_items and captured >= options.max_items)
                    ):
                        break
                    attempted_hashtag_urls.add(url)
                    try:
                        if hybrid_android_metrics:
                            await report_direct_result(
                                index,
                                url,
                                await capture_hashtag_grid_candidate(url),
                            )
                        else:
                            await navigate_with_retries(page, reel_detail_page_url(url))
                            if options.manual:
                                await async_input("현재 릴스를 다시 수집하려면 Enter를 누르세요: ")
                            else:
                                await page.wait_for_timeout(next_delay_seconds * 1000)
                            await report_direct_result(index, url, await capture_current_reel())
                    except CrawlerAccessError:
                        raise
                    except Exception as error:
                        await report_direct_error(index, url, error)

                if not stop_requested and captured == captured_before_candidate_batch:
                    # Android is genuinely idle only after every candidate in this
                    # batch has failed to yield a usable Reel.  Starting this work
                    # on the first skipped URL unnecessarily increased Instagram
                    # traffic while Python was still visiting the remaining URLs.
                    await queue_idle_hashtag_post_counts()
                if stop_requested or (options.max_items and captured >= options.max_items):
                    break
                if options.max_items:
                    break
                print(
                    "처리할 새 해시태그 후보가 없어 "
                    f"{HASHTAG_REDISCOVERY_INTERVAL_SECONDS}초 뒤 다시 검색합니다."
                )
                if await wait_for_stop_or_timeout(stop_event, HASHTAG_REDISCOVERY_INTERVAL_SECONDS):
                    break
            requested = options.max_items if options.max_items else "until stopped"
            print(f"Hashtag OR collection finished: matched={captured}, requested={requested}")
        elif direct_urls:
            use_parallel = bool(refresh_urls and options.background and not options.manual and options.direct_concurrency > 1)
            if use_parallel:
                queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
                for direct_index, direct_url in enumerate(direct_urls):
                    queue.put_nowait((direct_index, direct_url))
                fatal_errors: list[Exception] = []

                async def run_direct_worker(worker_number: int) -> None:
                    nonlocal page_recycle_count
                    worker_metadata = reel_metadata if worker_number == 0 else {}
                    worker_page = page if worker_number == 0 else await context.new_page()
                    if worker_number:
                        attach_reel_metadata_collector(worker_page, worker_metadata, rate_limit_state)
                    processed = 0
                    try:
                        while not fatal_errors and not stop_requested:
                            if await stop_if_shared_failure_limit_reached():
                                return
                            try:
                                index, url = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                return
                            try:
                                await navigate_with_retries(worker_page, reel_detail_page_url(url))
                                await worker_page.wait_for_timeout(DIRECT_REEL_SETTLE_MILLISECONDS)
                                record = await capture_current_reel(worker_page, worker_metadata, DIRECT_REEL_METADATA_TIMEOUT_MILLISECONDS)
                                await report_direct_result(index, url, record)
                            except CrawlerAccessError as error:
                                if can_skip_anonymous_refresh_access_error(error, no_login=options.no_login):
                                    await report_anonymous_login_skip(index, url, error)
                                else:
                                    fatal_errors.append(error)
                                    return
                            except Exception as error:
                                await report_direct_error(index, url, error)
                            finally:
                                queue.task_done()
                            processed += 1
                            if options.page_recycle_items > 0 and processed >= options.page_recycle_items and not queue.empty():
                                await safe_close(worker_page)
                                worker_metadata.clear()
                                worker_page = await context.new_page()
                                attach_reel_metadata_collector(worker_page, worker_metadata, rate_limit_state)
                                processed = 0
                                page_recycle_count += 1
                                await update_status({**progress_patch(), "state": "collecting", "recycle_reason": "direct refresh worker"}, True)
                    finally:
                        if worker_page is not page:
                            await safe_close(worker_page)

                workers = [asyncio.create_task(run_direct_worker(index)) for index in range(min(options.direct_concurrency, len(direct_urls)))]
                await asyncio.gather(*workers)
                if fatal_errors:
                    raise fatal_errors[0]
            else:
                for index, url in enumerate(direct_urls):
                    if stop_requested or await stop_if_shared_failure_limit_reached():
                        break
                    if options.hashtags and options.max_items and captured >= options.max_items:
                        break
                    try:
                        await navigate_with_retries(page, reel_detail_page_url(url))
                        if options.manual:
                            await async_input("현재 릴스를 다시 수집하려면 Enter를 누르세요: ")
                        else:
                            await page.wait_for_timeout(next_delay_seconds * 1000)
                        await report_direct_result(
                            index,
                            url,
                            await capture_current_reel(),
                        )
                    except CrawlerAccessError as error:
                        if can_skip_anonymous_refresh_access_error(error, no_login=options.no_login):
                            await report_anonymous_login_skip(index, url, error)
                            continue
                        raise
                    except Exception as error:
                        await report_direct_error(index, url, error)
            if options.hashtags:
                requested = options.max_items if options.max_items else "all candidates"
                print(f"Hashtag OR collection finished: matched={captured}, requested={requested}")
        else:
            items_since_recycle = views_since_recycle = consecutive_unproductive = 0
            consecutive_transition_stalls = consecutive_recovery_failures = 0
            last_reels_url = page.url
            transition_timeout_milliseconds = options.transition_timeout_seconds * 1000

            async def recycle_page(reason: str) -> None:
                nonlocal page, page_recycle_count, items_since_recycle, views_since_recycle
                nonlocal consecutive_unproductive, consecutive_transition_stalls, last_reels_url
                print(f"수집 탭 재생성 ({reason}): 진행={captured}, 중복={duplicate_count}, 누락={missing_count}", file=sys.stderr)
                page = await recycle_collection_page(
                    context,
                    page,
                    options.start_url,
                    reel_metadata,
                    rate_limit_state,
                )
                page_recycle_count += 1
                items_since_recycle = views_since_recycle = consecutive_unproductive = consecutive_transition_stalls = 0
                last_reels_url = page.url
                await update_status({**progress_patch(), "state": "collecting", "recycle_reason": reason}, True)

            while not stop_requested and (options.max_items == 0 or captured < options.max_items):
                if await stop_if_shared_failure_limit_reached():
                    break
                record: dict[str, Any] | None = None
                try:
                    if options.manual:
                        await async_input("현재 릴스를 저장하려면 Enter를 누르세요: ")
                    else:
                        await page.wait_for_timeout(next_delay_seconds * 1000)
                    if not is_instagram_reels_surface(page.url):
                        assert_instagram_page_access(page)
                        print("릴스 화면을 벗어나 마지막 릴스로 복귀합니다.", file=sys.stderr)
                        await navigate_with_retries(page, last_reels_url or options.start_url)
                        await page.wait_for_timeout(500)
                    record = await capture_current_reel()
                    views_since_recycle += 1
                    if record and record.get("uploadAgeFilteredOut"):
                        filtered_count += 1
                        consecutive_unproductive = 0
                        age = "unknown" if record.get("uploadAgeDays") == "" else f"{record.get('uploadAgeDays')}day"
                        print(f"Upload age skipped ({age}, max {options.max_upload_age_days}day): {record['url']}")
                    elif record and record.get("uploadDateUnavailable"):
                        filtered_count += 1
                        consecutive_unproductive = 0
                        print(f"업로드 날짜 미확인으로 저장 안 함: {record['url']}")
                    elif record and record.get("cooldownSkipped"):
                        cooldown_skipped_count += 1
                        consecutive_unproductive = 0
                        print(f"{record.get('cooldownLabel') or '재수집 간격'} 이내 저장 중복 건너뜀: {record['url']}")
                    elif record and record.get("duplicateInRun"):
                        duplicate_count += 1
                        consecutive_unproductive = 0
                        print(f"실행 중 재노출 릴스 건너뜀 (누적 {duplicate_count}): {record['url']}")
                    elif record and record.get("exactMetricUnavailable"):
                        missing_count += 1
                        consecutive_unproductive = 0
                        print(
                            f"정확 지표 누락으로 저장 안 함: {record['url']} "
                            f"({record.get('exactMetricStatus')}: {record.get('exactMetricError')})",
                            file=sys.stderr,
                        )
                        await record_web_collection_failure(
                            str(record.get("exactMetricError", "Required Reel data was unavailable.")),
                            str(record.get("url", "")),
                        )
                    elif record:
                        last_reels_url = record["url"]
                        captured += 1
                        items_since_recycle += 1
                        consecutive_unproductive = 0
                        record_web_collection_success()
                        print(f"[PYTHON] {stored_reel_progress_line(captured, options.max_items, record['url'], progress_offset=options.progress_offset)}")
                    else:
                        missing_count += 1
                        consecutive_unproductive += 1
                        print(f"현재 화면에서 릴스 URL 추출 실패 ({consecutive_unproductive}/{REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD})", file=sys.stderr)
                        await record_web_collection_failure("No Reel record could be extracted.")
                    if record and record.get("url"):
                        last_reels_url = record["url"]
                    consecutive_recovery_failures = 0
                    await update_status({**progress_patch(record.get("url", "") if record else ""), "last_error": ""})
                    if stop_requested or (options.max_items != 0 and captured >= options.max_items):
                        break
                    if options.page_recycle_items > 0 and items_since_recycle >= options.page_recycle_items:
                        await recycle_page(f"{items_since_recycle} items")
                        continue
                    if options.page_recycle_items > 0 and views_since_recycle >= options.page_recycle_items * 4:
                        await recycle_page(f"{views_since_recycle} viewed Reels")
                        continue
                    if consecutive_unproductive >= REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD:
                        await recycle_page(f"{consecutive_unproductive} unproductive views")
                        continue
                    if not options.manual:
                        transition = await advance_to_next_reel(page, record.get("shortcode", "") if record else "", transition_timeout_milliseconds)
                        stall = next_transition_stall_state(consecutive_transition_stalls, transition["changed"])
                        consecutive_transition_stalls = stall["consecutive"]
                        if not transition["changed"]:
                            transition_stall_count += 1
                            print(f"다음 릴스 전환 지연 ({consecutive_transition_stalls}/{REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD})", file=sys.stderr)
                            if stall["shouldRecycle"]:
                                await recycle_page(f"{consecutive_transition_stalls} transition stalls")
                except CrawlerAccessError:
                    raise
                except Exception as error:
                    recovery_failure_count += 1
                    consecutive_recovery_failures += 1
                    print(f"일시적 수집 오류 자동 복구 ({consecutive_recovery_failures}/{REEL_MAX_CONSECUTIVE_RECOVERY_FAILURES}): {error}", file=sys.stderr)
                    await reel_store.flush()
                    await update_status({**progress_patch(record.get("url", "") if record else ""), "state": "recovering", "last_error": str(error)[:500]}, True)
                    if await record_web_collection_failure(str(error), str(record.get("url", "") if record else "")):
                        break
                    if consecutive_recovery_failures >= REEL_MAX_CONSECUTIVE_RECOVERY_FAILURES:
                        request_graceful_stop("웹 연속 수집 실패 한도")
                        break
                    await asyncio.sleep(min(5, 0.5 * (2 ** (consecutive_recovery_failures - 1))))
                    await recycle_page(f"transient error {consecutive_recovery_failures}")

        if android_pipeline is not None:
            print(f"Waiting for Android metric queue: {android_pipeline.backlog} Reel(s).")
            await android_pipeline.close()
        await reel_store.flush()
        if detached_android_metrics:
            merged_android = await asyncio.to_thread(apply_completed_android_metric_jobs, options.data_dir)
            await reel_store.merge_external_android_metrics()
            pending_android = sum(android_metric_queue_counts(options.data_dir).values())
            print(
                "Android metric worker continues independently: "
                f"pending={pending_android}, applied_now={merged_android['applied']}"
            )
        print("릴스 스냅샷 저장을 마치고 브라우저를 닫습니다.")
        # Keep the source context alive until a deferred Python follower
        # runtime has copied its logged-in storage state.
        await stop_if_shared_failure_limit_reached()
        follower_stats = {"success": 0, "unavailable": 0, "failed": 0, "stopStatus": "", "stopError": ""}
        merged = 0
        if follower_enricher is not None:
            if aborted_by_failure_limit:
                await follower_enricher.stop(
                    "collection_failure_limit",
                    "Reel collection stopped after five consecutive collection failures.",
                )
            elif options.followers_after_reels:
                await ensure_follower_runtime()
                queued = await follower_enricher.enqueue_all()
                print(f"Follower web lookups queued after Reel collection: {queued}")
            follower_stats = await follower_enricher.drain()
            print(f"Follower web: success={follower_stats['success']} unavailable={follower_stats['unavailable']} failed={follower_stats['failed']}")
            if follower_stats["stopStatus"]:
                print(f"Follower web lookup stopped ({follower_stats['stopStatus']}): {follower_stats['stopError']}", file=sys.stderr)
                exit_code = 2
            merged = await asyncio.to_thread(
                merge_follower_data_into_rows,
                reel_store.rows,
                ensure_user_history(options.data_dir),
            )
        else:
            print("Follower web: 별도 프로필 조회 없이 현재 Reel 페이지에서 확인된 정확값만 사용했습니다.")
        if merged:
            reel_store.dirty = True
            await reel_store.flush()
        if detached_android_metrics:
            await asyncio.to_thread(apply_completed_android_metric_jobs, options.data_dir)
            await reel_store.merge_external_android_metrics()
        if follower_enricher is not None:
            users_xlsx = await asyncio.to_thread(write_users_xlsx, options.data_dir)
            print(f"사용자 정보 저장 완료: {users_xlsx}")
        print(f"Follower data merged into {csv_path.name}: {merged}")
        output_paths = await reel_store.export_outputs()
        await safe_close(follower_runtime.browser if follower_runtime else None)
        follower_runtime = None
        await safe_close(view_page)
        view_page = None
        if owns_collection_context:
            await safe_close(context)
            context = None
        else:
            await safe_close(page)
            page = None
        print(
            "저장 완료: "
            + ", ".join(f"{kind.upper()}={path}" for kind, path in output_paths.items())
        )
        await status_reporter.finish(
            "completed_with_errors" if follower_stats["stopStatus"] else ("stopped" if stop_requested else "completed"),
            {**progress_patch(), "follower_success": follower_stats["success"], "follower_failed": follower_stats["failed"], "follower_unavailable": follower_stats["unavailable"]},
        )
        return exit_code
    except Exception as error:
        if isinstance(error, CrawlerAccessError) and error.code == "rate_limited":
            retry_hint = (
                f" Retry-After={rate_limit_state.retry_after_seconds:.0f}s."
                if rate_limit_state.retry_after_seconds is not None
                else ""
            )
            print(
                "[PYTHON] Instagram HTTP 429 rate limit: this collection batch was stopped."
                " Scheduled collection will wait for its next window before retrying."
                + retry_hint,
                file=sys.stderr,
            )
            try:
                await asyncio.to_thread(
                    append_collection_log,
                    options.data_dir,
                    "python",
                    "rate_limited",
                    error=str(error),
                    retry_after_seconds=rate_limit_state.retry_after_seconds,
                )
            except Exception:
                pass
        if android_pipeline is not None:
            try:
                await android_pipeline.close()
            except Exception:
                pass
        if reel_store:
            try:
                await reel_store.flush()
                await reel_store.export_outputs()
            except Exception:
                pass
        if status_reporter:
            try:
                await status_reporter.finish("failed", {"last_error": str(error)[:500], "failure_code": getattr(error, "code", "collector_error")})
            except Exception:
                pass
        raise
    finally:
        if stop_watcher is not None:
            stop_watcher.cancel()
            await asyncio.gather(stop_watcher, return_exceptions=True)
        if owns_collection_context:
            await safe_close(view_page)
            await safe_close(context)
            await safe_close(browser)
        else:
            await safe_close(page)
            await safe_close(view_page)
        await safe_close(follower_runtime.browser if follower_runtime else None)
        if owns_collection_context and playwright_runtime:
            try:
                await playwright_runtime.stop()
            except Exception:
                pass
        if collector_lock:
            await collector_lock.release()
        if register_signal_handler:
            signal.signal(signal.SIGINT, previous_sigint)


async def run_collectors_in_shared_context(
    options_list: Sequence[argparse.Namespace],
    *,
    external_stop_event: asyncio.Event | None = None,
) -> list[int]:
    """Run independent output datasets concurrently in one logged-in browser context.

    A persistent Chromium profile can only be opened by one browser process.  Each
    collector therefore receives its own Reel and follower pages while sharing the
    single persistent context and its already-authenticated session.
    """
    if not options_list:
        return []
    first = options_list[0]
    if any(option.profile_dir != first.profile_dir for option in options_list):
        raise ValueError("Shared collection requires the same browser profile directory.")
    if any(bool(option.no_login) != bool(first.no_login) for option in options_list):
        raise ValueError("Shared collection requires matching login modes.")

    executable_path = locate_browser_executable()
    async_playwright = load_playwright()
    playwright_runtime = await async_playwright().start()
    browser: Any = None
    context: Any = None
    try:
        browser, context = await launch_collection_context(
            playwright_runtime.chromium,
            executable_path,
            first,
        )

        async def run_one(index: int, options: argparse.Namespace) -> int:
            try:
                return await run_collector(
                    options,
                    external_stop_event=external_stop_event,
                    register_signal_handler=False,
                    shared_context=context,
                    shared_browser=browser,
                    shared_playwright_runtime=playwright_runtime,
                    enable_stop_input=index == 0,
                )
            except Exception as error:
                print(f"공유 브라우저 수집 실패 ({options.data_dir.name}): {error}", file=sys.stderr)
                return 2

        return list(await asyncio.gather(*(run_one(index, options) for index, options in enumerate(options_list))))
    finally:
        await safe_close(context)
        await safe_close(browser)
        try:
            await playwright_runtime.stop()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    try:
        return asyncio.run(run_collector(options))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
