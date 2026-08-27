"""Pure scheduling policy for the fashion and beauty collection domains."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import urlsplit


KEYWORDS_PER_WINDOW = 5
SIX_HOUR_NEW_ONLY_KEYWORDS_PER_WINDOW = 5


FASHION_KEYWORDS: Sequence[str] = (
    "패션", "데일리룩", "오오티디", "오늘의코디", "코디추천", "패션스타그램",
    "여자코디", "남자코디", "미니멀룩", "스트릿패션", "캐주얼룩", "빈티지룩",
    "아메카지", "워크웨어", "Y2K패션", "클래식룩", "올드머니룩", "프레피룩",
    "모던룩", "시티보이룩", "시티걸룩", "러블리룩", "힙한코디", "출근룩",
    "하객룩", "캠퍼스룩", "데이트룩", "여행룩", "가을코디", "간절기코디",
    "데님코디", "셋업코디", "자켓코디", "니트코디", "스니커즈코디", "가방추천",
    "ootd", "dailylook", "outfitinspo", "koreanstyle", "kfashion", "streetstyle",
    "fashionreels", "lookbook", "styleinspo", "menswear", "womenswear", "streetwear",
)

BEAUTY_KEYWORDS: Sequence[str] = (
    "화장품", "뷰티", "뷰티스타그램", "화장품추천", "스킨케어", "기초화장품",
    "피부관리", "피부케어", "메이크업", "데일리메이크업", "메이크업튜토리얼", "메이크업추천",
    "K뷰티", "kbeauty", "koreanskincare", "koreanmakeup", "올리브영추천", "올리브영",
    "스킨케어루틴", "피부장벽", "진정케어", "수분케어", "모공케어", "트러블케어",
    "선크림", "선크림추천", "클렌징", "토너", "세럼", "앰플",
    "에센스", "크림", "마스크팩", "쿠션", "립메이크업", "블러셔",
    "아이메이크업", "향수추천", "헤어케어", "두피케어", "PDRN", "나이아신아마이드",
    "레티놀", "비타민C", "glassskin", "grwm", "cleanbeauty", "skincare",
)

SNAPSHOT_OFFSETS: Sequence[timedelta] = (
    timedelta(),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=2),
    timedelta(hours=4),
    timedelta(hours=8),
)


@dataclass(frozen=True)
class DatasetConfig:
    name: Literal["fashion", "beauty"]
    data_root: Path
    keywords: Sequence[str]
    base_output: bool = False


@dataclass(frozen=True)
class RunConfig:
    data_root: Path
    duration_hours: float = 16
    discovery_hours: float = 7
    discovery_interval_minutes: float = 30
    new_items_per_window: int = 300
    max_new_items_per_window: int = 300
    max_upload_age_days: float = 30
    background: bool = False
    direct_reel_info_wait_seconds: float = 3
    exact_metric_attempts: int = 3
    exact_metric_retry_delay_seconds: float = 2
    hashtag_candidates_per_keyword: int = 50
    keywords_per_window: int = KEYWORDS_PER_WINDOW
    new_only: bool = False
    base_output: bool = False
    fashion_keywords: Sequence[str] = FASHION_KEYWORDS
    beauty_keywords: Sequence[str] = BEAUTY_KEYWORDS
    domains: tuple[Literal["fashion", "beauty"], ...] = ("fashion", "beauty")


@dataclass(frozen=True)
class DueJob:
    dataset: str
    url: str
    due_at: datetime


_REEL_PATH = re.compile(r"^/reels?/([A-Za-z0-9_-]+)/?$", re.IGNORECASE)


def _parse_timestamp(value: Any) -> datetime | None:
    """Return a normalized UTC timestamp, or None for malformed input."""
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return None
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_reel_url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    try:
        hostname = parsed.hostname
        parsed.port  # Force validation of non-numeric and out-of-range ports.
    except ValueError:
        return None
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":"):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    hostname = hostname.lower().rstrip(".")
    if hostname != "instagram.com" and not hostname.endswith(".instagram.com"):
        return None
    match = _REEL_PATH.fullmatch(parsed.path)
    if not match:
        return None
    return f"https://www.instagram.com/reels/{match.group(1)}/"


def _collection_timestamp(row: dict[str, Any]) -> datetime | None:
    return _parse_timestamp(row.get("collected_at"))


def _rows_grouped_by_normalized_url(rows: Sequence[dict[str, Any]]) -> dict[str, list[tuple[dict[str, Any], datetime]]]:
    grouped: dict[str, list[tuple[dict[str, Any], datetime]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = _normalize_reel_url(row.get("url"))
        timestamp = _collection_timestamp(row)
        if url is None or timestamp is None:
            continue
        grouped.setdefault(url, []).append((row, timestamp))
    return grouped


def due_jobs(dataset: DatasetConfig, rows: list[dict[str, Any]], now: datetime) -> list[DueJob]:
    current = _parse_timestamp(now)
    if current is None:
        return []
    result: list[DueJob] = []
    for url, snapshots in _rows_grouped_by_normalized_url(rows).items():
        snapshots.sort(key=lambda snapshot: snapshot[1])
        if len(snapshots) < len(SNAPSHOT_OFFSETS):
            due_at = snapshots[0][1] + SNAPSHOT_OFFSETS[len(snapshots)]
            if due_at <= current:
                result.append(DueJob(dataset.name, url, due_at))
    return sorted(result, key=lambda job: (job.due_at, job.dataset, job.url))


def window_dataset(
    started_at: datetime,
    now: datetime,
    interval_minutes: float = 30,
) -> Literal["fashion", "beauty"]:
    started = _parse_timestamp(started_at)
    current = _parse_timestamp(now)
    if started is None or current is None:
        raise ValueError("started_at and now must be valid timestamps")
    interval_seconds = float(interval_minutes) * 60
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("interval_minutes must be finite and greater than zero")
    index = int((current - started).total_seconds() // interval_seconds)
    return "fashion" if index % 2 == 0 else "beauty"


def keyword_group(
    keywords: Sequence[str],
    active_window_number: int,
    keywords_per_window: int = KEYWORDS_PER_WINDOW,
) -> tuple[str, ...]:
    if not keywords:
        raise ValueError("keywords must contain at least one entry")
    if active_window_number < 1:
        raise ValueError("active_window_number must be one-based")
    if keywords_per_window < 1:
        raise ValueError("keywords_per_window must be greater than zero")
    group_count = math.ceil(len(keywords) / keywords_per_window)
    start = ((int(active_window_number) - 1) % group_count) * keywords_per_window
    return tuple(keywords[start : start + keywords_per_window])


def initial_count_in_window(rows: list[dict[str, Any]], start: datetime, end: datetime) -> int:
    window_start = _parse_timestamp(start)
    window_end = _parse_timestamp(end)
    if window_start is None or window_end is None or window_end <= window_start:
        return 0
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _normalize_reel_url(row.get("url")) is None:
            continue
        try:
            is_initial = int(str(row.get("collection_number", "")).strip()) == 1
        except (TypeError, ValueError):
            is_initial = False
        timestamp = _collection_timestamp(row)
        if is_initial and timestamp is not None and window_start <= timestamp < window_end:
            count += 1
    return count


def is_initial_candidate_allowed(uploaded_at: Any, collected_at: Any, max_days: float) -> bool:
    try:
        limit = float(max_days)
    except (TypeError, ValueError):
        return False
    if limit <= 0:
        return True
    uploaded = _parse_timestamp(uploaded_at)
    collected = _parse_timestamp(collected_at)
    if uploaded is None or collected is None:
        return False
    age_seconds = max(0.0, (collected - uploaded).total_seconds())
    return age_seconds <= limit * 86_400


__all__ = [
    "BEAUTY_KEYWORDS",
    "FASHION_KEYWORDS",
    "KEYWORDS_PER_WINDOW",
    "SIX_HOUR_NEW_ONLY_KEYWORDS_PER_WINDOW",
    "SNAPSHOT_OFFSETS",
    "DatasetConfig",
    "DueJob",
    "RunConfig",
    "due_jobs",
    "initial_count_in_window",
    "is_initial_candidate_allowed",
    "keyword_group",
    "window_dataset",
]
