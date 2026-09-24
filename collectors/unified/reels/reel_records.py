"""Pure Reel record normalization and derived-field helpers."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urljoin, urlparse


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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def isoformat_kst(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone(timedelta(hours=9))).isoformat(timespec="milliseconds")


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
    return re.findall(r"#[\w]+", str(value or ""), flags=re.UNICODE)


def hashtag_page_url(hashtag: str) -> str:
    return f"https://www.instagram.com/explore/tags/{quote(str(hashtag).strip().lstrip('#'), safe='')}/"


def normalize_reel_url(value: Any) -> dict[str, str] | None:
    parsed = urlparse(urljoin("https://www.instagram.com/", str(value or "")))
    match = re.match(r"^/reels?/([A-Za-z0-9_-]+)/?", parsed.path, re.I)
    if not match:
        return None
    return {"url": f"https://www.instagram.com/reels/{match.group(1)}/", "shortcode": match.group(1)}


def reel_detail_page_url(value: Any) -> str:
    """Return Instagram's singular Reel detail route for browser navigation."""
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
    normalized = normalize_reel_url(value)
    if normalized:
        return normalized
    parsed = urlparse(urljoin("https://www.instagram.com/", str(value or "")))
    match = re.match(r"^/p/([A-Za-z0-9_-]+)/?", parsed.path, re.I)
    if not match:
        return None
    return {"url": f"https://www.instagram.com/reels/{match.group(1)}/", "shortcode": match.group(1)}


def is_search_grid_card_visible(bounds: dict[str, Any], viewport_width: Any, viewport_height: Any) -> bool:
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
    views = parse_metric_count(view_count)
    followers = parse_metric_count(follower_count)
    if not isinstance(views, int) or not isinstance(followers, int) or followers <= 0:
        return ""
    return views / followers


def calculate_reel_derived_fields(
    current: dict[str, Any], previous: dict[str, Any] | None,
) -> dict[str, float | int | str]:
    reaction_rate = calculate_reaction_rate(current.get("view_count"), current.get("follower_count"))
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
    previous_rate = calculate_reaction_rate(previous.get("view_count"), previous.get("follower_count"))
    derived["reaction_rate_change"] = (
        reaction_rate - previous_rate
        if isinstance(reaction_rate, float) and isinstance(previous_rate, float)
        else ""
    )
    return derived


def is_like_count_marked_unavailable(value: Any) -> bool:
    return str(value or "").strip().upper() == UNAVAILABLE_LIKE_COUNT_MARKER


def enrich_reel_collection_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = [{} for _ in rows]
    latest_by_url: dict[str, dict[str, Any]] = {}
    ordered_rows = sorted(
        enumerate(rows),
        key=lambda item: (
            (normalize_reel_url(item[1].get("url")) or {"url": str(item[1].get("url", "") or "")})["url"],
            parse_datetime(item[1].get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc),
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


PROFILE_CATEGORY_TEXT_PATTERN = re.compile(r"^[^\r\n]{1,100}\([^\r\n()]{1,60}\)$")


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
                    "userId": str(value.get("pk") or value.get("id") or ""),
                    "username": observed_username,
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

