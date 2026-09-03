from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from .driver import AndroidDriver
from .store import CollectionStore
from .workflows import CollectorOptions, run_hashtag


# Kept in sync with python_version's public domain vocabulary. Android runs
# only new-Reel discovery; it deliberately never creates recollection jobs.
FASHION_KEYWORDS: tuple[str, ...] = (
    "패션", "데일리룩", "오오티디", "오늘의코디", "코디추천", "패션스타그램",
    "여자코디", "남자코디", "미니멀룩", "스트릿패션", "캐주얼룩", "빈티지룩",
    "아메카지", "워크웨어", "Y2K패션", "클래식룩", "올드머니룩", "프레피룩",
    "모던룩", "시티보이룩", "시티걸룩", "러블리룩", "힙한코디", "출근룩",
    "하객룩", "캠퍼스룩", "데이트룩", "여행룩", "가을코디", "간절기코디",
    "데님코디", "셋업코디", "자켓코디", "니트코디", "스니커즈코디", "가방추천",
    "ootd", "dailylook", "outfitinspo", "koreanstyle", "kfashion", "streetstyle",
    "fashionreels", "lookbook", "styleinspo", "menswear", "womenswear", "streetwear",
)
BEAUTY_KEYWORDS: tuple[str, ...] = (
    "화장품", "뷰티", "뷰티스타그램", "화장품추천", "스킨케어", "기초화장품",
    "피부관리", "피부케어", "메이크업", "데일리메이크업", "메이크업튜토리얼", "메이크업추천",
    "K뷰티", "kbeauty", "koreanskincare", "koreanmakeup", "올리브영추천", "올리브영",
    "스킨케어루틴", "피부장벽", "진정케어", "수분케어", "모공케어", "트러블케어",
    "선크림", "선크림추천", "클렌징", "토너", "세럼", "앰플",
    "에센스", "크림", "마스크팩", "쿠션", "립메이크업", "블러셔",
    "아이메이크업", "향수추천", "헤어케어", "두피케어", "PDRN", "나이아신아마이드",
    "레티놀", "비타민C", "glassskin", "grwm", "cleanbeauty", "skincare",
)


@dataclass(frozen=True)
class NewOnlyScheduleOptions:
    data_dir: Path
    duration_hours: float = 16.0
    discovery_interval_minutes: float = 30.0
    new_items_per_window: int = 300
    max_new_items_per_window: int = 300
    keywords_per_window: int = 5
    test_single_hashtag: bool = False
    base_output: bool = False
    fashion_keywords: Sequence[str] = FASHION_KEYWORDS
    beauty_keywords: Sequence[str] = BEAUTY_KEYWORDS


def _keyword_group(keywords: Sequence[str], window: int, count: int, single: bool) -> tuple[str, ...]:
    if single:
        return tuple(keywords[:1])
    start = (window * count) % len(keywords)
    return tuple((list(keywords) * 2)[start:start + count])


def _stems(domain: str, base_output: bool) -> tuple[str, str]:
    return ("reels", "users") if base_output else (f"{domain}_reels", f"{domain}_users")


def run_new_only_schedule(
    options: CollectorOptions,
    schedule: NewOnlyScheduleOptions,
    driver: AndroidDriver,
    domains: Sequence[str],
) -> int:
    """Run new-only domain discovery windows until the configured deadline."""
    started = time.monotonic()
    deadline = started + schedule.duration_hours * 3_600
    window = 0
    total = 0
    while time.monotonic() < deadline:
        domain = domains[window % len(domains)]
        keywords = schedule.fashion_keywords if domain == "fashion" else schedule.beauty_keywords
        selected = _keyword_group(keywords, window, schedule.keywords_per_window, schedule.test_single_hashtag)
        reel_stem, user_stem = _stems(domain, schedule.base_output)
        store = CollectionStore(schedule.data_dir, reel_stem=reel_stem, user_stem=user_stem)
        limit = min(schedule.new_items_per_window, schedule.max_new_items_per_window)
        total += run_hashtag(
            replace(options, max_items=limit, hashtags=selected, source_mode="hashtag"),
            driver,
            store,
        )
        window += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, schedule.discovery_interval_minutes * 60))
    return total
