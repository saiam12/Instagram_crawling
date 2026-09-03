from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from .driver import AndroidDriver
from .models import AccessBlockedError, CollectorError, LayoutUnrecognisedError, ObservedProfile, ObservedReel
from .store import CollectionStore, read_reel_urls_from_xlsx, reel_url_identity
from .ui_parser import (
    detect_access_block,
    has_visible_label,
    is_comments_panel,
    is_likes_and_plays_panel,
    is_profile_screen,
    parse_account_country,
    parse_uploaded_at,
    parse_visible_profile,
    parse_visible_reel,
)


REELS_LABELS = ("Reels", "릴스")
SEARCH_LABELS = ("Search", "검색")
# The compact side-rail text exposes a non-mutating “View likes” control. Its
# accessibility label includes the exact current count even when the paint
# layer abbreviates it as 15.6K.
LIKE_DETAILS_TRIGGER_LABELS = (
    "Like number is",
    "View likes",
    "좋아요 수",
    "좋아요 보기",
)
COMMENT_DETAILS_TRIGGER_LABELS = (
    "Comment number is",
    "View comments",
    "댓글 수",
    "댓글 보기",
)
COMMENT_DETAILS_TRIGGER_RESOURCE_IDS = ("comment_button", "comment_count", "comments_count")
SHARE_TRIGGER_RESOURCE_IDS = ("share_button", "share_count", "send_button")
SHARE_TRIGGER_LABELS = ("Share", "Send", "공유", "보내기")
COPY_LINK_RESOURCE_IDS = ("copy_link", "copylink", "copy_link_button")
COPY_LINK_LABELS = ("Copy link", "Copy Link", "링크 복사")
REEL_CARD_LABELS = ("Reel by", "릴스")
HASHTAG_GRID_REEL_RESOURCE_IDS = ("grid_card_layout_container",)
_REEL_READY_ATTEMPTS = 3
_DETAIL_PANEL_ATTEMPTS = 4
_PROFILE_READY_ATTEMPTS = 3
# A hashtag feed can start with a block of already-saved Reels.  Keep looking
# past it, but avoid an endless loop when Instagram keeps returning the same
# old Reel or has no further content.
_MIN_CONSECUTIVE_KNOWN_REEL_SKIPS = 12
_MAX_CONSECUTIVE_KNOWN_REEL_SKIPS = 50
AUTHOR_RESOURCE_IDS = ("clips_author_username", "author_username")
CAPTION_RESOURCE_IDS = ("clips_caption_component", "caption_component")
PROFILE_OPTIONS_LABELS = ("Options", "옵션")
ABOUT_ACCOUNT_LABELS = ("About this account", "이 계정 정보")
KST = timezone(timedelta(hours=9))
_REEL_URL_IN_TEXT = re.compile(r"https?://(?:www\.)?instagram\.com/reels?/[A-Za-z0-9_-]+/?[^\s'\"<>]*", re.IGNORECASE)


@dataclass(frozen=True)
class CollectorOptions:
    max_items: int = 50
    delay_seconds: float = 1.0
    checkpoint_items: int = 100
    progress_offset: int = 0
    manual: bool = False
    start_url: str = ""
    source_mode: str = "feed"
    source_query: str = ""
    reel_url: str = ""
    hashtags: tuple[str, ...] = ()
    verbose_progress: bool = False
    capture_screenshots: bool = True
    reuse_profiles_within_run: bool = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hashtag_page_url(hashtag: str) -> str:
    return f"https://www.instagram.com/explore/tags/{quote(hashtag.lstrip('#'), safe='')}/"


def _raise_for_access_block(xml: str) -> None:
    blocked = detect_access_block(xml)
    if blocked:
        raise AccessBlockedError(blocked)


def preflight(driver: AndroidDriver) -> None:
    """Verify the selected app is usable without attempting to authenticate."""
    driver.ensure_ready()
    _raise_for_access_block(driver.dump_ui())


def _reopen_reel_from_hashtag_grid(
    options: CollectorOptions,
    driver: AndroidDriver,
    grid_xml: str,
) -> str:
    """Return a Reel XML after a profile back-navigation lands on a tag grid."""
    if options.source_mode != "hashtag":
        return ""
    if not driver.tap_resource_id(HASHTAG_GRID_REEL_RESOURCE_IDS, ui_xml=grid_xml):
        return ""
    for attempt in range(_REEL_READY_ATTEMPTS):
        _ui_pause(options, 0.45 if attempt == 0 else 0.2)
        reel_xml = driver.dump_ui()
        _raise_for_access_block(reel_xml)
        reopened = parse_visible_reel(
            reel_xml,
            source_mode=options.source_mode,
            source_query=options.source_query,
            reel_url=options.reel_url,
            collected_at=utc_now_iso(),
        )
        if reopened.username:
            return reel_xml
    return ""


def _capture_account_country(
    options: CollectorOptions,
    driver: AndroidDriver,
    profile: ObservedProfile,
    profile_xml: str,
) -> ObservedProfile:
    """Read a public account country through the profile's About menu."""
    if not driver.tap_text(PROFILE_OPTIONS_LABELS, ui_xml=profile_xml):
        return profile

    menu_xml = ""
    menu_opened = False
    about_opened = False
    try:
        for attempt in range(_PROFILE_READY_ATTEMPTS):
            _ui_pause(options, 0.35 if attempt == 0 else 0.18)
            menu_xml = driver.dump_ui()
            _raise_for_access_block(menu_xml)
            menu_opened = has_visible_label(menu_xml, ABOUT_ACCOUNT_LABELS)
            if menu_opened and driver.tap_text(ABOUT_ACCOUNT_LABELS, ui_xml=menu_xml):
                about_opened = True
                break
        if not about_opened:
            return profile
        for attempt in range(_PROFILE_READY_ATTEMPTS):
            _ui_pause(options, 0.4 if attempt == 0 else 0.18)
            about_xml = driver.dump_ui()
            _raise_for_access_block(about_xml)
            country = parse_account_country(about_xml)
            if country:
                return replace(profile, account_country=country)
        return profile
    finally:
        # Instagram's Android builds do not all use the same back stack here.
        # On the current emulator build, Back from About this account returns
        # straight to the profile; on other builds it first returns to the
        # three-dot menu.  A fixed second Back wrongly closes the Reel on the
        # former, putting hashtag collection back on its result grid.  Inspect
        # the actual post-back surface and dismiss the menu only when it is
        # still present.
        menu_needs_back = menu_opened and not about_opened
        if about_opened:
            driver.press_back()  # About this account -> profile or options
            _ui_pause(options, 0.25)
            returned_xml = driver.dump_ui()
            _raise_for_access_block(returned_xml)
            menu_needs_back = has_visible_label(returned_xml, ABOUT_ACCOUNT_LABELS)
        if menu_needs_back:
            driver.press_back()  # profile options -> profile
            _ui_pause(options, 0.25)


def _capture_author_profile(
    options: CollectorOptions,
    driver: AndroidDriver,
    observed: ObservedReel,
    reel_xml: str,
) -> tuple[ObservedReel, str]:
    """Open the visible author profile, read public fields, and return to the Reel."""
    if not driver.tap_resource_id(AUTHOR_RESOURCE_IDS, ui_xml=reel_xml):
        return observed, reel_xml

    profile_xml = ""
    profile_detected = False
    try:
        for attempt in range(_PROFILE_READY_ATTEMPTS):
            _ui_pause(options, 0.45 if attempt == 0 else 0.2)
            profile_xml = driver.dump_ui()
            _raise_for_access_block(profile_xml)
            if not is_profile_screen(profile_xml):
                continue
            profile_detected = True
            profile = parse_visible_profile(profile_xml, expected_username=observed.username)
            observed = observed.with_profile(
                _capture_account_country(options, driver, profile, profile_xml)
            )
            break
    finally:
        # The author control is a navigation action.  Always restore the Reel
        # when the profile was rendered, including a still-loading profile with
        # no count values yet.
        if profile_detected or (
            profile_xml
            and not parse_visible_reel(
                profile_xml,
                source_mode=options.source_mode,
                source_query=options.source_query,
                reel_url=options.reel_url,
                collected_at=observed.collected_at,
            ).username
        ):
            driver.press_back()
            _ui_pause(options, 0.25)

    if not profile_detected:
        return observed, reel_xml
    restored_xml = driver.dump_ui()
    _raise_for_access_block(restored_xml)
    restored = parse_visible_reel(
        restored_xml,
        source_mode=options.source_mode,
        source_query=options.source_query,
        reel_url=options.reel_url,
        collected_at=observed.collected_at,
    )
    if restored.username == observed.username:
        return observed, restored_xml

    # In the hashtag search flow Instagram sometimes closes the Reel viewer
    # together with the author profile, returning to the two-column result
    # grid.  Restore a Reel before the outer loop swipes; otherwise it would
    # scroll the grid and fail three times with a missing Reel author.
    reopened_xml = _reopen_reel_from_hashtag_grid(options, driver, restored_xml)
    return observed, reopened_xml or reel_xml


def _capture_likes_and_plays(
    options: CollectorOptions,
    driver: AndroidDriver,
    store: CollectionStore,
    observed: ObservedReel,
    reel_xml: str,
) -> tuple[ObservedReel, str]:
    """Read exact Likes and plays values before leaving the current Reel.

    The side rail is transient.  In particular, visiting a profile first can
    make the accessible Like-number control disappear when the Reel is
    restored.  Opening this sheet first keeps the exact view-count path
    reliable, then returns a fresh Reel XML for the profile navigation.
    """
    if not driver.tap_text(LIKE_DETAILS_TRIGGER_LABELS, ui_xml=reel_xml):
        return observed, reel_xml

    panel_xml = ""
    panel_detected = False
    try:
        for attempt in range(_DETAIL_PANEL_ATTEMPTS):
            _ui_pause(options, 0.45 if attempt == 0 else 0.2)
            panel_xml = driver.dump_ui()
            _raise_for_access_block(panel_xml)
            if not is_likes_and_plays_panel(panel_xml):
                continue
            panel_detected = True
            panel = parse_visible_reel(
                panel_xml,
                source_mode=options.source_mode,
                source_query=options.source_query,
                reel_url=options.reel_url,
                collected_at=observed.collected_at,
            )
            # The sheet title is rendered before its metric rows.  Treat a
            # title-only sheet as loading and keep polling so the exact view
            # count is not silently replaced with "unavailable".
            if (
                panel.metrics.get("view_count") is None
                and panel.like_count_is_private is None
            ):
                continue
            detail_evidence = store.save_evidence(
                store.next_index(),
                panel_xml,
                driver,
                suffix=".likes_and_plays",
                capture_screenshot=options.capture_screenshots,
            )
            observed = replace(
                observed,
                metrics={**observed.metrics, **panel.metrics},
                visible_metrics={**observed.visible_metrics, **panel.visible_metrics},
                detail_evidence_paths=detail_evidence,
                like_count_is_private=(
                    panel.like_count_is_private
                    if panel.like_count_is_private is not None
                    else observed.like_count_is_private
                ),
            )
            break
    finally:
        # If the sheet is still loading, it has no author and the old code
        # left it on screen.  The following swipe then captured that blank
        # sheet as an @unknown Reel.  Return to the Reel in both cases.
        if panel_detected or (
            panel_xml
            and not parse_visible_reel(
                panel_xml,
                source_mode=options.source_mode,
                source_query=options.source_query,
                reel_url=options.reel_url,
                collected_at=observed.collected_at,
            ).username
        ):
            driver.press_back()
            _ui_pause(options, 0.25)

    if not panel_detected:
        return observed, reel_xml
    restored_xml = driver.dump_ui()
    _raise_for_access_block(restored_xml)
    restored = parse_visible_reel(
        restored_xml,
        source_mode=options.source_mode,
        source_query=options.source_query,
        reel_url=options.reel_url,
        collected_at=observed.collected_at,
    )
    if restored.username == observed.username:
        return observed, restored_xml
    # The detail sheet can use the same back stack as the profile flow.  If it
    # closes to the hashtag grid, reopen a card before trying to read the
    # author; cached Reel coordinates are not safe on that grid.
    reopened_xml = _reopen_reel_from_hashtag_grid(options, driver, restored_xml)
    return observed, reopened_xml or reel_xml


def _capture_comment_count(
    options: CollectorOptions,
    driver: AndroidDriver,
    observed: ObservedReel,
    reel_xml: str,
) -> tuple[ObservedReel, str]:
    """Resolve a missing comment count as zero, disabled/limited, or unknown."""
    if observed.metrics.get("comment_count") is not None:
        return observed, reel_xml
    tapped = driver.tap_resource_id(COMMENT_DETAILS_TRIGGER_RESOURCE_IDS, ui_xml=reel_xml)
    if not tapped and not driver.tap_text(COMMENT_DETAILS_TRIGGER_LABELS, ui_xml=reel_xml):
        return observed, reel_xml

    panel_xml = ""
    panel_detected = False
    try:
        for attempt in range(_DETAIL_PANEL_ATTEMPTS):
            _ui_pause(options, 0.45 if attempt == 0 else 0.2)
            panel_xml = driver.dump_ui()
            _raise_for_access_block(panel_xml)
            panel = parse_visible_reel(
                panel_xml,
                source_mode=options.source_mode,
                source_query=options.source_query,
                reel_url=options.reel_url,
                collected_at=observed.collected_at,
            )
            panel_detected = is_comments_panel(panel_xml) or not panel.username
            if panel.metrics.get("comment_count") is None and "comment_count" not in panel.visible_metrics:
                continue
            observed = replace(
                observed,
                metrics={**observed.metrics, **panel.metrics},
                visible_metrics={**observed.visible_metrics, **panel.visible_metrics},
            )
            break
    finally:
        if panel_detected:
            driver.press_back()
            _ui_pause(options, 0.25)

    if not panel_detected:
        return observed, reel_xml
    restored_xml = driver.dump_ui()
    _raise_for_access_block(restored_xml)
    restored = parse_visible_reel(
        restored_xml,
        source_mode=options.source_mode,
        source_query=options.source_query,
        reel_url=options.reel_url,
        collected_at=observed.collected_at,
    )
    if restored.username == observed.username:
        return observed, restored_xml
    reopened_xml = _reopen_reel_from_hashtag_grid(options, driver, restored_xml)
    return observed, reopened_xml or reel_xml


def _capture_caption_upload_date(
    options: CollectorOptions,
    driver: AndroidDriver,
    observed: ObservedReel,
    reel_xml: str,
) -> tuple[ObservedReel, str]:
    """Open the caption sheet to collect Instagram's displayed posting date.

    The feed only shows a shortened caption.  The date is exposed after the
    caption component is tapped.  We deliberately store only an ISO calendar
    date because Instagram does not show a posting time there.
    """
    if not driver.tap_resource_id(CAPTION_RESOURCE_IDS, ui_xml=reel_xml):
        return observed, reel_xml

    caption_xml = ""
    detail_opened = False
    try:
        for attempt in range(_DETAIL_PANEL_ATTEMPTS):
            _ui_pause(options, 0.4 if attempt == 0 else 0.18)
            caption_xml = driver.dump_ui()
            _raise_for_access_block(caption_xml)
            # A changed hierarchy indicates the tap opened the caption surface,
            # even if an unusual app language prevents date parsing below.
            detail_opened = detail_opened or caption_xml != reel_xml
            uploaded_at = parse_uploaded_at(caption_xml, collected_at=observed.collected_at)
            if uploaded_at:
                observed = replace(observed, uploaded_at=uploaded_at)
                break
    finally:
        if detail_opened:
            driver.press_back()
            _ui_pause(options, 0.25)

    if not detail_opened:
        return observed, reel_xml
    restored_xml = driver.dump_ui()
    _raise_for_access_block(restored_xml)
    restored = parse_visible_reel(
        restored_xml,
        source_mode=options.source_mode,
        source_query=options.source_query,
        reel_url=options.reel_url,
        collected_at=observed.collected_at,
    )
    if restored.username == observed.username:
        return observed, restored_xml
    reopened_xml = _reopen_reel_from_hashtag_grid(options, driver, restored_xml)
    return observed, reopened_xml or reel_xml


def _copied_reel_url(clipboard_text: str) -> str:
    """Return a valid Instagram Reel URL from Android's copied-share text."""
    for match in _REEL_URL_IN_TEXT.finditer(clipboard_text):
        candidate = match.group(0).rstrip(".,;:!?)]")
        parsed = urlparse(candidate)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.hostname.casefold().endswith("instagram.com")
            and parsed.path.casefold().startswith(("/reel/", "/reels/"))
        ):
            return candidate
    return ""


def _capture_reel_url(
    options: CollectorOptions,
    driver: AndroidDriver,
    observed: ObservedReel,
    reel_xml: str,
) -> tuple[ObservedReel, str]:
    """Use Instagram's visible Share -> Copy link controls to save a Reel URL."""
    if observed.reel_url or not callable(getattr(driver, "read_clipboard", None)):
        return observed, reel_xml
    opened = driver.tap_resource_id(SHARE_TRIGGER_RESOURCE_IDS, ui_xml=reel_xml)
    if not opened and not driver.tap_text(SHARE_TRIGGER_LABELS, ui_xml=reel_xml):
        return observed, reel_xml

    share_xml = ""
    copy_tapped = False
    share_panel_seen = False
    for attempt in range(_DETAIL_PANEL_ATTEMPTS):
        _ui_pause(options, 0.3 if attempt == 0 else 0.15)
        share_xml = driver.dump_ui()
        _raise_for_access_block(share_xml)
        share_panel_seen = share_panel_seen or has_visible_label(share_xml, COPY_LINK_LABELS)
        copy_tapped = driver.tap_resource_id(COPY_LINK_RESOURCE_IDS, ui_xml=share_xml)
        if not copy_tapped:
            copy_tapped = driver.tap_text(COPY_LINK_LABELS, ui_xml=share_xml)
        if not copy_tapped:
            continue
        _ui_pause(options, 0.15)
        try:
            copied_url = _copied_reel_url(str(driver.read_clipboard()))
        except CollectorError:
            # Older Android images can deny shell clipboard reads.  The visible
            # Copy link action has still completed, but URL enrichment must not
            # discard an otherwise valid Reel observation.
            copied_url = ""
        if copied_url:
            observed = replace(observed, reel_url=copied_url)
        break

    # Copy link normally leaves its share sheet open.  Check the concrete
    # screen first so versions that close it automatically are not navigated
    # backward out of the Reel.
    if not copy_tapped and not share_panel_seen:
        return observed, reel_xml
    _ui_pause(options, 0.15)
    restored_xml = driver.dump_ui()
    _raise_for_access_block(restored_xml)
    if has_visible_label(restored_xml, COPY_LINK_LABELS):
        driver.press_back()
        _ui_pause(options, 0.25)
        restored_xml = driver.dump_ui()
        _raise_for_access_block(restored_xml)
    restored = parse_visible_reel(
        restored_xml,
        source_mode=options.source_mode,
        source_query=options.source_query,
        reel_url=observed.reel_url,
        collected_at=observed.collected_at,
    )
    if restored.username == observed.username:
        return observed, restored_xml
    reopened_xml = _reopen_reel_from_hashtag_grid(options, driver, restored_xml)
    return observed, reopened_xml or reel_xml


def capture_current_reel(
    options: CollectorOptions,
    driver: AndroidDriver,
    store: CollectionStore,
    *,
    known_fingerprints: set[str] | None = None,
    profile_cache: dict[str, ObservedProfile] | None = None,
) -> ObservedReel:
    observed: ObservedReel | None = None
    xml = ""
    for attempt in range(_REEL_READY_ATTEMPTS):
        xml = driver.dump_ui()
        _raise_for_access_block(xml)
        candidate = parse_visible_reel(
            xml,
            source_mode=options.source_mode,
            source_query=options.source_query,
            reel_url=options.reel_url,
            collected_at=utc_now_iso(),
        )
        if candidate.username:
            observed = candidate
            break
        if attempt < _REEL_READY_ATTEMPTS - 1:
            _ui_pause(options, 0.25)
    if observed is None:
        raise LayoutUnrecognisedError(
            "The current screen did not expose a Reel author after waiting; it was not saved."
        )

    # New-only collection stops at the first known Reel.  Do this before
    # opening its detail sheet or creating evidence, so an existing Reel is
    # never re-collected as a side effect of the duplicate check.
    if known_fingerprints is not None and observed.reel_fingerprint in known_fingerprints:
        return observed

    # Reuse the just-read bounds.  Saving a screenshot before tapping can take
    # long enough for Instagram to fade the side rail, leaving no way to open
    # Likes and plays and therefore no exact view count.  Do this before
    # profile navigation, which otherwise can hide the side-rail control.
    observed, reel_xml = _capture_likes_and_plays(options, driver, store, observed, xml)
    observed, reel_xml = _capture_reel_url(options, driver, observed, reel_xml)
    observed, reel_xml = _capture_comment_count(options, driver, observed, reel_xml)
    observed, reel_xml = _capture_caption_upload_date(options, driver, observed, reel_xml)
    profile_key = observed.username.casefold()
    cached_profile = profile_cache.get(profile_key) if profile_cache and profile_key else None
    if cached_profile is not None:
        # A profile does not change meaningfully during one collector run.  By
        # reusing the first public snapshot for a repeated author we avoid a
        # profile navigation, country-menu navigation, and several UI dumps
        # without dropping any exported field from later Reels by that author.
        observed = observed.with_profile(replace(cached_profile, username=observed.username))
    else:
        observed, _ = _capture_author_profile(options, driver, observed, reel_xml)
        if profile_cache is not None and profile_key:
            profile_cache[profile_key] = observed.profile
    # The screenshot is deliberately captured after returning from the sheet;
    # it still represents the same current Reel while retaining the original,
    # pre-tap XML as its evidence.
    evidence = store.save_evidence(
        store.next_index(),
        xml,
        driver,
        capture_screenshot=options.capture_screenshots,
    )
    return observed.with_evidence(evidence)


def _pause(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _ui_pause(options: CollectorOptions, baseline_seconds: float) -> None:
    """Wait just long enough for a tapped Android surface to render.

    The old fixed 0.7--0.8 second sleeps were paid for every detail panel,
    even when the emulator was already ready.  The configured interval now
    caps these short readiness pauses, with a 0.15-second floor so an
    explicit zero interval cannot race the UI transition.
    """
    _pause(max(0.15, min(baseline_seconds, options.delay_seconds)))


def _metric_progress_value(observed: ObservedReel, key: str) -> str:
    metric = observed.metrics.get(key)
    return f"{metric.value:,}" if metric and metric.value is not None else "unavailable"


def _terminal_value(value: object) -> str:
    """Keep every captured field legible on one terminal line."""
    if isinstance(value, bool):
        return str(value).lower()
    return re.sub(r"\s+", " ", str(value)).strip()


def _field_progress_value(name: str, value: object, *, unavailable_note: str = "") -> str:
    normalized = _terminal_value(value) if value is not None else ""
    if not normalized:
        suffix = f" ({unavailable_note})" if unavailable_note else ""
        return f"{name}=unavailable{suffix}"
    return f"{name}=collected({_terminal_value(value)})"


def _metric_field_progress_value(observed: ObservedReel, key: str) -> str:
    metric = observed.metrics.get(key)
    if metric and metric.value is not None:
        return f"{key}=collected({metric.value:,})"
    visible = observed.visible_metrics.get(key, "")
    if key == "comment_count" and visible in {"comments_disabled", "comments_limited"}:
        return f"comment_count=unavailable({visible.removeprefix('comments_')})"
    if visible:
        return f"{key}=visible_only({_terminal_value(visible)}; exact=unavailable)"
    return f"{key}=unavailable"


def _caption_hashtags(caption: str) -> str:
    unique: dict[str, str] = {}
    for tag in re.findall(r"#[\w]+", caption, flags=re.UNICODE):
        unique.setdefault(tag.casefold(), tag)
    return " ".join(unique.values())


def _ad_progress_value(observed: ObservedReel) -> str:
    if observed.is_ad:
        return "true"
    if "#협찬" in observed.caption.casefold():
        return "협찬"
    return "false"


def _days_since_upload_for_progress(observed: ObservedReel) -> int | str:
    try:
        uploaded_date = datetime.fromisoformat(observed.uploaded_at).date()
        collected_time = datetime.fromisoformat(observed.collected_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return ""
    if collected_time.tzinfo is None:
        collected_time = collected_time.replace(tzinfo=timezone.utc)
    return max(0, (collected_time.astimezone(KST).date() - uploaded_date).days)


def _print_verbose_progress(observed: ObservedReel) -> None:
    """Print every Android/public export field with an explicit availability state."""
    profile = observed.profile
    print("  capture:", _field_progress_value("status", observed.status), "|", _field_progress_value("collected_at", observed.collected_at))
    print(
        "  reel:",
        " | ".join(
            (
                _field_progress_value("source_mode", observed.source_mode),
                _field_progress_value("source_query", observed.source_query),
                _field_progress_value("url", observed.reel_url, unavailable_note="not exposed by app"),
                _field_progress_value("user_id", profile.user_id, unavailable_note="not exposed by app"),
                _field_progress_value("username", observed.username),
            )
        ),
    )
    print(
        "  content:",
        " | ".join(
            (
                _field_progress_value("caption/title", observed.caption),
                _field_progress_value("hashtags", _caption_hashtags(observed.caption)),
                _field_progress_value("audio_name", observed.audio_name),
                _field_progress_value("location_name", observed.location_name, unavailable_note="not exposed by app"),
                _field_progress_value("ad", _ad_progress_value(observed)),
                _field_progress_value("uploaded_at", observed.uploaded_at, unavailable_note="caption detail was not exposed"),
                _field_progress_value("video_duration_seconds", "", unavailable_note="not exposed by app"),
                _field_progress_value("days_since_upload", _days_since_upload_for_progress(observed), unavailable_note="requires uploaded_at"),
            )
        ),
    )
    print(
        "  metrics:",
        " | ".join(
            _metric_field_progress_value(observed, key)
            for key in (
                "view_count",
                "like_count",
                "comment_count",
                "repost_count",
                "share_count",
                "save_count",
                "likes_and_plays_count",
            )
        ),
    )
    print(
        "  profile:",
        " | ".join(
            (
                _field_progress_value("biography", profile.biography),
                _field_progress_value("profile_category", profile.profile_category),
                _field_progress_value("post_count", profile.post_count),
                _field_progress_value("following_count", profile.following_count),
                _field_progress_value("follower_count", profile.follower_count),
                _field_progress_value("account_country", profile.account_country),
                _field_progress_value("like_count_is_private", observed.like_count_is_private),
            )
        ),
        flush=True,
    )


def _print_progress(
    current: int,
    total: int,
    observed: ObservedReel,
    *,
    verbose_progress: bool = False,
) -> None:
    username = f"@{observed.username}" if observed.username else "@unknown"
    source = (
        f" | hashtag=#{observed.source_query}"
        if observed.source_mode == "hashtag" and observed.source_query
        else ""
    )
    print(
        f"[{current}/{total}] Collected | {username}"
        f"{source}"
        f" | likes={_metric_progress_value(observed, 'like_count')}"
        f" | views={_metric_progress_value(observed, 'view_count')}",
        flush=True,
    )
    if verbose_progress:
        _print_verbose_progress(observed)


def _collect_scrolling_surface(
    options: CollectorOptions,
    driver: AndroidDriver,
    store: CollectionStore,
    limit: int,
    *,
    progress_start: int = 0,
    progress_total: int | None = None,
) -> int:
    seen = store.known_reel_fingerprints()
    stored = 0
    skipped_loading_screens = 0
    consecutive_known_reels = 0
    profile_cache: dict[str, ObservedProfile] | None = {} if options.reuse_profiles_within_run else None
    known_skip_limit = min(
        _MAX_CONSECUTIVE_KNOWN_REEL_SKIPS,
        max(_MIN_CONSECUTIVE_KNOWN_REEL_SKIPS, limit * 3),
    )
    while stored < limit:
        if options.manual:
            input("Press Enter to capture the current Reel: ")
        try:
            observed = capture_current_reel(
                options,
                driver,
                store,
                known_fingerprints=seen,
                profile_cache=profile_cache,
            )
        except LayoutUnrecognisedError as error:
            # A back navigation can occasionally leave the app on the
            # hashtag-results grid.  It contains Reel cards but no visible
            # author node, so recover by opening a card instead of repeatedly
            # swiping the grid as if it were the full-screen viewer.
            recovered_xml = _reopen_reel_from_hashtag_grid(options, driver, driver.dump_ui())
            if recovered_xml:
                skipped_loading_screens = 0
                print("Recovered hashtag result grid; reopening a Reel.", flush=True)
                _pause(options.delay_seconds)
                continue
            skipped_loading_screens += 1
            print(f"Skipped loading/empty screen: {error}", flush=True)
            if skipped_loading_screens >= _REEL_READY_ATTEMPTS:
                raise CollectorError(
                    "Instagram did not show a usable Reel after three attempts; collection stopped without saving blank data."
                ) from error
            driver.swipe_up()
            _pause(options.delay_seconds)
            continue
        skipped_loading_screens = 0
        if observed.reel_fingerprint in seen:
            consecutive_known_reels += 1
            if consecutive_known_reels >= known_skip_limit:
                print(
                    "Stopped this surface after "
                    f"{consecutive_known_reels} consecutive previously saved Reels; "
                    "continuing with the next source if available.",
                    flush=True,
                )
                break
            print(
                "Skipped previously saved Reel; searching the next Reel "
                f"({consecutive_known_reels}/{known_skip_limit}).",
                flush=True,
            )
            driver.swipe_up()
            _pause(options.delay_seconds)
            continue
        consecutive_known_reels = 0
        seen.add(observed.reel_fingerprint)
        store.append(observed)
        stored += 1
        _print_progress(
            progress_start + stored,
            progress_total or limit,
            observed,
            verbose_progress=options.verbose_progress,
        )
        if stored % options.checkpoint_items == 0:
            store.export()
        if stored < limit:
            driver.swipe_up()
            _pause(options.delay_seconds)
    return stored


def run_feed(options: CollectorOptions, driver: AndroidDriver, store: CollectionStore) -> int:
    preflight(driver)
    if options.start_url:
        driver.open_instagram_url(options.start_url)
        _pause(options.delay_seconds)
    else:
        driver.launch_instagram()
        driver.tap_text(REELS_LABELS)
    stored = _collect_scrolling_surface(
        replace(options, source_mode="feed"),
        driver,
        store,
        options.max_items,
        progress_start=options.progress_offset,
        progress_total=options.progress_offset + options.max_items,
    )
    store.export()
    return stored


def run_hashtag(options: CollectorOptions, driver: AndroidDriver, store: CollectionStore) -> int:
    if not options.hashtags:
        raise ValueError("At least one hashtag is required.")
    preflight(driver)
    stored = 0
    remaining_queries = len(options.hashtags)
    for hashtag in options.hashtags:
        if stored >= options.max_items:
            break
        driver.open_instagram_url(hashtag_page_url(hashtag))
        _pause(options.delay_seconds)
        if not driver.tap_text(REEL_CARD_LABELS):
            print(f"No Reel card was visible for hashtag #{hashtag}; trying the next hashtag.", flush=True)
            remaining_queries -= 1
            continue
        _pause(options.delay_seconds)
        per_query_limit = max(1, (options.max_items - stored + remaining_queries - 1) // remaining_queries)
        stored += _collect_scrolling_surface(
            replace(options, source_mode="hashtag", source_query=hashtag),
            driver,
            store,
            per_query_limit,
            progress_start=options.progress_offset + stored,
            progress_total=options.progress_offset + options.max_items,
        )
        remaining_queries -= 1
    if stored < options.max_items:
        print(
            f"New Reel target not reached: saved {stored}/{options.max_items}. "
            "Remaining visible Reels were previously saved, unavailable, or absent for the selected hashtags.",
            flush=True,
        )
    store.export()
    return stored


def _refresh_workbook_path(store: CollectionStore) -> str:
    for name in (f"{store.reel_stem}.xlsx", "instagram_data.xlsx"):
        candidate = store.data_dir / name
        if candidate.is_file():
            return str(candidate)
    return ""


def run_refresh(options: CollectorOptions, driver: AndroidDriver, store: CollectionStore) -> int:
    """Re-open URL-backed Android observations and append refreshed snapshots."""
    workbook_path = _refresh_workbook_path(store)
    if not workbook_path:
        raise CollectorError(
            f"No {store.reel_stem}.xlsx or instagram_data.xlsx was found in {store.data_dir}. "
            "Android refresh requires previously collected Reel URLs."
        )
    urls = read_reel_urls_from_xlsx(Path(workbook_path))
    unique_urls: dict[str, str] = {}
    for url in urls:
        # Keep the first shared URL to preserve its query parameters in the
        # public record while avoiding a duplicate app navigation.
        unique_urls.setdefault(reel_url_identity(url) or url.casefold(), url)
    targets = list(unique_urls.values())[: options.max_items]
    if not targets:
        raise CollectorError(
            "No Instagram Reel URLs were found. Existing rows without a URL cannot be refreshed by Android."
        )

    preflight(driver)
    refreshed = 0
    skipped = 0
    profile_cache: dict[str, ObservedProfile] | None = {} if options.reuse_profiles_within_run else None
    for position, url in enumerate(targets, start=1):
        driver.open_instagram_url(url)
        _pause(options.delay_seconds)
        refresh_options = replace(
            options,
            source_mode="refresh",
            source_query="",
            reel_url=url,
        )
        try:
            observed = capture_current_reel(
                refresh_options,
                driver,
                store,
                profile_cache=profile_cache,
            )
        except LayoutUnrecognisedError as error:
            skipped += 1
            print(f"Skipped refresh URL {position}/{len(targets)}: {error}", flush=True)
            continue
        observed = store.preserve_refresh_fields(observed)
        store.append(observed)
        refreshed += 1
        _print_progress(
            position,
            len(targets),
            observed,
            verbose_progress=options.verbose_progress,
        )
        if refreshed % options.checkpoint_items == 0:
            store.export()
    store.export()
    if skipped:
        print(f"Android refresh skipped {skipped} unavailable Reel URL(s).", flush=True)
    return refreshed
