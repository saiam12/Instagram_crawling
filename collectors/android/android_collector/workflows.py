from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from .diagnostics import CollectorDiagnostics
from .driver import AndroidDriver
from .models import AccessBlockedError, CollectorError, LayoutUnrecognisedError, ObservedReel
from .store import CollectionStore, read_reel_urls_from_xlsx, reel_url_identity
from .ui_parser import (
    detect_access_block,
    detect_rate_limit_signal,
    has_visible_label,
    parse_visible_reel,
)


REELS_LABELS = ("Reels", "릴스")
SEARCH_LABELS = ("Search", "검색")
SHARE_TRIGGER_RESOURCE_IDS = ("share_button", "share_count", "send_button")
SHARE_TRIGGER_LABELS = ("Share", "Send", "공유", "보내기")
COPY_LINK_RESOURCE_IDS = ("copy_link", "copylink", "copy_link_button")
COPY_LINK_LABELS = ("Copy link", "Copy Link", "링크 복사")
REEL_CARD_LABELS = ("Reel by", "릴스")
HASHTAG_GRID_REEL_RESOURCE_IDS = ("grid_card_layout_container",)
_REEL_READY_ATTEMPTS = 3
_DETAIL_PANEL_ATTEMPTS = 4
# A hashtag feed can start with a block of already-saved Reels.  Keep looking
# past it, but avoid an endless loop when Instagram keeps returning the same
# old Reel or has no further content.
_MIN_CONSECUTIVE_KNOWN_REEL_SKIPS = 12
_MAX_CONSECUTIVE_KNOWN_REEL_SKIPS = 50
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
    diagnostics: CollectorDiagnostics | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hashtag_page_url(hashtag: str) -> str:
    return f"https://www.instagram.com/explore/tags/{quote(hashtag.lstrip('#'), safe='')}/"


def _raise_for_access_block(xml: str, diagnostics: CollectorDiagnostics | None = None) -> None:
    blocked = detect_access_block(xml)
    if blocked:
        if blocked == "rate_limited" and diagnostics is not None:
            diagnostics.rate_limit_suspected(detect_rate_limit_signal(xml) or "rate-limit UI signal")
        raise AccessBlockedError(blocked)


def preflight(driver: AndroidDriver, diagnostics: CollectorDiagnostics | None = None) -> None:
    """Verify the selected app is usable without attempting to authenticate."""
    if diagnostics is not None:
        diagnostics.stage_start("PREFLIGHT")
    try:
        driver.ensure_ready()
        _raise_for_access_block(driver.dump_ui(), diagnostics)
    except CollectorError as error:
        if diagnostics is not None:
            diagnostics.stage_failed("PREFLIGHT", reason=type(error).__name__, error=str(error))
        raise
    if diagnostics is not None:
        diagnostics.stage_success("PREFLIGHT")


def _stage_start(options: CollectorOptions, stage: str, **values: object) -> None:
    if options.diagnostics is not None:
        options.diagnostics.stage_start(stage, **values)


def _stage_success(options: CollectorOptions, stage: str, **values: object) -> None:
    if options.diagnostics is not None:
        options.diagnostics.stage_success(stage, **values)


def _stage_failed(options: CollectorOptions, stage: str, reason: str, **values: object) -> None:
    if options.diagnostics is not None:
        options.diagnostics.stage_failed(stage, reason=reason, **values)


def _stage_timeout(options: CollectorOptions, stage: str, **values: object) -> None:
    if options.diagnostics is not None:
        options.diagnostics.stage_timeout(stage, **values)


def _retry(
    options: CollectorOptions,
    *,
    stage: str,
    target: str,
    attempt: int,
    total: int,
    reason: str,
    previous_wait: float,
) -> None:
    if options.diagnostics is not None and attempt > 0:
        options.diagnostics.record_retry(
            stage=stage,
            target=target,
            attempt=attempt + 1,
            total=total,
            reason=reason,
            previous_wait=previous_wait,
        )


def _update_media_diagnostics(options: CollectorOptions, observed: ObservedReel, *, stage: str | None = None) -> None:
    if options.diagnostics is None:
        return
    options.diagnostics.update_media(
        current_url=observed.reel_url or options.reel_url,
        username=observed.username,
        stage=stage,
    )


def _metadata_state(options: CollectorOptions, observed: ObservedReel) -> None:
    if options.diagnostics is None:
        return
    missing = [key for key in ("share_count", "save_count") if observed.metrics.get(key) is None]
    if missing:
        options.diagnostics.ui_event("METADATA_MISSING", fields=",".join(missing))
    else:
        options.diagnostics.ui_event("METADATA_RENDER_OK")


def _reopen_reel_from_hashtag_grid(
    options: CollectorOptions,
    driver: AndroidDriver,
    grid_xml: str,
) -> str:
    """Return a Reel XML after a profile back-navigation lands on a tag grid."""
    if options.source_mode != "hashtag":
        return ""
    _stage_start(options, "OPEN_REEL", source="hashtag_grid")
    if not driver.tap_resource_id(HASHTAG_GRID_REEL_RESOURCE_IDS, ui_xml=grid_xml):
        _stage_failed(options, "OPEN_REEL", "ELEMENT_LOOKUP_FAILED", target="hashtag_grid_reel")
        return ""
    _stage_success(options, "OPEN_REEL", source="hashtag_grid")
    _stage_start(options, "WAIT_FOR_RENDER", target="reel")
    for attempt in range(_REEL_READY_ATTEMPTS):
        _retry(
            options,
            stage="WAIT_FOR_RENDER",
            target="reopened_reel",
            attempt=attempt,
            total=_REEL_READY_ATTEMPTS,
            reason="ELEMENT_TIMEOUT",
            previous_wait=0.45 if attempt == 0 else 0.2,
        )
        _ui_pause(options, 0.45 if attempt == 0 else 0.2)
        reel_xml = driver.dump_ui()
        _raise_for_access_block(reel_xml, options.diagnostics)
        reopened = parse_visible_reel(
            reel_xml,
            source_mode=options.source_mode,
            source_query=options.source_query,
            reel_url=options.reel_url,
            collected_at=utc_now_iso(),
        )
        if reopened.username:
            _stage_success(options, "WAIT_FOR_RENDER", target="reopened_reel")
            if options.diagnostics is not None:
                options.diagnostics.ui_event("MEDIA_RENDER_OK", surface="reopened_reel")
            return reel_xml
    _stage_timeout(options, "WAIT_FOR_RENDER", target="reopened_reel")
    return ""


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
    _stage_start(options, "READ_REEL_URL")
    if observed.reel_url:
        _stage_success(options, "READ_REEL_URL", source="input_url")
        return observed, reel_xml
    if not callable(getattr(driver, "read_clipboard", None)):
        _stage_failed(options, "READ_REEL_URL", "ELEMENT_LOOKUP_FAILED", target="clipboard_reader")
        return observed, reel_xml
    opened = driver.tap_resource_id(SHARE_TRIGGER_RESOURCE_IDS, ui_xml=reel_xml)
    if not opened and not driver.tap_text(SHARE_TRIGGER_LABELS, ui_xml=reel_xml):
        _stage_failed(options, "READ_REEL_URL", "ELEMENT_LOOKUP_FAILED", target="share_button")
        return observed, reel_xml

    share_xml = ""
    copy_tapped = False
    share_panel_seen = False
    for attempt in range(_DETAIL_PANEL_ATTEMPTS):
        _retry(
            options,
            stage="READ_REEL_URL",
            target="share_panel_copy_link",
            attempt=attempt,
            total=_DETAIL_PANEL_ATTEMPTS,
            reason="ELEMENT_TIMEOUT",
            previous_wait=0.3 if attempt == 0 else 0.15,
        )
        _ui_pause(options, 0.3 if attempt == 0 else 0.15)
        share_xml = driver.dump_ui()
        _raise_for_access_block(share_xml, options.diagnostics)
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
            _update_media_diagnostics(options, observed, stage="READ_REEL_URL")
            _stage_success(options, "READ_REEL_URL", source="share_copy_link")
        else:
            _stage_failed(options, "READ_REEL_URL", "METADATA_MISSING", fields="reel_url")
        break

    if not copy_tapped:
        _stage_timeout(options, "READ_REEL_URL", target="share_panel_copy_link")

    # Copy link normally leaves its share sheet open.  Check the concrete
    # screen first so versions that close it automatically are not navigated
    # backward out of the Reel.
    if not copy_tapped and not share_panel_seen:
        return observed, reel_xml
    _ui_pause(options, 0.15)
    restored_xml = driver.dump_ui()
    _raise_for_access_block(restored_xml, options.diagnostics)
    if has_visible_label(restored_xml, COPY_LINK_LABELS):
        driver.press_back()
        _ui_pause(options, 0.25)
        restored_xml = driver.dump_ui()
        _raise_for_access_block(restored_xml, options.diagnostics)
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
) -> ObservedReel:
    observed: ObservedReel | None = None
    xml = ""
    _stage_start(options, "OPEN_REEL", source=options.source_mode)
    _stage_start(options, "WAIT_FOR_RENDER", target="reel")
    _stage_start(options, "READ_USERNAME")
    for attempt in range(_REEL_READY_ATTEMPTS):
        _retry(
            options,
            stage="WAIT_FOR_RENDER",
            target="reel",
            attempt=attempt,
            total=_REEL_READY_ATTEMPTS,
            reason="ELEMENT_TIMEOUT",
            previous_wait=0.25,
        )
        xml = driver.dump_ui()
        _raise_for_access_block(xml, options.diagnostics)
        candidate = parse_visible_reel(
            xml,
            source_mode=options.source_mode,
            source_query=options.source_query,
            reel_url=options.reel_url,
            collected_at=utc_now_iso(),
        )
        if candidate.username:
            observed = candidate
            _stage_success(options, "READ_USERNAME", username=observed.username)
            _update_media_diagnostics(options, observed, stage="WAIT_FOR_RENDER")
            _stage_success(options, "WAIT_FOR_RENDER", target="reel")
            _stage_success(options, "OPEN_REEL", source=options.source_mode)
            if options.diagnostics is not None:
                options.diagnostics.ui_event("MEDIA_RENDER_OK", username=observed.username)
                options.diagnostics.ui_event(
                    "VIDEO_RENDER_STATUS_UNAVAILABLE",
                    reason="UIAutomator exposes the Reel surface but not reliable video playback state.",
                )
            break
        if attempt < _REEL_READY_ATTEMPTS - 1:
            _ui_pause(options, 0.25)
    if observed is None:
        _stage_timeout(options, "READ_USERNAME", target="reel_author")
        _stage_timeout(options, "WAIT_FOR_RENDER", target="reel")
        _stage_failed(options, "OPEN_REEL", "UI_RENDER_FAILED", target="reel")
        if options.diagnostics is not None:
            options.diagnostics.ui_event("UI_RENDER_FAILED", target="reel")
        raise LayoutUnrecognisedError(
            "The current screen did not expose a Reel author after waiting; it was not saved."
        )

    # New-only collection stops at the first known Reel.  Do this before
    # opening its detail sheet or creating evidence, so an existing Reel is
    # never re-collected as a side effect of the duplicate check.
    if known_fingerprints is not None and observed.reel_fingerprint in known_fingerprints:
        return observed

    for stage, metric_key in (("READ_SHARE_COUNT", "share_count"), ("READ_SAVED_COUNT", "save_count")):
        _stage_start(options, stage)
        if observed.metrics.get(metric_key) is not None:
            _stage_success(options, stage)
        else:
            _stage_failed(options, stage, "METADATA_MISSING", fields=metric_key)

    observed, _ = _capture_reel_url(options, driver, observed, xml)
    observed = replace(
        observed,
        metrics={key: value for key, value in observed.metrics.items() if key in {"share_count", "save_count"}},
        visible_metrics={key: value for key, value in observed.visible_metrics.items() if key in {"share_count", "save_count"}},
        like_count_is_private=None,
    )
    _metadata_state(options, observed)
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
        f" | shares={_metric_progress_value(observed, 'share_count')}"
        f" | saved={_metric_progress_value(observed, 'save_count')}",
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
    known_skip_limit = min(
        _MAX_CONSECUTIVE_KNOWN_REEL_SKIPS,
        max(_MIN_CONSECUTIVE_KNOWN_REEL_SKIPS, limit * 3),
    )
    while stored < limit:
        if options.manual:
            input("Press Enter to capture the current Reel: ")
        if options.diagnostics is not None:
            options.diagnostics.begin_media()
        try:
            observed = capture_current_reel(
                options,
                driver,
                store,
                known_fingerprints=seen,
            )
        except LayoutUnrecognisedError as error:
            if options.diagnostics is not None:
                options.diagnostics.finish_media("FAILED", success=False, error=str(error))
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
        except CollectorError as error:
            if options.diagnostics is not None:
                options.diagnostics.finish_media("FAILED", success=False, error=str(error))
            raise
        skipped_loading_screens = 0
        if observed.reel_fingerprint in seen:
            _update_media_diagnostics(options, observed)
            if options.diagnostics is not None:
                options.diagnostics.finish_media(
                    "SKIPPED",
                    success=False,
                    error="already_saved",
                    count_as_failure=False,
                )
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
            _stage_start(options, "SCROLL_NEXT")
            driver.swipe_up()
            _stage_success(options, "SCROLL_NEXT")
            _pause(options.delay_seconds)
            continue
        consecutive_known_reels = 0
        seen.add(observed.reel_fingerprint)
        _stage_start(options, "SAVE_RESULT")
        try:
            store.append(observed)
        except Exception as error:
            _stage_failed(options, "SAVE_RESULT", type(error).__name__, error=str(error))
            if options.diagnostics is not None:
                options.diagnostics.finish_media("FAILED", success=False, error=str(error))
            raise
        _stage_success(options, "SAVE_RESULT")
        stored += 1
        if options.diagnostics is not None:
            options.diagnostics.finish_media("SUCCESS", success=True)
        _print_progress(
            progress_start + stored,
            progress_total or limit,
            observed,
            verbose_progress=options.verbose_progress,
        )
        if stored % options.checkpoint_items == 0:
            store.export()
        if stored < limit:
            _stage_start(options, "SCROLL_NEXT")
            driver.swipe_up()
            _stage_success(options, "SCROLL_NEXT")
            _pause(options.delay_seconds)
    return stored


def run_feed(options: CollectorOptions, driver: AndroidDriver, store: CollectionStore) -> int:
    preflight(driver, options.diagnostics)
    if options.start_url:
        _stage_start(options, "OPEN_REEL", source="start_url")
        driver.open_instagram_url(options.start_url)
        _stage_success(options, "OPEN_REEL", source="start_url")
        _pause(options.delay_seconds)
    else:
        _stage_start(options, "OPEN_REELS_TAB")
        driver.launch_instagram()
        if driver.tap_text(REELS_LABELS):
            _stage_success(options, "OPEN_REELS_TAB")
        else:
            _stage_failed(options, "OPEN_REELS_TAB", "ELEMENT_LOOKUP_FAILED", target="reels_tab")
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
    preflight(driver, options.diagnostics)
    stored = 0
    remaining_queries = len(options.hashtags)
    for hashtag in options.hashtags:
        if stored >= options.max_items:
            break
        _stage_start(options, "OPEN_REELS_TAB", source="hashtag", hashtag=hashtag)
        driver.open_instagram_url(hashtag_page_url(hashtag))
        _stage_success(options, "OPEN_REELS_TAB", source="hashtag", hashtag=hashtag)
        _pause(options.delay_seconds)
        _stage_start(options, "OPEN_REEL", source="hashtag", hashtag=hashtag)
        if not driver.tap_text(REEL_CARD_LABELS):
            _stage_failed(options, "OPEN_REEL", "ELEMENT_LOOKUP_FAILED", target="reel_card")
            print(f"No Reel card was visible for hashtag #{hashtag}; trying the next hashtag.", flush=True)
            remaining_queries -= 1
            continue
        _stage_success(options, "OPEN_REEL", source="hashtag", hashtag=hashtag)
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


def _run_refresh_impl(
    options: CollectorOptions,
    driver: AndroidDriver,
    store: CollectionStore,
    *,
    input_xlsx: Path | None = None,
    start_row: int | None = None,
    end_row: int | None = None,
) -> int:
    """Re-open URL-backed Android observations and append refreshed snapshots."""
    workbook_path = input_xlsx or _refresh_workbook_path(store)
    if not workbook_path:
        raise CollectorError(
            f"No {store.reel_stem}.xlsx or instagram_data.xlsx was found in {store.data_dir}. "
            "Android refresh requires previously collected Reel URLs."
        )
    workbook_path = Path(workbook_path)
    if workbook_path.suffix.casefold() != ".xlsx":
        raise CollectorError(f"Android refresh supports .xlsx input files only: {workbook_path}")
    if not workbook_path.is_file():
        raise CollectorError(f"Refresh input workbook was not found: {workbook_path}")
    urls = read_reel_urls_from_xlsx(
        workbook_path,
        start_row=start_row,
        end_row=end_row,
    )
    unique_urls: dict[str, str] = {}
    for url in urls:
        # Keep the first shared URL to preserve its query parameters in the
        # public record while avoiding a duplicate app navigation.
        unique_urls.setdefault(reel_url_identity(url) or url.casefold(), url)
    targets = list(unique_urls.values())
    if start_row is None or end_row is None:
        targets = targets[: options.max_items]
    if not targets:
        raise CollectorError(
            "No Instagram Reel URLs were found. Existing rows without a URL cannot be refreshed by Android."
        )

    preflight(driver, options.diagnostics)
    refreshed = 0
    skipped = 0
    for position, url in enumerate(targets, start=1):
        if options.diagnostics is not None:
            options.diagnostics.begin_media()
            options.diagnostics.update_media(current_url=url, stage="OPEN_REEL")
        _stage_start(options, "OPEN_REEL", source="refresh", position=position)
        try:
            driver.open_instagram_url(url)
        except CollectorError as error:
            _stage_failed(options, "OPEN_REEL", type(error).__name__, error=str(error))
            if options.diagnostics is not None:
                options.diagnostics.finish_media("FAILED", success=False, error=str(error))
            raise
        _stage_success(options, "OPEN_REEL", source="refresh", position=position)
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
            )
        except LayoutUnrecognisedError as error:
            skipped += 1
            if options.diagnostics is not None:
                options.diagnostics.finish_media("FAILED", success=False, error=str(error))
            print(f"Skipped refresh URL {position}/{len(targets)}: {error}", flush=True)
            continue
        except CollectorError as error:
            if options.diagnostics is not None:
                options.diagnostics.finish_media("FAILED", success=False, error=str(error))
            raise
        observed = store.preserve_refresh_fields(observed)
        _stage_start(options, "SAVE_RESULT")
        store.append(observed)
        _stage_success(options, "SAVE_RESULT")
        if options.diagnostics is not None:
            options.diagnostics.finish_media("SUCCESS", success=True)
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


def run_refresh(
    options: CollectorOptions,
    driver: AndroidDriver,
    store: CollectionStore,
    *,
    input_xlsx: Path | None = None,
    start_row: int | None = None,
    end_row: int | None = None,
) -> int:
    """Run refresh and export any appended rows before propagating a failure."""
    initial_row_count = len(store.rows)
    try:
        return _run_refresh_impl(
            options,
            driver,
            store,
            input_xlsx=input_xlsx,
            start_row=start_row,
            end_row=end_row,
        )
    except BaseException:
        if len(store.rows) > initial_row_count:
            try:
                store.export()
            except Exception:
                pass
        raise
