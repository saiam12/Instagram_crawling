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
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, urljoin, urlparse
from xml.etree import ElementTree


PYTHON_VERSION_ROOT = Path(__file__).resolve().parent.parent

if __package__:
    from .instagram_follower_enricher import (
        FollowerEnricher,
        ensure_user_history,
        read_csv_objects,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
    "days_since_upload",
    "view_count",
    "like_count",
    "comment_count",
    "repost_count",
    "follower_count",
]
RECOLLECT_FIELDS = [
    "collected_at",
    "days_since_upload",
    "view_count",
    "like_count",
    "comment_count",
    "repost_count",
    "follower_count",
]
# Keep refreshes ordered without exposing labels such as "Initial" and
# "2nd collect" in the row-oriented output files.
ROW_COLLECTION_FIELDS = ["collection_number", "days_since_previous", *CSV_FIELDS]
REEL_HISTORY_DIRECTORY = ".collector"
REEL_HISTORY_FILENAME = "reels_history_active.csv"
LEGACY_REEL_HISTORY_FILENAMES = ("reels_history.csv",)
PUBLIC_REELS_STEM = "reels"
LEGACY_REEL_DROPPED_FIELDS = {
    "collection_label",
    "reaction_rate",
    "follower_count_collected_at",
    "follower_lookup_status",
}
REEL_SUCCESS_INTERVAL_SECONDS = 0.5
FOLLOWER_SUCCESS_INTERVAL_SECONDS = 0.3
HASHTAG_REDISCOVERY_INTERVAL_SECONDS = 60
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
REEL_MAX_CONSECUTIVE_RECOVERY_FAILURES = 6
REEL_STATUS_WRITE_INTERVAL_SECONDS = 60
DIRECT_REEL_CONCURRENCY = 1
DIRECT_REEL_SETTLE_MILLISECONDS = 250
DIRECT_REEL_METADATA_TIMEOUT_MILLISECONDS = 700
DIRECT_REEL_INFO_TIMEOUT_SECONDS = 5.0
EXACT_REEL_DETAIL_SETTLE_MILLISECONDS = 1_000
EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS = 3_000
EXACT_REEL_DETAIL_POLL_MILLISECONDS = 150
EXACT_REEL_DETAIL_PAGE_ATTEMPTS = 3
EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS = 1_500
ANONYMOUS_FOLLOWER_MAX_ATTEMPTS = 3
ANONYMOUS_FOLLOWER_RETRY_SECONDS = 1.0
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
INSTAGRAM_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]{1,30}$")


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
    query = quote(f"#{hashtag}", safe="")
    return f"https://www.instagram.com/explore/search/keyword/?q={query}"


def normalize_reel_url(value: Any) -> dict[str, str] | None:
    parsed = urlparse(urljoin("https://www.instagram.com/", str(value or "")))
    match = re.match(r"^/reels?/([A-Za-z0-9_-]+)/?", parsed.path, re.I)
    if not match:
        return None
    return {"url": f"https://www.instagram.com/reels/{match.group(1)}/", "shortcode": match.group(1)}


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
    return all(
        exact_nonnegative_integer(candidate.get(field)) is not None
        for field in ["likeCount", "commentCount", "repostCount"]
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
    follower_count = (
        exact_nonnegative_integer(direct_user.get("follower_count"))
        if include_follower_count
        else None
    )
    repost_count = media.get("media_repost_count") if media.get("media_repost_count") is not None else media.get("repost_count")
    return {
        "userId": str(user.get("pk") or user.get("pk_id") or user.get("id") or "").strip(),
        "username": str(user.get("username") or "").strip(),
        "caption": str(caption.get("text") or media.get("caption_text") or "").strip(),
        "audioName": format_audio_name(asset.get("display_artist") or asset.get("artist_name") or music_artist.get("username"), asset.get("title") or asset.get("song_name"))
        or format_audio_name(original_artist.get("username") or user.get("username"), original.get("original_audio_title") or original.get("audio_name") or original.get("title")),
        "locationName": str(location.get("name") or location.get("short_name") or media.get("location_name") or "").strip(),
        "ad": media_is_advertisement(media),
        "uploadedAt": next((normalized for item in upload_candidates if (normalized := normalize_upload_time(item))), ""),
        # The Reel permalink feed omits views, while the creator's Reels
        # connection exposes the exact integer as play_count.
        "viewCount": play_count,
        "viewSourceField": "play_count" if play_count is not None else None,
        "followerCount": follower_count,
        "followerSourceField": "follower_count" if follower_count is not None else None,
        "likeCount": exact_nonnegative_integer(media.get("like_count")),
        "commentCount": exact_nonnegative_integer(media.get("comment_count")),
        "repostCount": exact_nonnegative_integer(repost_count),
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
    for field in ["userId", "username", "caption", "audioName", "locationName", "ad", "uploadedAt", "viewCount", "viewSourceField", "followerCount", "followerSourceField", "likeCount", "commentCount", "repostCount", "isReel"]:
        if field in {"ad", "isReel"}:
            merged[field] = bool(existing.get(field) or observed.get(field))
        elif field in metric_fields:
            merged[field] = existing.get(field) if exact_nonnegative_integer(existing.get(field)) is not None else observed.get(field)
        elif field == "viewSourceField":
            merged[field] = existing.get(field) if exact_nonnegative_integer(existing.get("viewCount")) is not None else observed.get(field)
        elif field == "followerSourceField":
            merged[field] = existing.get(field) if exact_nonnegative_integer(existing.get("followerCount")) is not None else observed.get(field)
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
    return merged


def exact_view_counts_from_metadata(shortcode: str, metadata: dict[str, Any] | None) -> dict[str, int]:
    candidate = metadata or {}
    count = exact_nonnegative_integer(candidate.get("viewCount"))
    if not shortcode or count is None or candidate.get("viewSourceField") != "play_count":
        return {}
    return {shortcode: count}


def direct_reel_info_diagnostic_message(diagnostic: dict[str, str] | None, missing_field: str) -> str:
    result = diagnostic or {}
    if result.get("status") == "success":
        return f"Direct Reel info returned HTTP 200 but did not include an exact raw {missing_field}."
    return str(result.get("error") or "").strip()


def collect_reel_metadata(value: Any, destination: dict[str, dict[str, Any]], depth: int = 0) -> None:
    if not isinstance(value, (dict, list)) or depth > 16:
        return
    if isinstance(value, dict):
        shortcode = str(value.get("code") or value.get("shortcode") or value.get("media_code") or "")
        if re.fullmatch(r"[A-Za-z0-9_-]+", shortcode):
            merged = merge_reel_metadata(destination.get(shortcode), metadata_from_media(value))
            if any(merged.values()):
                destination[shortcode] = merged
        children = value.values()
    else:
        children = value
    for child in children:
        if isinstance(child, (dict, list)):
            collect_reel_metadata(child, destination, depth + 1)


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
        "days_since_upload": days_since_upload(uploaded_at, timestamp),
        "view_count": view_count,
        "like_count": exact_nonnegative_integer(response.get("likeCount")),
        "comment_count": exact_nonnegative_integer(response.get("commentCount")),
        "repost_count": exact_nonnegative_integer(response.get("repostCount")),
        "follower_count": "",
    }


def build_anonymous_refresh_record(existing: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    """Keep prior static Reel data while replacing only freshly observed public metrics."""
    refreshed = {field: existing.get(field, "") for field in CSV_FIELDS}
    # Keep the stored identity: history/cooldown/export grouping intentionally
    # uses the raw URL column, while the history lookup accepts legacy URL forms.
    refreshed["url"] = refreshed["url"] or observed.get("url")
    refreshed["collected_at"] = observed.get("collected_at") or refreshed["collected_at"]
    for field in ["view_count", "like_count", "comment_count", "repost_count"]:
        refreshed[field] = exact_nonnegative_integer(observed.get(field))
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
        and follower_result.get("sourceField") in {"follower_count", "edge_followed_by.count"}
        and follower_count is not None
        else ""
    )
    return build_anonymous_refresh_from_history(rows, candidate)


def has_complete_reel_core_data(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    return all(str(record.get(field, "")).strip() for field in ["url", "user_id", "username", "uploaded_at"]) and all(exact_nonnegative_integer(record.get(field)) is not None for field in ["view_count", "like_count", "comment_count", "repost_count", "follower_count"]) and str(record.get("ad", "")).lower() in {"true", "false"}


def apply_exact_metric_results(
    record: dict[str, Any],
    shortcode: str,
    view_counts: dict[str, int],
    follower_result: dict[str, Any],
) -> dict[str, str]:
    for field in ["like_count", "comment_count", "repost_count"]:
        if exact_nonnegative_integer(record.get(field)) is None:
            return {"status": f"exact_{field}_unavailable", "error": f"Exact Network integer was unavailable for {field}."}
    view_count = exact_nonnegative_integer(view_counts.get(shortcode))
    if view_count is None:
        return {"status": "exact_view_unavailable", "error": "Exact play_count was unavailable for the target Reel."}
    follower_count = exact_nonnegative_integer(follower_result.get("followerCount"))
    if (
        follower_result.get("status") != "success"
        or follower_result.get("sourceField") not in {"follower_count", "edge_followed_by.count"}
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


def stored_reel_progress_line(stored_count: int, max_items: int, url: str) -> str:
    """Format progress for Reels that were actually appended to the store."""
    target = str(max_items) if max_items else "전체"
    return f"[{stored_count}/{target}] {url}"


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
    if base_field in {"collection_number", "view_count", "like_count", "comment_count", "repost_count", "follower_count"}:
        text = str(value).strip().replace(",", "")
        return int(text) if re.fullmatch(r"-?\d+", text) else value
    if base_field in {"days_since_previous", "days_since_upload"}:
        text = str(value).strip()
        return float(text) if re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)", text) else value
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
        fields = ["user_id", "username", "biography", "follower_count", "collected_at"]
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
    wide_fields, wide_rows = long_rows_to_wide(rows)
    public_fields, public_rows = project_public_records("reels", wide_rows, wide_fields)
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
            layout="columns",
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
    if base_field in {"view_count", "like_count", "comment_count", "repost_count", "follower_count"}:
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
    ) -> None:
        self.csv_path = csv_path
        self.flush_record_count = flush_record_count
        self.xlsx_layout = xlsx_layout
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
    ) -> "LongReelStore":
        store = cls(Path(csv_path), flush_record_count, xlsx_layout)
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
                store.rows = [{field: row.get(field, "") for field in store.fields} for row in legacy_rows]
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
                store.rows = [{field: row.get(field, "") for field in store.fields} for row in rows]
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
            cooldown = long_collected_record_cooldown(self.rows, record)
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

    async def _flush_locked(self, force: bool) -> bool:
        if not self.dirty or (not force and self.pending_record_count < self.flush_record_count):
            return False
        saved = self.pending_record_count
        await asyncio.to_thread(write_csv_records, self.csv_path, self.rows, self.fields)
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
            return await asyncio.to_thread(
                write_long_output_bundle,
                self.csv_path,
                self.rows,
                self.fields,
                xlsx_layout=self.xlsx_layout,
            )

    def stats(self) -> dict[str, Any]:
        return {"rows": len(self.rows), "pending": self.pending_record_count, "journalPath": str(self.journal_path)}


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
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--direct-reel-info-wait-seconds", type=float, default=2)
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
    parser.add_argument("--urls-file", type=Path)
    parser.add_argument("--data-dir", type=Path, default=PYTHON_VERSION_ROOT / "data_web")
    parser.add_argument("--profile-dir", type=Path, default=PYTHON_VERSION_ROOT / ".instagram_browser_profile")
    parser.set_defaults(storage_layout="history")
    parser.add_argument("--output-stem", default="")
    parser.add_argument("--xlsx-layout", choices=["rows", "columns", "both"], default="columns", help=argparse.SUPPRESS)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    options = build_parser().parse_args(argv)
    options.data_dir = options.data_dir.resolve()
    options.profile_dir = options.profile_dir.resolve()
    options.urls_file = options.urls_file.resolve() if options.urls_file else None
    if not options.output_stem:
        options.output_stem = PUBLIC_REELS_STEM
    if not re.fullmatch(r"[A-Za-z0-9_-]+", options.output_stem):
        build_parser().error("--output-stem may contain only letters, numbers, underscores, and hyphens.")
    try:
        options.hashtags = parse_hashtag_query(options.hashtag_query)
    except ValueError as error:
        build_parser().error(str(error))
    if options.max_items < 0:
        build_parser().error("--max-items must be a non-negative integer.")
    if options.interval_seconds < 1:
        build_parser().error("--interval-seconds must be at least 1.")
    if not math.isfinite(options.direct_reel_info_wait_seconds) or options.direct_reel_info_wait_seconds < 0:
        build_parser().error("--direct-reel-info-wait-seconds must be 0 or greater.")
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


def _response_status(response: Any) -> int | None:
    status = getattr(response, "status", None)
    return status() if callable(status) else status


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


def attach_reel_metadata_collector(page: Any, reel_metadata: dict[str, dict[str, Any]]) -> None:
    async def collect_response(response: Any) -> None:
        try:
            parsed = urlparse(response.url)
            content_type = response.headers.get("content-type", "")
            if not parsed.hostname or not parsed.hostname.endswith("instagram.com") or "json" not in content_type:
                return
            collect_reel_metadata(json.loads(await response.text()), reel_metadata)
        except Exception:
            pass

    def on_response(response: Any) -> None:
        asyncio.create_task(collect_response(response))

    page.on("response", on_response)


async def collect_embedded_reel_metadata(page: Any, reel_metadata: dict[str, dict[str, Any]]) -> None:
    try:
        embedded = await page.locator('script[type="application/json"]').all_text_contents()
    except Exception:
        return
    for raw in embedded:
        try:
            collect_reel_metadata(json.loads(raw), reel_metadata)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass


async def create_collection_page(
    context: Any,
    url: str,
    reel_metadata: dict[str, dict[str, Any]],
    *,
    allow_login: bool = False,
    keep_open_on_navigation_failure: bool = False,
) -> Any:
    page = await context.new_page()
    attach_reel_metadata_collector(page, reel_metadata)
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


async def recycle_collection_page(context: Any, page: Any, url: str, reel_metadata: dict[str, dict[str, Any]]) -> Any:
    if page:
        try:
            await page.close()
        except Exception:
            pass
    reel_metadata.clear()
    return await create_collection_page(context, url, reel_metadata)


def hashtag_candidate_limit(max_items: int, hashtag_count: int) -> int:
    """Return zero for exhaustive discovery, otherwise keep discovery broad and balanced."""
    if max_items == 0:
        return 0
    balanced_target = math.ceil(max(1, max_items) / max(1, hashtag_count))
    return max(12, balanced_target * 4)


async def collect_hashtag_reel_urls(
    page: Any,
    hashtags: list[str],
    max_items: int,
    reel_metadata: dict[str, dict[str, Any]] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[str]:
    groups: list[list[str]] = []
    metadata = reel_metadata if reel_metadata is not None else {}
    per_hashtag_limit = hashtag_candidate_limit(max_items, len(hashtags))
    for hashtag_index, hashtag in enumerate(hashtags, start=1):
        if should_stop and should_stop():
            return []
        await page.goto(hashtag_page_url(hashtag), wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_000)
        if should_stop and should_stop():
            return []
        if re.search(r"/accounts/login", page.url, re.I):
            raise CrawlerAccessError("login_required", "Instagram login is required for hashtag collection.")
        if re.search(r"/(?:challenge|checkpoint)/", page.url, re.I):
            raise CrawlerAccessError("challenge_required", "Instagram requested an account check during hashtag collection.")
        urls: list[str] = []
        seen: set[str] = set()
        unchanged_attempts = 0
        for _ in range(30):
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
                      && rect.width >= 20 && rect.height >= 20
                      && Boolean(element.querySelector('img, video'));
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
            await collect_embedded_reel_metadata(page, metadata)
            if should_stop and should_stop():
                return []
            unchanged_attempts = unchanged_attempts + 1 if len(urls) == before else 0
            if unchanged_attempts >= 4 or (per_hashtag_limit and len(urls) >= per_hashtag_limit):
                break
            await page.mouse.wheel(0, 1_200)
            await page.wait_for_timeout(800)
        print(f"[Hashtag {hashtag_index}/{len(hashtags)}] #{hashtag} -> 릴스 후보 {len(urls)}개")
        groups.append(urls)
    combined: list[str] = []
    combined_seen: set[str] = set()
    maximum_candidates = max(max_items * 4, max_items) if max_items else None
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
  const metricFrom = candidates => {
    for (const control of candidates) {
      let node = control;
      for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
        const token = metricToken(textOf(node));
        if (token) return token;
      }
    }
    return '';
  };
  const viewLine = textOf(document.querySelector('main')).split(/\n+/).find(line =>
    /(?:조회수|views?|plays?)/i.test(line) && metricToken(line)
  ) || '';
  const skip = new Set(['about', 'accounts', 'direct', 'explore', 'reel', 'reels', 'stories']);
  const profiles = [...scope.querySelectorAll('a[href^="/"]')].filter(visible).map(element => ({
    element, parts: (element.getAttribute('href') || '').split('/').filter(Boolean)
  })).filter(item => item.parts.length === 1 && !skip.has(item.parts[0].toLowerCase()))
    .sort((a, b) => centerDistance(a.element, videoRect) - centerDistance(b.element, videoRect));
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
    audioName, locationName, ad, uploadedAt,
    viewText: metricFrom(viewControls) || metricToken(viewLine),
    likeText: metricFrom(likeControls), commentText: metricFrom(commentControls), repostText: metricFrom(repostControls)
  };
}"""


async def extract_visible_reel(page: Any) -> dict[str, Any] | None:
    browser_data = await page.evaluate(EXTRACT_VISIBLE_REEL_SCRIPT)
    normalized = normalize_reel_url(browser_data.get("currentUrl")) or normalize_reel_url(browser_data.get("activeHref"))
    return {**browser_data, **normalized} if normalized else None


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


async def wait_for_reel_metadata(shortcode: str, reel_metadata: dict[str, dict[str, Any]], timeout_milliseconds: int = 1_000) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_milliseconds / 1000
    while time.monotonic() < deadline:
        if shortcode in reel_metadata:
            return reel_metadata[shortcode]
        await asyncio.sleep(0.1)
    return reel_metadata.get(shortcode, {})


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
    def report(status: str, error: str = "") -> None:
        if diagnostic is not None:
            diagnostic.clear()
            diagnostic.update({"status": status, "error": error})

    target = str(shortcode or "").strip()
    media_id = shortcode_to_media_id(target)
    if not media_id:
        report("invalid_shortcode", "Direct Reel info could not derive a media ID from the shortcode.")
        return {}
    try:
        endpoint = await asyncio.wait_for(
            page.evaluate(
                """async mediaId => {
                  const controller = new AbortController();
                  const timeoutId = setTimeout(() => controller.abort(), 4000);
                  try {
                    const response = await fetch(`/api/v1/media/${encodeURIComponent(mediaId)}/info/`, {
                      credentials: 'include',
                      headers: {'X-IG-App-ID': '936619743392459', 'X-Requested-With': 'XMLHttpRequest'},
                      signal: controller.signal
                    });
                    return {status: response.status, text: await response.text()};
                  } finally {
                    clearTimeout(timeoutId);
                  }
                }""",
                media_id,
            ),
            timeout=DIRECT_REEL_INFO_TIMEOUT_SECONDS,
        )
        endpoint_status = int(endpoint.get("status") or 0)
        if endpoint_status != 200:
            report("http_error", f"Direct Reel info returned HTTP {endpoint_status}.")
            return {}
        try:
            payload = json.loads(str(endpoint.get("text") or ""))
        except json.JSONDecodeError:
            report("invalid_json", "Direct Reel info returned an invalid JSON response.")
            return {}
        items = payload.get("items") if isinstance(payload, dict) else None
        media = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        observed_shortcode = str(media.get("code") or media.get("shortcode") or "").strip()
        observed_media_id = str(media.get("pk") or media.get("id") or "").strip()
        if (observed_shortcode and observed_shortcode != target) or (
            not observed_shortcode and observed_media_id != media_id
        ):
            report("identity_mismatch", "Direct Reel info did not identify the requested Reel.")
            return {}
        report("success")
        return metadata_from_media(media, include_follower_count=True) if media else {}
    except asyncio.TimeoutError:
        report("timeout", f"Direct Reel info timed out after {DIRECT_REEL_INFO_TIMEOUT_SECONDS:g} seconds.")
        return {}
    except Exception as error:
        message = str(error).strip()
        if "abort" in message.casefold():
            report("timeout", "Direct Reel info was aborted after 4 seconds.")
        else:
            report("request_error", f"Direct Reel info request failed: {message[:300] or type(error).__name__}")
        return {}


async def read_reel_detail_metadata(
    page: Any,
    shortcode: str,
    existing_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wait for a Reel detail response to fill missing exact engagement integers."""
    target = str(shortcode or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", target):
        return dict(existing_metadata or {})
    metadata: dict[str, dict[str, Any]] = {target: dict(existing_metadata or {})}
    response_tasks: set[asyncio.Task[None]] = set()

    async def collect_response(response: Any) -> None:
        try:
            parsed = urlparse(response.url)
            content_type = response.headers.get("content-type", "")
            if not parsed.hostname or not parsed.hostname.endswith("instagram.com") or "json" not in content_type:
                return
            collect_reel_metadata(json.loads(await response.text()), metadata)
        except Exception:
            return

    def on_response(response: Any) -> None:
        task = asyncio.create_task(collect_response(response))
        response_tasks.add(task)
        task.add_done_callback(response_tasks.discard)

    def current_metadata() -> dict[str, Any]:
        return metadata.get(target, {})

    async def cancel_pending_response_tasks() -> None:
        pending_tasks = [task for task in response_tasks if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            done, _ = await asyncio.wait(pending_tasks, timeout=0.5)
            if done:
                await asyncio.gather(*done, return_exceptions=True)

    page.on("response", on_response)
    try:
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
                await cancel_pending_response_tasks()
                await page.wait_for_timeout(EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS * (page_attempt + 1))
                continue
            if _response_status(response) == 429 or re.search(r"/(?:accounts/login|challenge|checkpoint)/", page.url, re.I):
                return current_metadata()
            await page.wait_for_timeout(EXACT_REEL_DETAIL_SETTLE_MILLISECONDS)
            deadline = time.monotonic() + EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS / 1000
            while time.monotonic() < deadline:
                candidate = current_metadata()
                if has_exact_engagement_metadata(candidate):
                    return candidate
                await page.wait_for_timeout(EXACT_REEL_DETAIL_POLL_MILLISECONDS)
            if page_attempt + 1 < EXACT_REEL_DETAIL_PAGE_ATTEMPTS:
                await cancel_pending_response_tasks()
                await page.wait_for_timeout(EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS * (page_attempt + 1))
        return current_metadata()
    except asyncio.CancelledError:
        raise
    except Exception:
        return current_metadata()
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        await cancel_pending_response_tasks()


async def read_profile_reel_view_counts(page: Any, username: str, shortcodes: set[str]) -> dict[str, int]:
    """Read exact play_count integers from the creator Reels GraphQL response."""
    normalized_username = str(username or "").strip().lstrip("@")
    targets = {str(shortcode).strip() for shortcode in shortcodes if str(shortcode).strip()}
    if not INSTAGRAM_USERNAME_PATTERN.fullmatch(normalized_username) or not targets:
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    response_tasks: set[asyncio.Task[None]] = set()

    async def collect_response(response: Any) -> None:
        try:
            parsed = urlparse(response.url)
            content_type = response.headers.get("content-type", "")
            if not parsed.hostname or not parsed.hostname.endswith("instagram.com") or "json" not in content_type:
                return
            collect_reel_metadata(json.loads(await response.text()), metadata)
        except Exception:
            return

    def on_response(response: Any) -> None:
        task = asyncio.create_task(collect_response(response))
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

    async def cancel_pending_response_tasks() -> None:
        pending_tasks = [task for task in response_tasks if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            done, _ = await asyncio.wait(pending_tasks, timeout=0.5)
            if done:
                await asyncio.gather(*done, return_exceptions=True)

    page.on("response", on_response)
    try:
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
                await cancel_pending_response_tasks()
                await page.wait_for_timeout(PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS)
                continue
            if _response_status(response) == 429 or re.search(r"/(?:accounts/login|challenge|checkpoint)/", page.url, re.I):
                return {}
            await page.wait_for_timeout(PROFILE_REEL_VIEW_SETTLE_MILLISECONDS)
            for attempt in range(PROFILE_REEL_VIEW_SCROLL_ATTEMPTS):
                found = exact_target_counts()
                if len(found) == len(targets):
                    return found
                if attempt + 1 < PROFILE_REEL_VIEW_SCROLL_ATTEMPTS:
                    await page.mouse.wheel(0, 1_500)
                    await page.wait_for_timeout(PROFILE_REEL_VIEW_SCROLL_MILLISECONDS)
            found = exact_target_counts()
            if len(found) == len(targets):
                return found
            if page_attempt + 1 < PROFILE_REEL_VIEW_PAGE_ATTEMPTS:
                await cancel_pending_response_tasks()
                await page.wait_for_timeout(PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS)
        return found
    except asyncio.CancelledError:
        raise
    except Exception:
        return {}
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        await cancel_pending_response_tasks()


async def resolve_exact_reel_metrics(
    active_page: Any,
    shortcode: str,
    existing_metadata: dict[str, Any] | None,
    fallback_page_factory: Callable[[], Any],
    *,
    direct_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    observed_direct = (
        await request_reel_info_metadata(active_page, shortcode)
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
        if fallback_page is None:
            fallback_page = await fallback_page_factory()
        return fallback_page

    if not has_exact_engagement_metadata(metadata):
        metadata = await read_reel_detail_metadata(
            await get_fallback_page(),
            shortcode,
            metadata,
        )
    view_counts = exact_view_counts_from_metadata(shortcode, metadata)
    username = str(metadata.get("username") or "").strip().lstrip("@")
    if not view_counts and INSTAGRAM_USERNAME_PATTERN.fullmatch(username):
        view_counts = await read_profile_reel_view_counts(
            await get_fallback_page(),
            username,
            {shortcode},
        )
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
    normalized = str(username or "").strip().lstrip("@")
    if not INSTAGRAM_USERNAME_PATTERN.fullmatch(normalized):
        return {"status": "profile_unavailable", "error": "Invalid Instagram username.", "source": "instagram_web"}
    try:
        response = await page.goto(f"https://www.instagram.com/{quote(normalized, safe='')}/", wait_until="domcontentloaded", timeout=30_000)
        if _response_status(response) == 429:
            return {"status": "rate_limited", "error": "Instagram returned HTTP 429.", "source": "instagram_web"}
        await page.wait_for_timeout(FOLLOWER_PROFILE_SETTLE_MILLISECONDS)
        if re.search(r"/accounts/login", page.url, re.I):
            return {"status": "login_required", "error": "Instagram login is required.", "source": "instagram_web"}
        if re.search(r"/(?:challenge|checkpoint)/", page.url, re.I):
            return {"status": "challenge_required", "error": "Instagram requested an account check.", "source": "instagram_web"}
        endpoint = await page.evaluate(
            """async expected => {
              const response = await fetch(`/api/v1/users/web_profile_info/?username=${encodeURIComponent(expected)}`, {
                credentials: 'include',
                headers: {'X-IG-App-ID': '936619743392459', 'X-Requested-With': 'XMLHttpRequest'}
              });
              return {status: response.status, text: await response.text()};
            }""",
            normalized,
        )
        endpoint_status = int(endpoint.get("status") or 0)
        if endpoint_status == 429:
            return {"status": "rate_limited", "error": "Instagram returned HTTP 429 for web_profile_info.", "source": "instagram_web"}
        if endpoint_status in {401, 403}:
            return {"status": "login_required", "error": "Instagram login is required for web_profile_info.", "source": "instagram_web"}
        if endpoint_status == 404:
            return {"status": "profile_unavailable", "error": "Instagram profile is unavailable.", "source": "instagram_web"}
        if endpoint_status != 200:
            return {"status": "web_error", "error": f"Instagram web_profile_info returned HTTP {endpoint_status}.", "source": "instagram_web"}
        payload = json.loads(str(endpoint.get("text") or ""))
        user = _dict(_dict(payload.get("data")).get("user"))
        response_username = str(user.get("username") or "").strip().lstrip("@")
        if response_username.casefold() != normalized.casefold():
            return {"status": "web_error", "error": "Instagram web_profile_info returned a different username.", "source": "instagram_web"}
        biography = str(user.get("biography") or "")
        count = exact_nonnegative_integer(_dict(user.get("edge_followed_by")).get("count"))
        if count is None:
            return {
                "status": "web_unavailable",
                "error": "Exact follower count was absent from web_profile_info.",
                "source": "instagram_web",
                "biography": biography,
            }
        return {**follower_count_success(count, "edge_followed_by.count"), "biography": biography}
    except Exception as error:
        return {"status": "web_error", "error": str(error)[:500], "source": "instagram_web"}


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
    def __init__(self, page: Any, interval_seconds: float = 8) -> None:
        self.page = page
        self.context = page.context
        self.interval_seconds = interval_seconds
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
            result = await request_web_follower_count(self.page, payload["username"])
            if result["status"] == "web_error":
                try:
                    await self._replace_page()
                    result = await request_web_follower_count(self.page, payload["username"])
                except Exception as error:
                    result = {"status": "web_error", "error": f"Follower page retry failed: {str(error)[:400]}", "source": "instagram_web"}
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
    for restored_page in list(context.pages):
        await safe_close(restored_page)
    return None, context


def initial_collection_page_url(start_url: str, *, background: bool, no_login: bool) -> str:
    """Keep every collection mode on its requested Instagram surface."""
    del background, no_login
    return start_url


async def run_collector(options: argparse.Namespace) -> int:
    stop_requested = False
    stop_event = asyncio.Event()
    event_loop = asyncio.get_running_loop()
    interrupt_count = 0
    status_reporter: CollectorStatusReporter | None = None
    reel_store: LongReelStore | None = None
    collector_lock: CollectorLock | None = None
    browser: Any = None
    context: Any = None
    follower_runtime: FollowerRuntime | None = None
    view_runtime: FollowerRuntime | None = None
    playwright_runtime: Any = None
    follower_enricher: FollowerEnricher | None = None
    exit_code = 0

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

    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_interrupt)
    if options.background and sys.stdin.isatty():
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
        status_reporter = CollectorStatusReporter(options.data_dir, options)

        async def update_status(patch: dict[str, Any], force: bool = False) -> None:
            try:
                await status_reporter.update(patch, force)
            except Exception as error:
                print(f"상태 파일 저장 실패: {error}", file=sys.stderr)

        await update_status({"state": "followers" if options.followers_only else "collecting"}, True)
        csv_path = options.data_dir / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME
        refresh_urls = load_reel_urls(options.urls_file) if not options.followers_only and options.urls_file else []
        anonymous_refresh = bool(options.no_login and refresh_urls)
        start_url = refresh_urls[0] if refresh_urls else (hashtag_page_url(options.hashtags[0]) if options.hashtags else options.start_url)
        if not options.followers_only:
            reel_store = await LongReelStore.create(
                csv_path,
                options.checkpoint_items,
                options.xlsx_layout,
            )
        executable_path = locate_browser_executable()
        async_playwright = load_playwright()
        playwright_runtime = await async_playwright().start()
        chromium = playwright_runtime.chromium
        browser, context = await launch_collection_context(chromium, executable_path, options)

        async def ensure_follower_runtime() -> SequentialWebFollowerLookup:
            nonlocal follower_runtime
            if follower_runtime is None:
                follower_runtime = await create_background_follower_runtime(chromium, context, executable_path)
            lookup = SequentialWebFollowerLookup(follower_runtime.page, options.follower_interval_seconds)
            if follower_enricher is not None:
                follower_enricher.set_lookup_impl(lookup)
            return lookup

        async def ensure_view_runtime() -> FollowerRuntime:
            nonlocal view_runtime
            if view_runtime is None:
                view_runtime = await create_background_follower_runtime(chromium, context, executable_path)
            return view_runtime

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
                print(f"[Follower {progress['completed']}/{progress['queued']}] @{progress['username']} -> {outcome}")
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

        seen: set[str] = set()
        exact_follower_cache: dict[str, dict[str, Any]] = {}
        reel_metadata: dict[str, dict[str, Any]] = {}
        initial_page_url = initial_collection_page_url(
            start_url,
            background=options.background,
            no_login=options.no_login,
        )
        try:
            page = await create_collection_page(
                context,
                initial_page_url,
                reel_metadata,
                allow_login=not options.background and not options.no_login,
                keep_open_on_navigation_failure=not options.background and not options.no_login,
            )
        except CrawlerAccessError as error:
            if options.no_login and error.code == "login_required":
                raise CrawlerAccessError(
                    "anonymous_access_blocked",
                    "Instagram requires login for this request. Anonymous collection can only continue for public Reel pages that Instagram exposes without login.",
                ) from error
            raise
        if options.no_login:
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
            print("저장된 Instagram 브라우저 프로필로 백그라운드 모드를 시작했습니다.")
        else:
            print("브라우저에서 Instagram에 로그인하고 릴스 화면을 연 뒤 이 창으로 돌아오세요.")
            await async_input("준비가 끝났으면 Enter를 누르세요: ")
            assert_instagram_page_access(page)
            expected_surface = is_instagram_hashtag_surface(page.url) if options.hashtags else is_instagram_reels_surface(page.url)
            if not expected_surface and not refresh_urls:
                await navigate_with_retries(page, start_url)
        if anonymous_refresh:
            print("무로그인 재수집: 기존 정적 정보는 보존하고, 새 공개 지표만 추가합니다.")
        else:
            follower_enricher = await start_follower_enricher()

        captured = duplicate_count = missing_count = filtered_count = 0
        cooldown_skipped_count = page_recycle_count = transition_stall_count = recovery_failure_count = 0
        collected_shortcodes: set[str] = set()
        next_delay_seconds = options.interval_seconds

        def progress_patch(last_reel_url: str = "") -> dict[str, Any]:
            patch = {
                "captured": captured, "duplicates": duplicate_count, "missing": missing_count,
                "filtered": filtered_count, "cooldown_skipped": cooldown_skipped_count,
                "page_recycles": page_recycle_count, "transition_stalls": transition_stall_count,
                "recovery_failures": recovery_failure_count,
            }
            if last_reel_url:
                patch["last_reel_url"] = last_reel_url
            return patch

        async def capture_current_reel(
            target_page: Any = None,
            target_metadata: dict[str, dict[str, Any]] | None = None,
            metadata_timeout_milliseconds: int = 1_000,
        ) -> dict[str, Any] | None:
            nonlocal next_delay_seconds
            active_page = target_page or page
            metadata = target_metadata if target_metadata is not None else reel_metadata
            next_delay_seconds = options.interval_seconds
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
            response_metadata = await wait_for_reel_metadata(record["shortcode"], metadata, metadata_timeout_milliseconds)
            if not response_metadata.get("userId") or not response_metadata.get("username"):
                await collect_embedded_reel_metadata(active_page, metadata)
                response_metadata = metadata.get(record["shortcode"], response_metadata)
            direct_reel_info_diagnostic: dict[str, str] = {}
            direct_metadata = await request_initial_reel_info_metadata(
                active_page,
                record["shortcode"],
                direct_reel_info_diagnostic,
                settle_milliseconds=round(options.direct_reel_info_wait_seconds * 1_000),
            )
            response_metadata = merge_direct_reel_metadata(response_metadata, direct_metadata)
            collected = build_collected_record(record, response_metadata)
            metadata.pop(record["shortcode"], None)
            if options.max_upload_age_days > 0 and not parse_datetime(collected.get("uploaded_at")):
                seen.add(record["shortcode"])
                return {**record, "uploadDateUnavailable": True}
            if not is_within_upload_age_days(collected, options.max_upload_age_days):
                seen.add(record["shortcode"])
                return {**record, "uploadAgeFilteredOut": True, "uploadAgeDays": collected["days_since_upload"]}

            async def exact_fallback_page() -> Any:
                return (await ensure_view_runtime()).page

            response_metadata, exact_views = await resolve_exact_reel_metrics(
                active_page,
                record["shortcode"],
                response_metadata,
                exact_fallback_page,
                direct_metadata=direct_metadata,
            )
            collected = build_collected_record(record, response_metadata)
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

                async def anonymous_profile_lookup() -> dict[str, Any]:
                    return (
                        await request_anonymous_follower_count(await exact_fallback_page(), username)
                        if INSTAGRAM_USERNAME_PATTERN.fullmatch(username)
                        else {"status": "profile_unavailable", "error": "Previous Reel history has no valid username.", "source": "instagram_web"}
                    )

                follower_result = await resolve_follower_result(
                    direct_metadata,
                    follower_key,
                    exact_follower_cache,
                    anonymous_profile_lookup,
                )
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
                seen.add(record["shortcode"])
                public_metric_fields = ["view_count", "like_count", "comment_count", "repost_count"]
                return {
                    **record,
                    "snapshotLabel": stored["label"],
                    "cooldownSkipped": bool(stored.get("skipped")),
                    "cooldownLabel": stored.get("cooldownLabel", ""),
                    "nextCollectionAt": stored.get("nextCollectionAt", ""),
                    "collectionComplete": all(exact_nonnegative_integer(anonymous_collected.get(field)) is not None for field in public_metric_fields),
                }
            missing_engagement_field = next(
                (
                    field
                    for field in ["like_count", "comment_count", "repost_count"]
                    if exact_nonnegative_integer(collected.get(field)) is None
                ),
                "",
            )
            if missing_engagement_field:
                seen.add(record["shortcode"])
                direct_message = direct_reel_info_diagnostic_message(
                    direct_reel_info_diagnostic,
                    missing_engagement_field,
                )
                return {
                    **record,
                    "exactMetricUnavailable": True,
                    "exactMetricStatus": f"exact_{missing_engagement_field}_unavailable",
                    "exactMetricError": " ".join(part for part in [
                        f"Exact Network integer was unavailable for {missing_engagement_field} after the Reel detail response wait.",
                        direct_message,
                    ] if part),
                }
            follower_payload = {"user_id": str(collected["user_id"]), "username": str(collected["username"]), "seen_at": str(collected["collected_at"])}
            follower_key = follower_payload["user_id"] or follower_payload["username"].casefold()

            async def profile_lookup() -> dict[str, Any]:
                await ensure_follower_runtime()
                if follower_enricher is None:
                    raise RuntimeError("Follower enricher is unavailable.")
                return await follower_enricher.lookup_user_now(**follower_payload)

            follower_result = await resolve_follower_result(
                direct_metadata,
                follower_key,
                exact_follower_cache,
                profile_lookup,
            )
            exact_result = apply_exact_metric_results(
                collected,
                record["shortcode"],
                exact_views,
                follower_result,
            )
            if exact_result["status"] != "success":
                seen.add(record["shortcode"])
                direct_message = (
                    direct_reel_info_diagnostic_message(direct_reel_info_diagnostic, "view_count")
                    if exact_result["status"] == "exact_view_unavailable"
                    else ""
                )
                return {
                    **record,
                    "exactMetricUnavailable": True,
                    "exactMetricStatus": exact_result["status"],
                    "exactMetricError": " ".join(part for part in [exact_result["error"], direct_message] if part),
                }
            if not has_complete_reel_core_data(collected):
                seen.add(record["shortcode"])
                return {
                    **record,
                    "exactMetricUnavailable": True,
                    "exactMetricStatus": "exact_core_data_unavailable",
                    "exactMetricError": "Required exact Network fields or Reel identity fields were unavailable.",
                }
            if follower_enricher is not None:
                await follower_enricher.track_user(**follower_payload)
            next_delay_seconds = REEL_SUCCESS_INTERVAL_SECONDS
            stored = await reel_store.append(collected)
            if not stored.get("skipped"):
                collected_shortcodes.add(record["shortcode"])
            seen.add(record["shortcode"])
            return {
                **record,
                "snapshotLabel": stored["label"],
                "cooldownSkipped": bool(stored.get("skipped")),
                "cooldownLabel": stored.get("cooldownLabel", ""),
                "nextCollectionAt": stored.get("nextCollectionAt", ""),
                "collectionComplete": has_complete_reel_core_data(collected),
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
            elif record and record.get("cooldownSkipped"):
                cooldown_skipped_count += 1
                print(f"{record.get('cooldownLabel') or '재수집 간격'} 이내 중복 건너뜀: {record['url']}")
            elif record:
                captured += 1
                print(stored_reel_progress_line(captured, options.max_items, record["url"]))
            else:
                missing_count += 1
                print(f"수집 실패: {url}", file=sys.stderr)
            await update_status(progress_patch(record.get("url", "") if record else ""))

        async def report_direct_error(index: int, url: str, error: Exception) -> None:
            nonlocal missing_count
            missing_count += 1
            print(f"수집 실패: {url} ({error})", file=sys.stderr)
            await update_status({**progress_patch(url), "last_error": str(error)[:500]})

        if options.hashtags:
            attempted_hashtag_urls: set[str] = set()

            async def discover_hashtag_urls() -> list[str]:
                nonlocal filtered_count, cooldown_skipped_count
                try:
                    discovered = await collect_hashtag_reel_urls(
                        page,
                        options.hashtags,
                        options.max_items,
                        reel_metadata,
                        should_stop=lambda: stop_requested,
                    )
                except CrawlerAccessError as error:
                    if options.max_items == 0 and error.code == "hashtag_reels_not_found":
                        print("해시태그에서 새 릴스 후보를 찾지 못했습니다.")
                        return []
                    raise
                prefiltered = prefilter_hashtag_reel_urls(
                    discovered,
                    reel_metadata,
                    reel_store.rows,
                    options.max_upload_age_days,
                    required_hashtags=options.hashtags,
                )
                filtered_count += prefiltered["uploadAgeSkipped"]
                cooldown_skipped_count += prefiltered["cooldownSkipped"]
                candidates = unattempted_hashtag_urls(prefiltered["urls"], attempted_hashtag_urls)
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
                hashtag_urls = await discover_hashtag_urls()
                for index, url in enumerate(hashtag_urls):
                    if stop_requested or (options.max_items and captured >= options.max_items):
                        break
                    attempted_hashtag_urls.add(url)
                    try:
                        await navigate_with_retries(page, url)
                        if options.manual:
                            await async_input("현재 릴스를 다시 수집하려면 Enter를 누르세요: ")
                        else:
                            await page.wait_for_timeout(next_delay_seconds * 1000)
                        await report_direct_result(index, url, await capture_current_reel())
                    except CrawlerAccessError:
                        raise
                    except Exception as error:
                        await report_direct_error(index, url, error)

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
                        attach_reel_metadata_collector(worker_page, worker_metadata)
                    processed = 0
                    try:
                        while not fatal_errors:
                            try:
                                index, url = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                return
                            try:
                                await navigate_with_retries(worker_page, url)
                                await worker_page.wait_for_timeout(DIRECT_REEL_SETTLE_MILLISECONDS)
                                record = await capture_current_reel(worker_page, worker_metadata, DIRECT_REEL_METADATA_TIMEOUT_MILLISECONDS)
                                await report_direct_result(index, url, record)
                            except CrawlerAccessError as error:
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
                                attach_reel_metadata_collector(worker_page, worker_metadata)
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
                    if stop_requested:
                        break
                    if options.hashtags and options.max_items and captured >= options.max_items:
                        break
                    try:
                        await navigate_with_retries(page, url)
                        if options.manual:
                            await async_input("현재 릴스를 다시 수집하려면 Enter를 누르세요: ")
                        else:
                            await page.wait_for_timeout(next_delay_seconds * 1000)
                        await report_direct_result(
                            index,
                            url,
                            await capture_current_reel(),
                        )
                    except CrawlerAccessError:
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
                page = await recycle_collection_page(context, page, options.start_url, reel_metadata)
                page_recycle_count += 1
                items_since_recycle = views_since_recycle = consecutive_unproductive = consecutive_transition_stalls = 0
                last_reels_url = page.url
                await update_status({**progress_patch(), "state": "collecting", "recycle_reason": reason}, True)

            while not stop_requested and (options.max_items == 0 or captured < options.max_items):
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
                    elif record:
                        last_reels_url = record["url"]
                        captured += 1
                        items_since_recycle += 1
                        consecutive_unproductive = 0
                        print(stored_reel_progress_line(captured, options.max_items, record["url"]))
                    else:
                        missing_count += 1
                        consecutive_unproductive += 1
                        print(f"현재 화면에서 릴스 URL 추출 실패 ({consecutive_unproductive}/{REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD})", file=sys.stderr)
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
                    if consecutive_recovery_failures >= REEL_MAX_CONSECUTIVE_RECOVERY_FAILURES:
                        raise RuntimeError(f"Repeated collection recovery failure: {error}") from error
                    await asyncio.sleep(min(5, 0.5 * (2 ** (consecutive_recovery_failures - 1))))
                    await recycle_page(f"transient error {consecutive_recovery_failures}")

        await reel_store.flush()
        print("릴스 스냅샷 저장을 마치고 브라우저를 닫습니다.")
        await safe_close(context)
        context = None
        follower_stats = {"success": 0, "unavailable": 0, "failed": 0, "stopStatus": "", "stopError": ""}
        merged = 0
        if follower_enricher is not None:
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
            print("Follower web: 무로그인 재수집에서는 각 릴스별 직접 조회 결과만 사용했습니다.")
        if merged:
            reel_store.dirty = True
            await reel_store.flush()
        if follower_enricher is not None:
            users_xlsx = await asyncio.to_thread(write_users_xlsx, options.data_dir)
            print(f"사용자 정보 저장 완료: {users_xlsx}")
        print(f"Follower data merged into {csv_path.name}: {merged}")
        output_paths = await reel_store.export_outputs()
        await safe_close(follower_runtime.browser if follower_runtime else None)
        follower_runtime = None
        await safe_close(view_runtime.browser if view_runtime else None)
        view_runtime = None
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
        await safe_close(context)
        await safe_close(browser)
        await safe_close(follower_runtime.browser if follower_runtime else None)
        await safe_close(view_runtime.browser if view_runtime else None)
        if playwright_runtime:
            try:
                await playwright_runtime.stop()
            except Exception:
                pass
        if collector_lock:
            await collector_lock.release()
        signal.signal(signal.SIGINT, previous_sigint)


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
