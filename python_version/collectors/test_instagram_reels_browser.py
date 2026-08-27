from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
import zipfile
from xml.etree import ElementTree
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from collectors import instagram_reels_browser as reels_browser
from collectors.instagram_follower_enricher import (
    FollowerEnricher,
    read_csv_objects,
    user_history_path,
)
from exporters import instagram_collector as xlsx_exporter
from exporters.instagram_collector import read_reel_urls_from_xlsx
from collectors.instagram_reels_browser import (
    CSV_FIELDS,
    CollectorLock,
    LongReelStore,
    PYTHON_VERSION_ROOT,
    advance_to_next_reel,
    collection_label,
    collected_record_cooldown,
    create_collection_page,
    build_collected_record,
    collect_reel_metadata,
    collect_hashtag_reel_urls,
    integrate_collected_record,
    launch_collection_context,
    merge_follower_data_into_rows,
    hashtag_candidate_limit,
    hashtag_page_url,
    has_complete_reel_core_data,
    is_instagram_hashtag_surface,
    media_is_reel,
    next_recollect_available_at,
    next_transition_stall_state,
    normalize_search_grid_reel_url,
    parse_args,
    parse_hashtag_query,
    parse_metric_count,
    prefilter_hashtag_reel_urls,
    request_web_follower_count,
)


def reel_record(index: int, collected_at: str | None = None) -> dict[str, object]:
    timestamp = collected_at or (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
    return {
        "collected_at": timestamp,
        "url": f"https://www.instagram.com/reels/test_{index}/",
        "user_id": str(10_000_000 + index),
        "username": f"test_user_{index}",
        "title": f"test reel {index}",
        "hashtags": "#test",
        "audio_name": "test audio",
        "location_name": "",
        "ad": "false",
        "uploaded_at": timestamp,
        "video_duration_seconds": 12.5,
        "days_since_upload": 0,
        "view_count": index * 10,
        "like_count": index,
        "comment_count": 0,
        "repost_count": 0,
        "follower_count": "",
    }


def read_xlsx_header(path: Path) -> list[str]:
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as workbook:
        worksheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    first_row = worksheet.find(f".//{namespace}row")
    if first_row is None:
        return []
    return [
        "".join(node.text or "" for node in cell.findall(f".//{namespace}t"))
        for cell in first_row.findall(f"{namespace}c")
    ]


class CollectorUtilityTests(unittest.TestCase):
    def test_scheduler_flags_are_false_by_default(self) -> None:
        options = parse_args([])
        self.assertFalse(options.new_urls_only)
        self.assertFalse(options.disable_recollect_cooldown)

    def test_filter_new_urls_removes_prior_history_url(self) -> None:
        existing = [reel_record(1)]
        urls = [str(existing[0]["url"]), "https://www.instagram.com/reels/new/"]
        filter_urls = getattr(reels_browser, "filter_new_urls", None)
        self.assertIsNotNone(filter_urls)
        self.assertEqual(filter_urls(urls, existing), ["https://www.instagram.com/reels/new/"])

    def test_stored_reel_progress_line_uses_saved_count_not_candidate_position(self) -> None:
        """Terminal progress must identify the nth persisted Reel, not the nth candidate inspected."""
        formatter = getattr(reels_browser, "stored_reel_progress_line", None)
        self.assertIsNotNone(formatter)
        self.assertEqual(
            formatter(3, 50, "https://www.instagram.com/reels/saved/"),
            "[3/50] https://www.instagram.com/reels/saved/",
        )

    def test_cli_defaults_and_long_run_controls(self) -> None:
        defaults = parse_args([])
        self.assertEqual(defaults.page_recycle_items, 200)
        self.assertEqual(defaults.checkpoint_items, 100)
        self.assertEqual(defaults.storage_layout, "history")
        self.assertEqual(defaults.output_stem, "reels")
        self.assertEqual(defaults.xlsx_layout, "columns")
        self.assertEqual(getattr(defaults, "direct_reel_info_wait_seconds", None), 3)
        self.assertEqual(getattr(defaults, "exact_metric_attempts", None), 3)
        self.assertEqual(getattr(defaults, "exact_metric_retry_delay_seconds", None), 2)
        self.assertFalse(defaults.no_login)
        self.assertEqual(defaults.data_dir, PYTHON_VERSION_ROOT / "data_web")
        self.assertEqual(defaults.profile_dir, PYTHON_VERSION_ROOT / ".instagram_browser_profile")
        options = parse_args([
            "--max-items", "10000", "--page-recycle-items", "250",
            "--checkpoint-items", "500", "--direct-concurrency", "2",
            "--followers-after-reels", "--direct-reel-info-wait-seconds", "4",
            "--exact-metric-attempts", "4", "--exact-metric-retry-delay-seconds", "3.5",
        ])
        self.assertEqual(options.max_items, 10_000)
        self.assertEqual(options.page_recycle_items, 250)
        self.assertEqual(options.checkpoint_items, 500)
        self.assertEqual(options.direct_concurrency, 1)
        self.assertEqual(getattr(options, "direct_reel_info_wait_seconds", None), 4)
        self.assertEqual(getattr(options, "exact_metric_attempts", None), 4)
        self.assertEqual(getattr(options, "exact_metric_retry_delay_seconds", None), 3.5)
        self.assertTrue(options.followers_after_reels)

    def test_user_xlsx_contains_current_user_data_and_biography(self) -> None:
        writer = getattr(reels_browser, "write_users_xlsx", None)
        self.assertIsNotNone(writer)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "users.csv").write_text(
                "user_id,username,biography,profile_category,post_count,follower_count,collected_at\n"
                "987654321,profile_user,영상은 릴스탭 눌러주세요~,의류(브랜드),2078,37293,2026-01-01T00:00:00.000Z\n",
                encoding="utf-8",
            )
            output = writer(data_dir)
            self.assertEqual(output, data_dir / "users.xlsx")
            self.assertTrue(output.exists())
            public_fields, public_rows = read_csv_objects(data_dir / "users.csv")
            history_fields, history_rows = read_csv_objects(
                data_dir / ".collector" / "users_history_active.csv"
            )
            xlsx_header = read_xlsx_header(output)
            with zipfile.ZipFile(output) as workbook:
                workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
                worksheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")

        self.assertIn('sheet name="users"', workbook_xml)
        self.assertIn("biography", worksheet_xml)
        self.assertIn("영상은 릴스탭 눌러주세요~", worksheet_xml)
        self.assertEqual(public_fields, [
            "collection_number", "days_since_previous", "user_id", "username", "biography",
            "profile_category", "post_count", "follower_count", "follower_count_change", "collected_at",
        ])
        self.assertEqual(xlsx_header, public_fields)
        self.assertEqual(public_rows[0]["follower_count_change"], "")
        self.assertEqual(history_fields, [
            "user_id", "username", "biography", "profile_category", "post_count", "follower_count", "collected_at",
        ])
        self.assertEqual(history_rows[0]["follower_count"], "37293")
        self.assertEqual(history_rows[0]["profile_category"], "의류(브랜드)")
        self.assertEqual(history_rows[0]["post_count"], "2078")

    def test_direct_reel_info_wait_rejects_negative_and_nonfinite_values(self) -> None:
        self.assertEqual(parse_args(["--direct-reel-info-wait-seconds", "0"]).direct_reel_info_wait_seconds, 0)
        for value in ("-1", "nan", "inf", "-inf"):
            with self.subTest(value=value), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parse_args(["--direct-reel-info-wait-seconds", value])

    def test_exact_metric_retry_options_are_bounded(self) -> None:
        for arguments in (
            ["--exact-metric-attempts", "0"],
            ["--exact-metric-attempts", "6"],
            ["--exact-metric-retry-delay-seconds", "-1"],
            ["--exact-metric-retry-delay-seconds", "nan"],
        ):
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parse_args(arguments)

    def test_collection_entry_keeps_the_reels_url_in_foreground_login_mode(self) -> None:
        entry_url = getattr(reels_browser, "initial_collection_page_url", None)
        self.assertIsNotNone(entry_url)
        self.assertEqual(
            entry_url("https://www.instagram.com/reels/", background=False, no_login=False),
            "https://www.instagram.com/reels/",
        )
        self.assertEqual(
            entry_url("https://www.instagram.com/reels/custom/", background=True, no_login=False),
            "https://www.instagram.com/reels/custom/",
        )

    def test_hashtag_or_and_metric_parsing(self) -> None:
        self.assertEqual(parse_hashtag_query('"맛집" OR "서울맛집" OR #맛집'), ["맛집", "서울맛집"])
        self.assertEqual(parse_metric_count("1.2만"), 12_000)
        self.assertEqual(parse_metric_count("3.4M"), 3_400_000)
        self.assertEqual(hashtag_candidate_limit(100, 12), 50)
        self.assertEqual(hashtag_candidate_limit(200, 24), 50)
        self.assertEqual(hashtag_candidate_limit(0, 6), 0)
        self.assertEqual(hashtag_candidate_limit(600, 12, 50), 50)
        unlimited = parse_args(["--max-items", "0", "--hashtag-query", '"패션" OR "ootd"'])
        self.assertEqual(unlimited.max_items, 0)
        self.assertEqual(unlimited.hashtags, ["패션", "ootd"])
        self.assertEqual(
            hashtag_page_url("패션스타그램"),
            "https://www.instagram.com/explore/search/keyword/?q=%23%ED%8C%A8%EC%85%98%EC%8A%A4%ED%83%80%EA%B7%B8%EB%9E%A8",
        )
        self.assertTrue(is_instagram_hashtag_surface(hashtag_page_url("패션스타그램")))
        self.assertFalse(is_instagram_hashtag_surface("https://www.instagram.com/explore/search/keyword/?q=패션스타그램"))
        self.assertEqual(
            normalize_search_grid_reel_url("https://www.instagram.com/p/DaWg18xAVcE/?img_index=1"),
            {"url": "https://www.instagram.com/reels/DaWg18xAVcE/", "shortcode": "DaWg18xAVcE"},
        )

    def test_hashtag_rediscovery_excludes_already_attempted_urls(self) -> None:
        select_new = getattr(reels_browser, "unattempted_hashtag_urls", None)
        self.assertIsNotNone(select_new)

        urls = select_new(
            [
                "https://www.instagram.com/reels/known/",
                "https://www.instagram.com/reels/new-one/",
                "https://www.instagram.com/reels/new-two/",
            ],
            {"https://www.instagram.com/reels/known/"},
        )

        self.assertEqual(urls, [
            "https://www.instagram.com/reels/new-one/",
            "https://www.instagram.com/reels/new-two/",
        ])

    def test_visible_korean_caption_date_is_normalized_without_network_metadata(self) -> None:
        self.assertEqual(
            reels_browser.normalize_upload_time("2026년 8월 20일"),
            "2026-08-20T00:00:00.000Z",
        )

    def test_shortcode_to_media_id_uses_instagram_urlsafe_base64_alphabet(self) -> None:
        convert = getattr(reels_browser, "shortcode_to_media_id", None)
        self.assertIsNotNone(convert)
        self.assertEqual(convert("BA"), "64")
        self.assertEqual(convert("B_"), "127")

    def test_direct_reel_info_fills_exact_metrics_without_overwriting_existing_identity(self) -> None:
        merge = getattr(reels_browser, "merge_direct_reel_metadata", None)
        direct_views = getattr(reels_browser, "exact_view_counts_from_metadata", None)
        self.assertIsNotNone(merge)
        self.assertIsNotNone(direct_views)

        metadata = merge(
            {
                "userId": "123",
                "username": "original",
                "caption": "existing caption",
                "viewCount": 20_000,
                "viewSourceField": "play_count",
                "likeCount": 300,
                "commentCount": 40,
                "repostCount": 5,
            },
            {
                "userId": "123",
                "username": "network-copy",
                "viewCount": 24_585,
                "viewSourceField": "play_count",
                "likeCount": 321,
                "commentCount": 45,
                "repostCount": 6,
            },
        )

        self.assertEqual(metadata["username"], "original")
        self.assertEqual(metadata["caption"], "existing caption")
        self.assertEqual(metadata["likeCount"], 321)
        self.assertEqual(metadata["commentCount"], 45)
        self.assertEqual(metadata["repostCount"], 6)
        self.assertEqual(direct_views("target", metadata), {"target": 24_585})

    def test_generic_reel_metadata_does_not_trust_user_follower_count(self) -> None:
        metadata: dict[str, dict[str, object]] = {}

        collect_reel_metadata({
            "code": "BA",
            "product_type": "clips",
            "user": {
                "pk": "123",
                "username": "example",
                "follower_count": 37_293,
            },
        }, metadata)

        self.assertIsNone(metadata["BA"].get("followerCount"))
        self.assertIsNone(metadata["BA"].get("followerSourceField"))

    def test_visible_reel_date_selector_searches_rendered_main_time_without_network_fallback(self) -> None:
        """The displayed timestamp can be outside the narrow active-Reel scope."""
        script = reels_browser.EXTRACT_VISIBLE_REEL_SCRIPT
        self.assertIn("scope.querySelectorAll('time')", script)
        self.assertIn("document.querySelectorAll('main time')", script)
        self.assertIn("rect.right > 0 && rect.left < innerWidth", script)
        self.assertNotIn("article:published_time", script)

    def test_collected_record_uses_network_upload_time_when_caption_time_is_missing(self) -> None:
        record = build_collected_record(
            {
                "url": "https://www.instagram.com/reels/visible-time-only/",
                "uploadedAt": "",
            },
            {"uploadedAt": "2026-08-20T00:00:00.000Z"},
            "2026-08-25T00:00:00.000Z",
        )
        self.assertEqual(record["uploaded_at"], "2026-08-20T00:00:00.000Z")
        self.assertEqual(record["days_since_upload"], 5)

    def test_reel_video_duration_is_numeric_when_present_and_blank_when_missing(self) -> None:
        base_record = {
            "url": "https://www.instagram.com/reels/duration/",
            "title": "duration test",
        }
        present = build_collected_record(
            base_record,
            reels_browser.metadata_from_media({"video_duration": 12.75}),
            "2026-01-01T00:00:00.000Z",
        )
        missing = build_collected_record(
            base_record,
            reels_browser.metadata_from_media({}),
            "2026-01-01T00:00:00.000Z",
        )

        self.assertEqual(present["video_duration_seconds"], 12.75)
        self.assertEqual(missing["video_duration_seconds"], "")

    def test_reel_media_detection_uses_instagram_clips_signals(self) -> None:
        self.assertTrue(media_is_reel({"product_type": "clips"}))
        self.assertTrue(media_is_reel({"is_clips_media": True}))
        self.assertFalse(media_is_reel({"product_type": "feed", "media_type": 2}))

    def test_visible_search_grid_reel_urls_excludes_hidden_and_non_grid_links(self) -> None:
        select_urls = getattr(reels_browser, "visible_search_grid_reel_urls", None)
        self.assertIsNotNone(select_urls)
        urls = select_urls([
            {"href": "https://www.instagram.com/reels/visible-card/", "visible": True, "gridCard": True},
            {"href": "https://www.instagram.com/reels/hidden-card/", "visible": False, "gridCard": True},
            {"href": "https://www.instagram.com/reels/sidebar-link/", "visible": True, "gridCard": False},
        ])
        self.assertEqual(urls, ["https://www.instagram.com/reels/visible-card/"])

    def test_search_grid_card_visibility_requires_horizontal_and_vertical_viewport_overlap(self) -> None:
        is_visible = getattr(reels_browser, "is_search_grid_card_visible", None)
        self.assertIsNotNone(is_visible)
        self.assertTrue(is_visible({"left": 20, "right": 220, "top": 10, "bottom": 210, "width": 200, "height": 200}, 1200, 800))
        self.assertFalse(is_visible({"left": 1250, "right": 1450, "top": 10, "bottom": 210, "width": 200, "height": 200}, 1200, 800))
        self.assertFalse(is_visible({"left": 20, "right": 220, "top": 810, "bottom": 1010, "width": 200, "height": 200}, 1200, 800))

    def test_hashtag_prefilter_uses_existing_history_but_not_network_upload_dates(self) -> None:
        timestamp = "2026-08-16T12:00:00.000Z"
        duplicate = reel_record(1, "2026-08-16T11:00:00.000Z")
        duplicate["url"] = "https://www.instagram.com/reels/duplicate/"
        expired_history = reel_record(2, "2026-08-01T00:00:00.000Z")
        expired_history["url"] = "https://www.instagram.com/reels/expired/"
        expired_history["uploaded_at"] = "2026-06-01T00:00:00.000Z"
        urls = [
            duplicate["url"],
            "https://www.instagram.com/reels/older/",
            "https://www.instagram.com/reels/unknown/",
            "https://www.instagram.com/reels/newest/",
            "https://www.instagram.com/reels/expired/",
        ]
        metadata = {
            "older": {"uploadedAt": "2026-08-14T12:00:00.000Z"},
            "newest": {"uploadedAt": "2026-08-16T06:00:00.000Z"},
            "expired": {"uploadedAt": "2026-06-01T00:00:00.000Z"},
        }
        result = prefilter_hashtag_reel_urls(urls, metadata, [duplicate, expired_history], 30, timestamp)
        self.assertEqual(result["cooldownSkipped"], 1)
        self.assertEqual(result["uploadAgeSkipped"], 1)
        self.assertEqual(result["ageUnknown"], 3)
        self.assertEqual(result["urls"], [
            "https://www.instagram.com/reels/older/",
            "https://www.instagram.com/reels/unknown/",
            "https://www.instagram.com/reels/newest/",
        ])

    def test_hashtag_prefilter_keeps_a_search_card_despite_caption_mismatch(self) -> None:
        urls = [
            "https://www.instagram.com/reels/matching/",
            "https://www.instagram.com/reels/mismatch/",
            "https://www.instagram.com/reels/no-caption/",
        ]
        metadata = {
            "matching": {"caption": "오늘의 #여자코디"},
            "mismatch": {"caption": "오늘의 #남자코디"},
        }
        result = prefilter_hashtag_reel_urls(
            urls,
            metadata,
            [],
            30,
            "2026-08-16T12:00:00.000Z",
            ["여자코디"],
        )

        self.assertEqual(result["urls"], [
            "https://www.instagram.com/reels/matching/",
            "https://www.instagram.com/reels/mismatch/",
            "https://www.instagram.com/reels/no-caption/",
        ])

    def test_transition_stalls_recycle_on_eighth_attempt(self) -> None:
        consecutive = 0
        for _ in range(7):
            state = next_transition_stall_state(consecutive, False)
            consecutive = state["consecutive"]
            self.assertFalse(state["shouldRecycle"])
        self.assertTrue(next_transition_stall_state(consecutive, False)["shouldRecycle"])
        self.assertEqual(next_transition_stall_state(8, True)["consecutive"], 0)

    def test_recollection_cooldown_and_unchanged_metrics(self) -> None:
        rows: list[dict[str, object]] = []
        fields = list(CSV_FIELDS)
        initial = reel_record(0, "2026-01-01T00:00:00.000Z")
        initial.update({"view_count": "1000", "like_count": "100", "comment_count": "20", "repost_count": "3", "follower_count": "1000"})
        integrate_collected_record(rows, fields, initial)
        skipped_record = reel_record(0, "2026-01-01T05:59:59.000Z")
        skipped = collected_record_cooldown(rows[0], fields, skipped_record)
        self.assertEqual(skipped["cooldownLabel"], "6시간")
        refreshed = reel_record(0, "2026-01-01T06:00:00.000Z")
        refreshed.update({"view_count": "1200", "like_count": "100", "comment_count": "20", "repost_count": "3", "follower_count": "1000"})
        result = integrate_collected_record(rows, fields, refreshed)
        self.assertEqual(result["label"], "2nd collect")
        self.assertEqual(rows[0]["2nd collect_view_count"], "1200")
        self.assertEqual(rows[0]["2nd collect_like_count"], "100")
        self.assertEqual(rows[0]["2nd collect_follower_count"], "1000")
        self.assertNotIn("reaction_rate", rows[0])

    def test_monthly_cooldown_clamps_end_of_month(self) -> None:
        january_31 = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
        available = next_recollect_available_at(january_31, 6)
        self.assertEqual(available.isoformat(), "2026-02-28T12:00:00+00:00")
        self.assertEqual(collection_label(23), "23rd collect")

    def test_exact_follower_refresh_replaces_a_larger_legacy_estimate(self) -> None:
        rows: list[dict[str, object]] = []
        fields = list(CSV_FIELDS)
        initial = reel_record(0, "2026-01-01T00:00:00.000Z")
        initial["follower_count"] = "52120000"
        refreshed = reel_record(0, "2026-01-01T06:00:00.000Z")
        refreshed["follower_count"] = "48000"
        integrate_collected_record(rows, fields, initial)
        integrate_collected_record(rows, fields, refreshed)
        self.assertEqual(rows[0]["2nd collect_follower_count"], "48000")

    def test_anonymous_refresh_preserves_static_metadata_and_blanks_follower(self) -> None:
        """Catch a refresh that overwrites BGM/static fields or reuses followers."""
        existing = reel_record(1, "2026-01-01T00:00:00.000Z")
        existing.update({
            "title": "original caption",
            "hashtags": "#original",
            "audio_name": "original artist · original audio",
            "location_name": "original location",
            "ad": "true",
            "uploaded_at": "2025-12-20T00:00:00.000Z",
            "view_count": 1_000,
            "like_count": 100,
            "comment_count": 20,
            "repost_count": 3,
            "follower_count": 5_000,
        })
        observed = reel_record(1, "2026-01-03T00:00:00.000Z")
        observed.update({
            "title": "new rendered caption",
            "hashtags": "#new",
            "audio_name": "",
            "location_name": "new location",
            "ad": "false",
            "uploaded_at": "2026-01-02T00:00:00.000Z",
            "video_duration_seconds": 13.25,
            "view_count": 1_100,
            "like_count": 111,
            "comment_count": 25,
            "repost_count": 4,
            "follower_count": 5_999,
        })

        unavailable_observed = {**observed, "follower_count": ""}
        refreshed = reels_browser.build_anonymous_refresh_record(existing, unavailable_observed)

        self.assertEqual(refreshed["collected_at"], "2026-01-03T00:00:00.000Z")
        self.assertEqual(refreshed["title"], "original caption")
        self.assertEqual(refreshed["hashtags"], "#original")
        self.assertEqual(refreshed["audio_name"], "original artist · original audio")
        self.assertEqual(refreshed["location_name"], "original location")
        self.assertEqual(refreshed["ad"], "true")
        self.assertEqual(refreshed["uploaded_at"], "2025-12-20T00:00:00.000Z")
        self.assertEqual(refreshed["video_duration_seconds"], 13.25)
        self.assertEqual(refreshed["days_since_upload"], 14)
        self.assertEqual(refreshed["view_count"], 1_100)
        self.assertEqual(refreshed["like_count"], 111)
        self.assertEqual(refreshed["comment_count"], 25)
        self.assertEqual(refreshed["repost_count"], 4)
        self.assertEqual(refreshed["follower_count"], "")
        successful_refresh = reels_browser.build_anonymous_refresh_record(existing, observed)
        self.assertEqual(successful_refresh["follower_count"], 5_999)
        zero_follower_refresh = reels_browser.build_anonymous_refresh_record(existing, {**observed, "follower_count": 0})
        self.assertEqual(zero_follower_refresh["follower_count"], 0)

    def test_anonymous_history_refresh_creates_elapsed_days_and_metric_delta_in_wide_export(self) -> None:
        """Catch a no-login refresh that creates a new initial row instead of a snapshot."""
        initial = reel_record(1, "2026-01-01T00:00:00.000Z")
        initial.update({
            "audio_name": "original artist · original audio",
            "uploaded_at": "2025-12-20T00:00:00.000Z",
            "view_count": 1_000,
            "like_count": 100,
            "comment_count": 20,
            "repost_count": 3,
            "follower_count": 5_000,
        })
        observed = reel_record(1, "2026-01-03T00:00:00.000Z")
        observed.update({
            "view_count": 1_100,
            "like_count": 110,
            "comment_count": 25,
            "repost_count": 4,
            "follower_count": "",
        })

        refreshed = reels_browser.build_anonymous_refresh_from_history([initial], observed)
        self.assertIsNotNone(refreshed)
        fields, rows = reels_browser.long_rows_to_wide([initial, refreshed])
        matrix = [fields, [str(rows[0].get(field, "") if rows[0].get(field, "") is not None else "") for field in fields]]
        projected = xlsx_exporter._xlsx_project_rows("reels", matrix)
        displayed = dict(zip(projected[0], projected[1]))

        self.assertEqual(rows[0]["audio_name"], "original artist · original audio")
        self.assertEqual(rows[0]["2nd collect_view_count"], "1100")
        self.assertEqual(rows[0]["2nd collect_follower_count"], "")
        self.assertEqual(displayed["2nd collect_days_since_previous"], "+2day")
        self.assertEqual(displayed["2nd collect_view_count"], "1,100(+100)")

    def test_anonymous_refresh_uses_target_play_count_and_trusted_follower_source(self) -> None:
        """Catch use of a rendered view label or an untrusted follower response."""
        initial = reel_record(1, "2026-01-01T00:00:00.000Z")
        observed = reel_record(1, "2026-01-03T00:00:00.000Z")
        observed.update({"view_count": 9_999, "follower_count": 8_888})

        untrusted = reels_browser.build_anonymous_refresh_from_exact_metrics(
            [initial], observed, "test_1", {"test_1": 1_101},
            {"status": "success", "followerCount": 8_888, "sourceField": "profile.followers"},
        )
        trusted = reels_browser.build_anonymous_refresh_from_exact_metrics(
            [initial], observed, "test_1", {"test_1": 1_101},
            {"status": "success", "followerCount": 5_001, "sourceField": "edge_followed_by.count"},
        )

        self.assertIsNotNone(untrusted)
        self.assertIsNotNone(trusted)
        self.assertEqual(untrusted["view_count"], 1_101)
        self.assertEqual(untrusted["follower_count"], "")
        self.assertEqual(trusted["follower_count"], 5_001)

    def test_anonymous_history_refresh_keeps_the_prior_url_identity_for_a_legacy_variant(self) -> None:
        """Catch a canonical history match that is appended under a different raw URL."""
        initial = reel_record(1, "2026-01-01T00:00:00.000Z")
        initial.update({
            "url": "https://www.instagram.com/reel/test_1/?legacy=1",
            "view_count": 1_000,
        })
        observed = reel_record(1, "2026-01-03T00:00:00.000Z")
        observed["view_count"] = 1_100

        refreshed = reels_browser.build_anonymous_refresh_from_history([initial], observed)
        self.assertIsNotNone(refreshed)
        fields, rows = reels_browser.long_rows_to_wide([initial, refreshed])

        self.assertEqual(refreshed["url"], "https://www.instagram.com/reel/test_1/?legacy=1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["2nd collect_view_count"], "1100")

    def test_network_media_without_a_view_metric_rejects_the_rendered_compact_label(self) -> None:
        """A compact label has lost digits and must not be stored as an exact view count."""
        metadata: dict[str, dict[str, object]] = {}
        collect_reel_metadata({
            "code": "DA55rVCNIrp",
            "product_type": "clips",
            "like_count": 3_262,
            "comment_count": 7,
            "media_repost_count": 9,
            "view_state_item_type": 128,
            "user": {"pk": "52988919621", "username": "kzby.kr"},
        }, metadata)

        output = StringIO()
        with redirect_stdout(output):
            collected = build_collected_record(
                {
                    "url": "https://www.instagram.com/reels/DA55rVCNIrp/",
                    "title": "sample",
                    "viewText": "1.6만",
                },
                metadata["DA55rVCNIrp"],
                "2026-08-24T00:00:00.000Z",
            )

        self.assertIsNone(collected["view_count"])
        self.assertEqual(output.getvalue(), "")

    def test_profile_reels_play_count_is_stored_as_the_exact_view_count(self) -> None:
        metadata: dict[str, dict[str, object]] = {}
        collect_reel_metadata({
            "data": {
                "fetch__XDTUserDict": {
                    "clips_connection": {
                        "edges": [{
                            "node": {
                                "media": {
                                    "code": "Db2uAL7xbRZ",
                                    "product_type": "clips",
                                    "play_count": 24_585,
                                    "like_count": 564,
                                    "comment_count": 15,
                                }
                            }
                        }]
                    }
                }
            }
        }, metadata)

        output = StringIO()
        with redirect_stdout(output):
            collected = build_collected_record(
                {
                    "url": "https://www.instagram.com/reels/Db2uAL7xbRZ/",
                    "viewText": "2.4만",
                },
                metadata["Db2uAL7xbRZ"],
                "2026-08-24T00:00:00.000Z",
            )

        self.assertEqual(collected["view_count"], 24_585)
        self.assertEqual(output.getvalue(), "")

    def test_reel_core_data_rejects_a_missing_exact_view_count(self) -> None:
        self.assertFalse(has_complete_reel_core_data({
            "url": "https://www.instagram.com/reels/DA55rVCNIrp/",
            "user_id": "52988919621",
            "username": "kzby.kr",
            "uploaded_at": "2024-10-09T13:16:30.000Z",
            "view_count": None,
            "like_count": 3_262,
            "comment_count": 7,
            "repost_count": 9,
            "ad": "false",
        }))

    def test_reel_core_data_rejects_a_missing_exact_follower_count(self) -> None:
        self.assertFalse(has_complete_reel_core_data({
            "url": "https://www.instagram.com/reels/DA55rVCNIrp/",
            "user_id": "52988919621",
            "username": "kzby.kr",
            "uploaded_at": "2024-10-09T13:16:30.000Z",
            "view_count": 24_585,
            "like_count": 3_262,
            "comment_count": 7,
            "repost_count": 9,
            "follower_count": "",
            "ad": "false",
        }))

    def test_exact_metric_results_are_applied_together_before_a_reel_is_saved(self) -> None:
        apply_results = getattr(reels_browser, "apply_exact_metric_results", None)
        self.assertIsNotNone(apply_results)
        record = {
            "view_count": None,
            "like_count": 564,
            "comment_count": 15,
            "repost_count": 18,
            "follower_count": "",
        }

        result = apply_results(
            record,
            "Db2uAL7xbRZ",
            {"Db2uAL7xbRZ": 24_585},
            {
                "status": "success",
                "followerCount": 37_293,
                "sourceField": "edge_followed_by.count",
            },
        )

        self.assertEqual(result, {"status": "success", "error": ""})
        self.assertEqual(record["view_count"], 24_585)
        self.assertEqual(record["follower_count"], 37_293)

    def test_exact_metric_results_accept_direct_user_follower_count(self) -> None:
        record = {
            "view_count": None,
            "like_count": 564,
            "comment_count": 15,
            "repost_count": 18,
            "follower_count": "",
        }

        result = reels_browser.apply_exact_metric_results(
            record,
            "Db2uAL7xbRZ",
            {"Db2uAL7xbRZ": 24_585},
            {
                "status": "success",
                "followerCount": 37_293,
                "sourceField": "follower_count",
            },
        )

        self.assertEqual(result, {"status": "success", "error": ""})
        self.assertEqual(record["follower_count"], 37_293)

    def test_incomplete_exact_metric_results_do_not_partially_modify_a_reel(self) -> None:
        apply_results = getattr(reels_browser, "apply_exact_metric_results", None)
        self.assertIsNotNone(apply_results)
        record = {
            "view_count": None,
            "like_count": 564,
            "comment_count": 15,
            "repost_count": 18,
            "follower_count": "",
        }

        result = apply_results(
            record,
            "Db2uAL7xbRZ",
            {},
            {
                "status": "success",
                "followerCount": 37_293,
                "sourceField": "edge_followed_by.count",
            },
        )

        self.assertEqual(result["status"], "exact_view_unavailable")
        self.assertIsNone(record["view_count"])
        self.assertEqual(record["follower_count"], "")

    def test_missing_network_engagement_metric_rejects_the_exact_metric_bundle(self) -> None:
        record = {
            "view_count": None,
            "like_count": None,
            "comment_count": 15,
            "repost_count": 18,
            "follower_count": "",
        }

        result = reels_browser.apply_exact_metric_results(
            record,
            "Db2uAL7xbRZ",
            {"Db2uAL7xbRZ": 24_585},
            {
                "status": "success",
                "followerCount": 37_293,
                "sourceField": "edge_followed_by.count",
            },
        )

        self.assertEqual(result["status"], "exact_like_count_unavailable")
        self.assertIsNone(record["view_count"])
        self.assertEqual(record["follower_count"], "")

    def test_rendered_compact_engagement_labels_are_not_stored_as_exact_metrics(self) -> None:
        collected = build_collected_record({
            "url": "https://www.instagram.com/reels/target/",
            "likeText": "1.6만",
            "commentText": "1.2K",
            "repostText": "3.4K",
        })

        self.assertIsNone(collected["like_count"])
        self.assertIsNone(collected["comment_count"])
        self.assertIsNone(collected["repost_count"])

    def test_reel_core_data_rejects_compact_metric_strings(self) -> None:
        self.assertFalse(has_complete_reel_core_data({
            "url": "https://www.instagram.com/reels/target/",
            "user_id": "1",
            "username": "example",
            "uploaded_at": "2026-08-24T00:00:00.000Z",
            "view_count": 24_585,
            "like_count": "1.6만",
            "comment_count": 15,
            "repost_count": 18,
            "follower_count": 37_293,
            "ad": "false",
        }))

    def test_direct_reel_info_diagnostic_explains_a_missing_metric_after_a_successful_response(self) -> None:
        message = reels_browser.direct_reel_info_diagnostic_message(
            {"status": "success", "error": ""},
            "repost_count",
        )

        self.assertEqual(
            message,
            "Direct Reel info returned HTTP 200 but did not include an exact raw repost_count.",
        )


class CollectorAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_cooldown_skips_nearby_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = await LongReelStore.create(Path(directory) / "reels_rows.csv")
            await store.append(reel_record(1, "2026-08-26T00:00:00Z"))
            result = await store.append(reel_record(1, "2026-08-26T00:30:00Z"))
            self.assertTrue(result.get("skipped"))

    async def test_disabled_cooldown_accepts_due_fashion_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = await LongReelStore.create(
                Path(directory) / "reels_rows.csv",
                disable_recollect_cooldown=True,
            )
            await store.append(reel_record(1, "2026-08-26T00:00:00Z"))
            result = await store.append(reel_record(1, "2026-08-26T00:30:00Z"))
            self.assertFalse(result.get("skipped"))

    async def test_hashtag_rediscovery_wait_is_interrupted_by_stop_request(self) -> None:
        wait_for_stop = getattr(reels_browser, "wait_for_stop_or_timeout", None)
        request_stop = getattr(reels_browser, "request_stop_threadsafe", None)
        self.assertIsNotNone(wait_for_stop)
        self.assertIsNotNone(request_stop)
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def stop(_source: str) -> bool:
            stop_event.set()
            return True

        worker = threading.Thread(
            target=lambda: request_stop(loop, stop, "test stop"),
            daemon=True,
        )
        started = time.monotonic()
        worker.start()

        stopped = await wait_for_stop(stop_event, 0.5)
        worker.join(timeout=1)

        self.assertTrue(stopped)
        self.assertLess(time.monotonic() - started, 0.25)

    async def test_direct_follower_count_skips_profile_lookup_and_is_cached(self) -> None:
        resolve = getattr(reels_browser, "resolve_follower_result", None)
        self.assertIsNotNone(resolve)
        profile_lookup_calls = 0

        async def profile_lookup() -> dict[str, object]:
            nonlocal profile_lookup_calls
            profile_lookup_calls += 1
            return {
                "status": "success",
                "followerCount": 99_999,
                "sourceField": "edge_followed_by.count",
            }

        cache: dict[str, dict[str, object]] = {}
        result = await resolve(
            {
                "followerCount": 37_293,
                "followerSourceField": "follower_count",
            },
            "123",
            cache,
            profile_lookup,
        )

        self.assertEqual(profile_lookup_calls, 0)
        self.assertEqual(result, {
            "status": "success",
            "followerCount": 37_293,
            "error": "",
            "source": "instagram_web",
            "sourceField": "follower_count",
        })
        self.assertEqual(cache["123"], result)

        cached = await resolve({}, "123", cache, profile_lookup)

        self.assertEqual(profile_lookup_calls, 0)
        self.assertEqual(cached, result)

    async def test_missing_direct_follower_count_uses_profile_lookup(self) -> None:
        resolve = getattr(reels_browser, "resolve_follower_result", None)
        self.assertIsNotNone(resolve)

        async def profile_lookup() -> dict[str, object]:
            return {
                "status": "success",
                "followerCount": 37_293,
                "error": "",
                "source": "instagram_web",
                "sourceField": "edge_followed_by.count",
            }

        result = await resolve(
            {},
            "123",
            {},
            profile_lookup,
        )

        self.assertEqual(result["sourceField"], "edge_followed_by.count")

    async def test_direct_reel_info_request_maps_exact_integer_metrics(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.media_id = ""

            async def evaluate(self, _script: str, media_id: str) -> dict[str, object]:
                self.media_id = media_id
                return {
                    "status": 200,
                    "text": json.dumps({
                        "items": [{
                            "code": "BA",
                            "product_type": "clips",
                            "user": {
                                "pk": "123",
                                "username": "example",
                                "follower_count": 37_293,
                            },
                            "caption": {"text": "test #reel"},
                            "taken_at": 1_774_915_200,
                            "play_count": 24_585,
                            "like_count": 321,
                            "comment_count": 45,
                            "media_repost_count": 6,
                        }]
                    }),
                }

        request = getattr(reels_browser, "request_reel_info_metadata", None)
        self.assertIsNotNone(request)
        page = Page()
        metadata = await request(page, "BA")

        self.assertEqual(page.media_id, "64")
        self.assertEqual(metadata["viewCount"], 24_585)
        self.assertEqual(metadata["viewSourceField"], "play_count")
        self.assertEqual(metadata["likeCount"], 321)
        self.assertEqual(metadata["commentCount"], 45)
        self.assertEqual(metadata["repostCount"], 6)
        self.assertEqual(metadata["followerCount"], 37_293)
        self.assertEqual(metadata["followerSourceField"], "follower_count")
        self.assertEqual(metadata["uploadedAt"], "2026-03-31T00:00:00.000Z")

    async def test_direct_reel_info_does_not_trust_owner_follower_count(self) -> None:
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                return {
                    "status": 200,
                    "text": json.dumps({
                        "items": [{
                            "code": "BA",
                            "product_type": "clips",
                            "owner": {
                                "pk": "123",
                                "username": "example",
                                "follower_count": 37_293,
                            },
                        }]
                    }),
                }

        metadata = await reels_browser.request_reel_info_metadata(Page(), "BA")

        self.assertIsNone(metadata.get("followerCount"))
        self.assertIsNone(metadata.get("followerSourceField"))

    async def test_direct_reel_info_request_returns_empty_metadata_on_http_error(self) -> None:
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                return {
                    "status": 403,
                    "text": json.dumps({
                        "items": [{
                            "code": "BA",
                            "product_type": "clips",
                            "play_count": 99_999,
                            "like_count": 999,
                            "comment_count": 99,
                            "media_repost_count": 9,
                        }]
                    }),
                }

        metadata = await reels_browser.request_reel_info_metadata(Page(), "BA")

        self.assertEqual(metadata, {})

    async def test_direct_reel_info_request_reports_the_http_failure_reason(self) -> None:
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                return {"status": 403, "text": "forbidden"}

        diagnostic: dict[str, str] = {}
        metadata = await reels_browser.request_reel_info_metadata(Page(), "BA", diagnostic)

        self.assertEqual(metadata, {})
        self.assertEqual(diagnostic, {
            "status": "http_error",
            "error": "Direct Reel info returned HTTP 403.",
        })

    async def test_direct_reel_info_request_rejects_response_without_target_shortcode(self) -> None:
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                return {
                    "status": 200,
                    "text": json.dumps({
                        "items": [{
                            "product_type": "clips",
                            "play_count": 99_999,
                            "like_count": 999,
                            "comment_count": 99,
                            "media_repost_count": 9,
                        }]
                    }),
                }

        metadata = await reels_browser.request_reel_info_metadata(Page(), "BA")

        self.assertEqual(metadata, {})

    async def test_direct_reel_info_request_accepts_matching_media_id_when_shortcode_is_omitted(self) -> None:
        """The direct endpoint can identify the target by pk even without code."""
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                return {
                    "status": 200,
                    "text": json.dumps({
                        "items": [{
                            "pk": "64",
                            "product_type": "clips",
                            "play_count": 24_585,
                            "like_count": 321,
                            "comment_count": 45,
                            "media_repost_count": 6,
                        }]
                    }),
                }

        metadata = await reels_browser.request_reel_info_metadata(Page(), "BA")

        self.assertEqual(metadata["viewCount"], 24_585)
        self.assertEqual(metadata["repostCount"], 6)

    async def test_direct_reel_info_request_rejects_a_conflicting_shortcode_even_when_media_id_matches(self) -> None:
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                return {
                    "status": 200,
                    "text": json.dumps({
                        "items": [{
                            "code": "BB",
                            "pk": "64",
                            "product_type": "clips",
                            "play_count": 24_585,
                            "like_count": 321,
                            "comment_count": 45,
                            "media_repost_count": 6,
                        }]
                    }),
                }

        metadata = await reels_browser.request_reel_info_metadata(Page(), "BA")

        self.assertEqual(metadata, {})

    async def test_direct_reel_info_request_returns_empty_metadata_when_fetch_fails(self) -> None:
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                raise RuntimeError("fetch failed")

        try:
            metadata = await reels_browser.request_reel_info_metadata(Page(), "BA")
        except RuntimeError as error:
            self.fail(f"Direct fetch errors must fall back instead of escaping: {error}")

        self.assertEqual(metadata, {})

    async def test_direct_reel_info_request_times_out_to_empty_metadata(self) -> None:
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                await asyncio.sleep(0.05)
                return {
                    "status": 200,
                    "text": json.dumps({
                        "items": [{
                            "code": "BA",
                            "product_type": "clips",
                            "play_count": 99_999,
                            "like_count": 999,
                            "comment_count": 99,
                            "media_repost_count": 9,
                        }]
                    }),
                }

        with patch.object(reels_browser, "DIRECT_REEL_INFO_TIMEOUT_SECONDS", 0.01, create=True):
            metadata = await reels_browser.request_reel_info_metadata(Page(), "BA")

        self.assertEqual(metadata, {})

    async def test_complete_direct_reel_info_skips_slow_navigation_fallbacks(self) -> None:
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                return {
                    "status": 200,
                    "text": json.dumps({
                        "items": [{
                            "code": "BA",
                            "product_type": "clips",
                            "user": {"pk": "123", "username": "example"},
                            "taken_at": 1_774_915_200,
                            "play_count": 24_585,
                            "like_count": 321,
                            "comment_count": 45,
                            "media_repost_count": 6,
                        }]
                    }),
                }

        async def slow_page() -> object:
            raise AssertionError("Complete direct metrics must not create the slow fallback page")

        resolve = getattr(reels_browser, "resolve_exact_reel_metrics", None)
        self.assertIsNotNone(resolve)
        metadata, view_counts = await resolve(Page(), "BA", {}, slow_page)

        self.assertEqual(metadata["likeCount"], 321)
        self.assertEqual(metadata["commentCount"], 45)
        self.assertEqual(metadata["repostCount"], 6)
        self.assertEqual(view_counts, {"BA": 24_585})

    async def test_direct_metric_retry_recovers_missing_first_response(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.requests = 0
                self.waits: list[int] = []

            async def wait_for_timeout(self, milliseconds: int) -> None:
                self.waits.append(milliseconds)

            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                self.requests += 1
                media: dict[str, object] = {
                    "code": "BA",
                    "product_type": "clips",
                    "user": {"pk": "123", "username": "example"},
                    "play_count": 24_585,
                    "like_count": 321,
                    "comment_count": 45,
                }
                if self.requests == 2:
                    media["media_repost_count"] = 6
                return {"status": 200, "text": json.dumps({"items": [media]})}

        async def slow_page() -> object:
            raise AssertionError("A successful direct retry must not open the fallback page")

        page = Page()
        metadata, view_counts = await reels_browser.resolve_exact_reel_metrics(
            page,
            "BA",
            {},
            slow_page,
            max_direct_attempts=3,
            retry_delay_seconds=0.5,
        )

        self.assertEqual(page.requests, 2)
        self.assertEqual(page.waits, [500])
        self.assertEqual(metadata["repostCount"], 6)
        self.assertEqual(view_counts, {"BA": 24_585})

    async def test_follower_web_lookup_retries_transient_errors_three_times(self) -> None:
        class Context:
            async def new_page(self) -> "Page":
                return Page(self)

        class Page:
            def __init__(self, context: Context) -> None:
                self.context = context

            def is_closed(self) -> bool:
                return False

            async def close(self) -> None:
                return None

        calls = 0

        async def flaky_lookup(_page: object, _username: str) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls < 3:
                return {"status": "web_error", "error": "temporary", "source": "test"}
            return {
                "status": "success",
                "followerCount": 1_234,
                "sourceField": "edge_followed_by.count",
            }

        lookup = reels_browser.SequentialWebFollowerLookup(
            Page(Context()),
            max_attempts=3,
            retry_delay_seconds=0,
        )
        with patch.object(reels_browser, "request_web_follower_count", new=flaky_lookup):
            result = await lookup({"username": "example", "userId": "123"})

        self.assertEqual(calls, 3)
        self.assertEqual(result["status"], "success")

    async def test_prefetched_direct_reel_info_is_reused_without_a_second_request(self) -> None:
        class Page:
            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                raise AssertionError("Prefetched direct metadata must not be fetched twice")

        async def slow_page() -> object:
            raise AssertionError("Complete prefetched metrics must not create the slow fallback page")

        direct_metadata = {
            "userId": "123",
            "username": "example",
            "uploadedAt": "2026-03-31T00:00:00.000Z",
            "viewCount": 24_585,
            "viewSourceField": "play_count",
            "likeCount": 321,
            "commentCount": 45,
            "repostCount": 6,
        }
        try:
            metadata, view_counts = await reels_browser.resolve_exact_reel_metrics(
                Page(),
                "BA",
                {},
                slow_page,
                direct_metadata=direct_metadata,
            )
        except TypeError as error:
            self.fail(f"Resolver must accept prefetched direct metadata: {error}")

        self.assertEqual(metadata["uploadedAt"], "2026-03-31T00:00:00.000Z")
        self.assertEqual(view_counts, {"BA": 24_585})

    async def test_initial_direct_reel_info_waits_before_fetching_metrics(self) -> None:
        """The first direct API request must run only after its configured detail-page wait."""
        class Page:
            def __init__(self) -> None:
                self.waits: list[int] = []

            async def wait_for_timeout(self, milliseconds: int) -> None:
                self.waits.append(milliseconds)

            async def evaluate(self, _script: str, _media_id: str) -> dict[str, object]:
                if self.waits != [2_000]:
                    return {"status": 200, "text": json.dumps({"items": []})}
                return {
                    "status": 200,
                    "text": json.dumps({
                        "items": [{
                            "code": "BA",
                            "product_type": "clips",
                            "play_count": 24_585,
                            "like_count": 321,
                            "comment_count": 45,
                            "media_repost_count": 6,
                        }]
                    }),
                }

        request = getattr(reels_browser, "request_initial_reel_info_metadata", None)
        self.assertIsNotNone(request)
        metadata = await request(Page(), "BA", settle_milliseconds=2_000)

        self.assertEqual(metadata["repostCount"], 6)

    async def test_hashtag_search_rejects_metadata_only_reels_without_a_visible_card(self) -> None:
        class Mouse:
            async def wheel(self, _x: int, _y: int) -> None:
                return None

        class Locator:
            async def all_text_contents(self) -> list[str]:
                return []

        metadata: dict[str, dict[str, object]] = {}

        class Page:
            url = ""
            mouse = Mouse()

            async def goto(self, url: str, **_kwargs: object) -> None:
                self.url = url
                metadata["clipCode"] = {"isReel": True}

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

            async def eval_on_selector_all(self, _selector: str, _script: str) -> list[dict[str, object]]:
                return []

            def locator(self, _selector: str) -> Locator:
                return Locator()

        with self.assertRaisesRegex(reels_browser.CrawlerAccessError, "릴스 URL을 찾지 못했습니다"):
            await collect_hashtag_reel_urls(Page(), ["패션"], 1, metadata)

    async def test_unlimited_hashtag_search_returns_every_discovered_candidate(self) -> None:
        class Mouse:
            async def wheel(self, _x: int, _y: int) -> None:
                return None

        class Locator:
            async def all_text_contents(self) -> list[str]:
                return []

        class Page:
            url = ""
            mouse = Mouse()

            async def goto(self, url: str, **_kwargs: object) -> None:
                self.url = url

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

            async def eval_on_selector_all(self, _selector: str, _script: str) -> list[dict[str, object]]:
                return [
                    {"href": "https://www.instagram.com/p/firstCode/", "visible": True, "gridCard": True},
                    {"href": "https://www.instagram.com/reels/secondCode/", "visible": True, "gridCard": True},
                    {"href": "https://www.instagram.com/p/thirdCode/", "visible": True, "gridCard": True},
                ]

            def locator(self, _selector: str) -> Locator:
                return Locator()

        urls = await collect_hashtag_reel_urls(Page(), ["패션"], 0, {})
        self.assertEqual(urls, [
            "https://www.instagram.com/reels/firstCode/",
            "https://www.instagram.com/reels/secondCode/",
            "https://www.instagram.com/reels/thirdCode/",
        ])

    async def test_hashtag_discovery_stops_before_opening_another_tag(self) -> None:
        class Page:
            async def goto(self, *_args: object, **_kwargs: object) -> None:
                raise AssertionError("A stop request must prevent hashtag navigation")

        urls = await collect_hashtag_reel_urls(
            Page(),
            ["여성코디", "ootd"],
            0,
            {},
            should_stop=lambda: True,
        )

        self.assertEqual(urls, [])

    async def test_profile_reels_network_play_count_is_used_instead_of_the_compact_label(self) -> None:
        class Response:
            status = 200
            url = "https://www.instagram.com/graphql/query"
            headers = {"content-type": "application/json"}

            async def text(self) -> str:
                return json.dumps({
                    "data": {
                        "fetch__XDTUserDict": {
                            "clips_connection": {
                                "edges": [{
                                    "node": {
                                        "media": {
                                            "code": "target",
                                            "product_type": "clips",
                                            "play_count": 24_585,
                                        }
                                    }
                                }]
                            }
                        }
                    }
                })

        class Mouse:
            async def wheel(self, _x: int, _y: int) -> None:
                raise AssertionError("The target Reel's view label was already available")

        class Page:
            url = ""
            mouse = Mouse()

            def __init__(self) -> None:
                self.response_handler: object | None = None

            def on(self, event: str, handler: object) -> None:
                if event == "response":
                    self.response_handler = handler

            def remove_listener(self, event: str, handler: object) -> None:
                if event == "response" and self.response_handler is handler:
                    self.response_handler = None

            async def goto(self, url: str, **_kwargs: object) -> Response:
                self.url = url
                response = Response()
                if callable(self.response_handler):
                    self.response_handler(response)
                return response

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0)

            async def evaluate(self, _script: str, shortcodes: list[str]) -> dict[str, str]:
                if shortcodes != ["target"]:
                    raise AssertionError(f"Unexpected target Reels: {shortcodes}")
                return {"target": "2.4만"}

        lookup = getattr(reels_browser, "read_profile_reel_view_counts", None)
        self.assertIsNotNone(lookup)
        output = StringIO()
        with redirect_stdout(output):
            counts = await lookup(Page(), "example", {"target"})

        self.assertEqual(counts, {"target": 24_585})
        self.assertEqual(output.getvalue(), "")

    async def test_profile_reels_retries_a_fresh_navigation_when_play_count_is_missing_first(self) -> None:
        """Catch a view lookup that gives up before the retry response provides play_count."""
        class Response:
            status = 200
            url = "https://www.instagram.com/graphql/query"
            headers = {"content-type": "application/json"}

            async def text(self) -> str:
                return json.dumps({
                    "data": {"fetch__XDTUserDict": {"clips_connection": {"edges": [{
                        "node": {"media": {
                            "code": "target", "product_type": "clips", "play_count": 24_585,
                        }}
                    }]}}}
                })

        class Mouse:
            async def wheel(self, _x: int, _y: int) -> None:
                raise AssertionError("A one-item scan should not need to scroll")

        class Page:
            url = ""
            mouse = Mouse()

            def __init__(self) -> None:
                self.goto_count = 0
                self.response_handler: object | None = None

            def on(self, event: str, handler: object) -> None:
                if event == "response":
                    self.response_handler = handler

            def remove_listener(self, event: str, handler: object) -> None:
                if event == "response" and self.response_handler is handler:
                    self.response_handler = None

            async def goto(self, url: str, **_kwargs: object) -> Response:
                self.goto_count += 1
                self.url = url
                if self.goto_count == 2 and callable(self.response_handler):
                    self.response_handler(Response())
                return Response()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0)

        page = Page()
        original = (
            reels_browser.PROFILE_REEL_VIEW_SETTLE_MILLISECONDS,
            reels_browser.PROFILE_REEL_VIEW_SCROLL_ATTEMPTS,
            reels_browser.PROFILE_REEL_VIEW_SCROLL_MILLISECONDS,
            getattr(reels_browser, "PROFILE_REEL_VIEW_PAGE_ATTEMPTS", None),
            getattr(reels_browser, "PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS", None),
        )
        reels_browser.PROFILE_REEL_VIEW_SETTLE_MILLISECONDS = 0
        reels_browser.PROFILE_REEL_VIEW_SCROLL_ATTEMPTS = 1
        reels_browser.PROFILE_REEL_VIEW_SCROLL_MILLISECONDS = 0
        reels_browser.PROFILE_REEL_VIEW_PAGE_ATTEMPTS = 2
        reels_browser.PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS = 0
        try:
            counts = await reels_browser.read_profile_reel_view_counts(page, "example", {"target"})
        finally:
            (
                reels_browser.PROFILE_REEL_VIEW_SETTLE_MILLISECONDS,
                reels_browser.PROFILE_REEL_VIEW_SCROLL_ATTEMPTS,
                reels_browser.PROFILE_REEL_VIEW_SCROLL_MILLISECONDS,
                page_attempts,
                retry_delay,
            ) = original
            if page_attempts is None:
                del reels_browser.PROFILE_REEL_VIEW_PAGE_ATTEMPTS
            else:
                reels_browser.PROFILE_REEL_VIEW_PAGE_ATTEMPTS = page_attempts
            if retry_delay is None:
                del reels_browser.PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS
            else:
                reels_browser.PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS = retry_delay

        self.assertEqual(page.goto_count, 2)
        self.assertEqual(counts, {"target": 24_585})

    async def test_profile_reels_retries_when_an_unrelated_json_response_body_stalls(self) -> None:
        """Catch profile polling that waits forever for a non-target JSON body."""
        class StalledResponse:
            url = "https://www.instagram.com/graphql/query"
            headers = {"content-type": "application/json"}
            status = 200

            async def text(self) -> str:
                await asyncio.Event().wait()
                return "{}"

        class ExactResponse:
            url = "https://www.instagram.com/graphql/query"
            headers = {"content-type": "application/json"}
            status = 200

            async def text(self) -> str:
                return json.dumps({
                    "data": {"fetch__XDTUserDict": {"clips_connection": {"edges": [{
                        "node": {"media": {
                            "code": "target", "product_type": "clips", "play_count": 24_585,
                        }}
                    }]}}}
                })

        class Mouse:
            async def wheel(self, _x: int, _y: int) -> None:
                raise AssertionError("A one-item scan should not need to scroll")

        class Page:
            url = ""
            mouse = Mouse()

            def __init__(self) -> None:
                self.goto_count = 0
                self.response_handler: object | None = None

            def on(self, event: str, handler: object) -> None:
                if event == "response":
                    self.response_handler = handler

            def remove_listener(self, event: str, handler: object) -> None:
                if event == "response" and self.response_handler is handler:
                    self.response_handler = None

            async def goto(self, url: str, **_kwargs: object) -> StalledResponse:
                self.goto_count += 1
                self.url = url
                if callable(self.response_handler):
                    self.response_handler(StalledResponse() if self.goto_count == 1 else ExactResponse())
                return StalledResponse()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0)

        page = Page()
        original = (
            reels_browser.PROFILE_REEL_VIEW_SETTLE_MILLISECONDS,
            reels_browser.PROFILE_REEL_VIEW_SCROLL_ATTEMPTS,
            reels_browser.PROFILE_REEL_VIEW_SCROLL_MILLISECONDS,
            reels_browser.PROFILE_REEL_VIEW_PAGE_ATTEMPTS,
            reels_browser.PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS,
        )
        reels_browser.PROFILE_REEL_VIEW_SETTLE_MILLISECONDS = 0
        reels_browser.PROFILE_REEL_VIEW_SCROLL_ATTEMPTS = 1
        reels_browser.PROFILE_REEL_VIEW_SCROLL_MILLISECONDS = 0
        reels_browser.PROFILE_REEL_VIEW_PAGE_ATTEMPTS = 2
        reels_browser.PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS = 0
        try:
            counts = await asyncio.wait_for(
                reels_browser.read_profile_reel_view_counts(page, "example", {"target"}),
                timeout=0.5,
            )
        finally:
            (
                reels_browser.PROFILE_REEL_VIEW_SETTLE_MILLISECONDS,
                reels_browser.PROFILE_REEL_VIEW_SCROLL_ATTEMPTS,
                reels_browser.PROFILE_REEL_VIEW_SCROLL_MILLISECONDS,
                reels_browser.PROFILE_REEL_VIEW_PAGE_ATTEMPTS,
                reels_browser.PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS,
            ) = original

        self.assertEqual(page.goto_count, 2)
        self.assertEqual(counts, {"target": 24_585})

    async def test_profile_reels_retries_after_a_transient_navigation_error(self) -> None:
        """Catch a profile lookup that returns before its second fresh navigation."""
        class Response:
            url = "https://www.instagram.com/graphql/query"
            headers = {"content-type": "application/json"}
            status = 200

            async def text(self) -> str:
                return json.dumps({
                    "data": {"fetch__XDTUserDict": {"clips_connection": {"edges": [{
                        "node": {"media": {
                            "code": "target", "product_type": "clips", "play_count": 24_585,
                        }}
                    }]}}}
                })

        class Mouse:
            async def wheel(self, _x: int, _y: int) -> None:
                raise AssertionError("A one-item scan should not need to scroll")

        class Page:
            url = ""
            mouse = Mouse()

            def __init__(self) -> None:
                self.goto_count = 0
                self.response_handler: object | None = None

            def on(self, event: str, handler: object) -> None:
                if event == "response":
                    self.response_handler = handler

            def remove_listener(self, event: str, handler: object) -> None:
                if event == "response" and self.response_handler is handler:
                    self.response_handler = None

            async def goto(self, url: str, **_kwargs: object) -> Response:
                self.goto_count += 1
                self.url = url
                if self.goto_count == 1:
                    raise RuntimeError("transient navigation error")
                if callable(self.response_handler):
                    self.response_handler(Response())
                return Response()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0)

        page = Page()
        original = (
            reels_browser.PROFILE_REEL_VIEW_SETTLE_MILLISECONDS,
            reels_browser.PROFILE_REEL_VIEW_SCROLL_ATTEMPTS,
            reels_browser.PROFILE_REEL_VIEW_SCROLL_MILLISECONDS,
            reels_browser.PROFILE_REEL_VIEW_PAGE_ATTEMPTS,
            reels_browser.PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS,
        )
        reels_browser.PROFILE_REEL_VIEW_SETTLE_MILLISECONDS = 0
        reels_browser.PROFILE_REEL_VIEW_SCROLL_ATTEMPTS = 1
        reels_browser.PROFILE_REEL_VIEW_SCROLL_MILLISECONDS = 0
        reels_browser.PROFILE_REEL_VIEW_PAGE_ATTEMPTS = 2
        reels_browser.PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS = 0
        try:
            counts = await reels_browser.read_profile_reel_view_counts(page, "example", {"target"})
        finally:
            (
                reels_browser.PROFILE_REEL_VIEW_SETTLE_MILLISECONDS,
                reels_browser.PROFILE_REEL_VIEW_SCROLL_ATTEMPTS,
                reels_browser.PROFILE_REEL_VIEW_SCROLL_MILLISECONDS,
                reels_browser.PROFILE_REEL_VIEW_PAGE_ATTEMPTS,
                reels_browser.PROFILE_REEL_VIEW_RETRY_DELAY_MILLISECONDS,
            ) = original

        self.assertEqual(page.goto_count, 2)
        self.assertEqual(counts, {"target": 24_585})

    async def test_reel_detail_response_recovers_missing_exact_engagement_after_waiting(self) -> None:
        class Response:
            url = "https://www.instagram.com/api/v1/media/target/info/"
            headers = {"content-type": "application/json"}
            status = 200

            async def text(self) -> str:
                return json.dumps({
                    "items": [{
                        "code": "target",
                        "product_type": "clips",
                        "like_count": 564,
                        "comment_count": 15,
                        "media_repost_count": 18,
                    }]
                })

        class Page:
            url = ""

            def __init__(self) -> None:
                self.response_handler: object | None = None
                self.wait_count = 0

            def on(self, event: str, handler: object) -> None:
                if event == "response":
                    self.response_handler = handler

            def remove_listener(self, event: str, handler: object) -> None:
                if event == "response" and self.response_handler is handler:
                    self.response_handler = None

            async def goto(self, url: str, **_kwargs: object) -> Response:
                self.url = url
                return Response()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                self.wait_count += 1
                if self.wait_count == 1 and callable(self.response_handler):
                    self.response_handler(Response())
                await asyncio.sleep(0)

        lookup = getattr(reels_browser, "read_reel_detail_metadata", None)
        self.assertIsNotNone(lookup)
        detail = await lookup(
            Page(),
            "target",
            {"likeCount": 564, "commentCount": 15, "repostCount": -1},
        )

        self.assertEqual(detail["likeCount"], 564)
        self.assertEqual(detail["commentCount"], 15)
        self.assertEqual(detail["repostCount"], 18)

    async def test_reel_detail_retries_a_fresh_navigation_when_engagement_is_missing_first(self) -> None:
        """Catch a detail lookup that gives up before a later response provides exact counts."""
        class Response:
            url = "https://www.instagram.com/api/v1/media/target/info/"
            headers = {"content-type": "application/json"}
            status = 200

            async def text(self) -> str:
                return json.dumps({"items": [{
                    "code": "target", "product_type": "clips",
                    "like_count": 564, "comment_count": 15, "media_repost_count": 18,
                }]})

        class Page:
            url = ""

            def __init__(self) -> None:
                self.goto_count = 0
                self.response_handler: object | None = None

            def on(self, event: str, handler: object) -> None:
                if event == "response":
                    self.response_handler = handler

            def remove_listener(self, event: str, handler: object) -> None:
                if event == "response" and self.response_handler is handler:
                    self.response_handler = None

            async def goto(self, url: str, **_kwargs: object) -> Response:
                self.goto_count += 1
                self.url = url
                if self.goto_count == 2 and callable(self.response_handler):
                    self.response_handler(Response())
                return Response()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0)

        page = Page()
        original = (
            reels_browser.EXACT_REEL_DETAIL_SETTLE_MILLISECONDS,
            reels_browser.EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS,
            reels_browser.EXACT_REEL_DETAIL_POLL_MILLISECONDS,
            getattr(reels_browser, "EXACT_REEL_DETAIL_PAGE_ATTEMPTS", None),
            getattr(reels_browser, "EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS", None),
        )
        reels_browser.EXACT_REEL_DETAIL_SETTLE_MILLISECONDS = 0
        reels_browser.EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS = 1
        reels_browser.EXACT_REEL_DETAIL_POLL_MILLISECONDS = 1
        reels_browser.EXACT_REEL_DETAIL_PAGE_ATTEMPTS = 2
        reels_browser.EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS = 0
        try:
            detail = await reels_browser.read_reel_detail_metadata(
                page,
                "target",
                {"likeCount": None, "commentCount": None, "repostCount": None},
            )
        finally:
            (
                reels_browser.EXACT_REEL_DETAIL_SETTLE_MILLISECONDS,
                reels_browser.EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS,
                reels_browser.EXACT_REEL_DETAIL_POLL_MILLISECONDS,
                page_attempts,
                retry_delay,
            ) = original
            if page_attempts is None:
                del reels_browser.EXACT_REEL_DETAIL_PAGE_ATTEMPTS
            else:
                reels_browser.EXACT_REEL_DETAIL_PAGE_ATTEMPTS = page_attempts
            if retry_delay is None:
                del reels_browser.EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS
            else:
                reels_browser.EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS = retry_delay

        self.assertEqual(page.goto_count, 2)
        self.assertEqual(detail["likeCount"], 564)
        self.assertEqual(detail["commentCount"], 15)
        self.assertEqual(detail["repostCount"], 18)

    async def test_reel_detail_retries_after_a_transient_navigation_error(self) -> None:
        """Catch a detail lookup that returns before its second fresh navigation."""
        class Response:
            url = "https://www.instagram.com/api/v1/media/target/info/"
            headers = {"content-type": "application/json"}
            status = 200

            async def text(self) -> str:
                return json.dumps({"items": [{
                    "code": "target", "product_type": "clips",
                    "like_count": 564, "comment_count": 15, "media_repost_count": 18,
                }]})

        class Page:
            url = ""

            def __init__(self) -> None:
                self.goto_count = 0
                self.response_handler: object | None = None

            def on(self, event: str, handler: object) -> None:
                if event == "response":
                    self.response_handler = handler

            def remove_listener(self, event: str, handler: object) -> None:
                if event == "response" and self.response_handler is handler:
                    self.response_handler = None

            async def goto(self, url: str, **_kwargs: object) -> Response:
                self.goto_count += 1
                self.url = url
                if self.goto_count == 1:
                    raise RuntimeError("transient navigation error")
                if callable(self.response_handler):
                    self.response_handler(Response())
                return Response()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0)

        page = Page()
        original = (
            reels_browser.EXACT_REEL_DETAIL_SETTLE_MILLISECONDS,
            reels_browser.EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS,
            reels_browser.EXACT_REEL_DETAIL_POLL_MILLISECONDS,
            reels_browser.EXACT_REEL_DETAIL_PAGE_ATTEMPTS,
            reels_browser.EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS,
        )
        reels_browser.EXACT_REEL_DETAIL_SETTLE_MILLISECONDS = 0
        reels_browser.EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS = 10
        reels_browser.EXACT_REEL_DETAIL_POLL_MILLISECONDS = 1
        reels_browser.EXACT_REEL_DETAIL_PAGE_ATTEMPTS = 2
        reels_browser.EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS = 0
        try:
            detail = await reels_browser.read_reel_detail_metadata(
                page,
                "target",
                {"likeCount": None, "commentCount": None, "repostCount": None},
            )
        finally:
            (
                reels_browser.EXACT_REEL_DETAIL_SETTLE_MILLISECONDS,
                reels_browser.EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS,
                reels_browser.EXACT_REEL_DETAIL_POLL_MILLISECONDS,
                reels_browser.EXACT_REEL_DETAIL_PAGE_ATTEMPTS,
                reels_browser.EXACT_REEL_DETAIL_RETRY_DELAY_MILLISECONDS,
            ) = original

        self.assertEqual(page.goto_count, 2)
        self.assertEqual(detail["likeCount"], 564)
        self.assertEqual(detail["commentCount"], 15)
        self.assertEqual(detail["repostCount"], 18)

    async def test_reel_detail_lookup_stops_at_its_deadline_when_a_json_body_stalls(self) -> None:
        class Response:
            url = "https://www.instagram.com/api/v1/media/target/info/"
            headers = {"content-type": "application/json"}
            status = 200

            async def text(self) -> str:
                await asyncio.Event().wait()
                return "{}"

        class Page:
            url = ""

            def __init__(self) -> None:
                self.response_handler: object | None = None

            def on(self, event: str, handler: object) -> None:
                if event == "response":
                    self.response_handler = handler

            def remove_listener(self, event: str, handler: object) -> None:
                if event == "response" and self.response_handler is handler:
                    self.response_handler = None

            async def goto(self, url: str, **_kwargs: object) -> Response:
                self.url = url
                response = Response()
                if callable(self.response_handler):
                    self.response_handler(response)
                return response

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0)

        lookup = getattr(reels_browser, "read_reel_detail_metadata", None)
        self.assertIsNotNone(lookup)
        original = (
            reels_browser.EXACT_REEL_DETAIL_SETTLE_MILLISECONDS,
            reels_browser.EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS,
            reels_browser.EXACT_REEL_DETAIL_POLL_MILLISECONDS,
        )
        reels_browser.EXACT_REEL_DETAIL_SETTLE_MILLISECONDS = 0
        reels_browser.EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS = 20
        reels_browser.EXACT_REEL_DETAIL_POLL_MILLISECONDS = 1
        try:
            detail = await asyncio.wait_for(
                lookup(Page(), "target", {"likeCount": 564, "commentCount": 15, "repostCount": None}),
                timeout=0.5,
            )
        finally:
            (
                reels_browser.EXACT_REEL_DETAIL_SETTLE_MILLISECONDS,
                reels_browser.EXACT_REEL_DETAIL_TIMEOUT_MILLISECONDS,
                reels_browser.EXACT_REEL_DETAIL_POLL_MILLISECONDS,
            ) = original

        self.assertEqual(detail["likeCount"], 564)
        self.assertEqual(detail["commentCount"], 15)
        self.assertIsNone(detail["repostCount"])

    async def test_follower_lookup_uses_web_profile_info_exact_integer_instead_of_compact_label(self) -> None:
        class Response:
            status = 200

        class Page:
            url = ""

            async def goto(self, url: str, **_kwargs: object) -> Response:
                self.url = url
                return Response()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

            async def evaluate(self, _script: str, _username: str) -> dict[str, object]:
                return {
                    "status": 200,
                    "text": json.dumps({
                        "data": {
                            "user": {
                                "username": "example",
                                "biography": "🌟 영상은 릴스탭 눌러주세요🌟\n💗 누구보다 빠르게 이쁘고 트렌디한 신상템 소개해요 💕",
                                "category_name": "의류(브랜드)",
                                "media_count": 2_078,
                                "edge_followed_by": {"count": 37_293},
                            }
                        }
                    }),
                    "values": ["1.6만 followers"],
                    "descriptions": [],
                    "profileMatches": True,
                    "body": "",
                    "login": False,
                }

        output = StringIO()
        with redirect_stdout(output):
            result = await request_web_follower_count(Page(), "example")

        self.assertEqual(result["followerCount"], 37_293)
        self.assertEqual(
            result.get("biography"),
            "🌟 영상은 릴스탭 눌러주세요🌟\n💗 누구보다 빠르게 이쁘고 트렌디한 신상템 소개해요 💕",
        )
        self.assertEqual(result.get("profile_category"), "의류(브랜드)")
        self.assertEqual(result.get("postCount"), 2_078)
        self.assertEqual(output.getvalue(), "")

    async def test_follower_lookup_uses_an_exact_visible_profile_header_when_the_endpoint_fails(self) -> None:
        class Response:
            status = 200

        class Page:
            url = ""

            async def goto(self, url: str, **_kwargs: object) -> Response:
                self.url = url
                return Response()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

            async def evaluate(self, _script: str, _username: str) -> dict[str, object]:
                return {
                    "status": 400,
                    "text": "{}",
                    "profileText": "euqele\n게시물 2078 팔로워 3230 팔로우 5111\n의류(브랜드)\n롯데잠실 4층",
                    "profileTexts": ["팔로워 3230", "의류(브랜드)"],
                }

        result = await request_web_follower_count(Page(), "euqele")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["followerCount"], 3_230)
        self.assertEqual(result["sourceField"], "profile_header_text")
        self.assertEqual(result["profile_category"], "의류(브랜드)")
        self.assertEqual(result["postCount"], 2_078)

    def test_visible_profile_follower_parser_rejects_compact_labels(self) -> None:
        parser = getattr(reels_browser, "exact_visible_profile_follower_count", None)
        post_parser = getattr(reels_browser, "exact_visible_profile_post_count", None)
        self.assertIsNotNone(parser)
        self.assertIsNotNone(post_parser)
        self.assertEqual(parser("게시물 2078 팔로워 3230 팔로우 5111"), 3_230)
        self.assertEqual(post_parser("게시물 2078 팔로워 3230 팔로우 5111"), 2_078)
        self.assertEqual(parser("1.6만 followers"), None)
        self.assertEqual(post_parser("게시물 2.1만"), None)
        self.assertEqual(parser("followers 3.2K"), None)

    async def test_anonymous_follower_retry_stops_after_three_transient_errors(self) -> None:
        """Catch an anonymous retry loop that can run forever after web errors."""
        class Page:
            goto_calls = 0

            async def goto(self, _url: str, **_kwargs: object) -> object:
                self.goto_calls += 1
                raise RuntimeError("temporary network failure")

        page = Page()
        result = await reels_browser.request_anonymous_follower_count(page, "example")

        self.assertEqual(page.goto_calls, 3)
        self.assertEqual(result["status"], "web_error")

    async def test_no_login_uses_temporary_context_without_profile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_dir = Path(directory) / "must_not_be_created"
            context = object()

            class FakeBrowser:
                async def new_context(self, **kwargs: object) -> object:
                    self.context_options = kwargs
                    return context

            class FakeChromium:
                def __init__(self) -> None:
                    self.browser = FakeBrowser()
                    self.launch_options: dict[str, object] = {}

                async def launch(self, **kwargs: object) -> FakeBrowser:
                    self.launch_options = kwargs
                    return self.browser

                async def launch_persistent_context(self, *_args: object, **_kwargs: object) -> object:
                    raise AssertionError("persistent context must not be used in no-login mode")

            chromium = FakeChromium()
            options = parse_args(["--no-login", "--profile-dir", str(profile_dir)])
            browser, launched_context = await launch_collection_context(chromium, "browser.exe", options)

            self.assertIs(browser, chromium.browser)
            self.assertIs(launched_context, context)
            self.assertFalse(profile_dir.exists())
            self.assertFalse(chromium.launch_options["headless"])

    async def test_reel_navigation_waits_for_shortcode_change(self) -> None:
        identities = [
            {"currentUrl": "https://www.instagram.com/reels/old/", "activeHref": ""},
            {"currentUrl": "https://www.instagram.com/reels/new/", "activeHref": ""},
        ]

        class Keyboard:
            presses: list[str]

            def __init__(self) -> None:
                self.presses = []

            async def press(self, key: str) -> None:
                self.presses.append(key)

        class Mouse:
            scrolls = 0

            async def wheel(self, _x: int, _y: int) -> None:
                self.scrolls += 1

        class Page:
            def __init__(self) -> None:
                self.calls = 0
                self.keyboard = Keyboard()
                self.mouse = Mouse()

            async def evaluate(self, _script: str) -> dict[str, str]:
                value = identities[min(self.calls, len(identities) - 1)]
                self.calls += 1
                return value

            async def wait_for_timeout(self, _milliseconds: float) -> None:
                return None

        page = Page()
        transition = await advance_to_next_reel(page, "old", 1_000)
        self.assertTrue(transition["changed"])
        self.assertEqual(transition["shortcode"], "new")
        self.assertEqual(page.keyboard.presses, ["Escape"])
        self.assertEqual(page.mouse.scrolls, 1)

    async def test_manual_startup_keeps_page_open_after_navigation_failure(self) -> None:
        class Page:
            url = "https://www.instagram.com/"

            def __init__(self) -> None:
                self.goto_calls = 0
                self.close_calls = 0

            def on(self, _event: str, _handler: object) -> None:
                return None

            async def goto(self, *_args: object, **_kwargs: object) -> None:
                self.goto_calls += 1
                raise RuntimeError("net::ERR_HTTP_RESPONSE_CODE_FAILURE")

            async def wait_for_timeout(self, _milliseconds: float) -> None:
                return None

            async def close(self) -> None:
                self.close_calls += 1

        page = Page()

        class Context:
            async def new_page(self) -> Page:
                return page

        opened = await create_collection_page(
            Context(),
            "https://www.instagram.com/reels/",
            {},
            allow_login=True,
            keep_open_on_navigation_failure=True,
        )
        self.assertIs(opened, page)
        self.assertEqual(page.goto_calls, 3)
        self.assertEqual(page.close_calls, 0)

    async def test_lock_prevents_a_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = await CollectorLock(directory).acquire()
            with self.assertRaisesRegex(RuntimeError, "Another collector"):
                await CollectorLock(directory).acquire()
            await first.release()
            second = await CollectorLock(directory).acquire()
            await second.release()

    async def test_long_reel_store_recovers_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "reels_rows.csv"
            interrupted = await LongReelStore.create(csv_path, 500, "rows")
            await interrupted.append(reel_record(1))
            await interrupted.append(reel_record(2))
            self.assertTrue(interrupted.journal_path.exists())
            recovered = await LongReelStore.create(csv_path, 500, "rows")
            self.assertEqual(recovered.stats()["rows"], 2)
            self.assertFalse(recovered.journal_path.exists())

    async def test_long_store_exports_recollections_as_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "reels_rows.csv"
            store = await LongReelStore.create(csv_path, 100, "rows")
            first = reel_record(1, "2026-01-01T00:00:00.000Z")
            second = reel_record(1, "2026-01-01T06:00:00.000Z")
            first["view_count"] = 1_000
            second["view_count"] = 1_250
            first["follower_count"] = 10_000
            second["follower_count"] = 10_000
            await store.append(first)
            await store.append(second)
            await store.flush()
            outputs = await store.export_outputs()
            self.assertEqual(len(store.rows), 2)
            self.assertNotIn("collection_label", store.fields)
            self.assertNotIn("collection_label", store.rows[1])
            self.assertEqual(store.rows[1]["view_count"], 1_250)
            self.assertEqual(set(outputs), {"csv", "json", "xlsx"})
            self.assertEqual(outputs["csv"].name, "reels.csv")
            self.assertEqual(outputs["json"].name, "reels.json")
            self.assertEqual(outputs["xlsx"].name, "reels.xlsx")
            for path in outputs.values():
                self.assertTrue(path.exists(), path)
            expected_fields = [
                "collection_number", "days_since_previous", "collected_at", "url", "user_id", "username",
                "title", "hashtags", "audio_name", "location_name", "ad",
                "uploaded_at", "video_duration_seconds", "days_since_upload", "view_count", "like_count",
                "comment_count", "repost_count", "follower_count",
                "reaction_rate",
                "view_count_change", "like_count_change", "comment_count_change",
                "repost_count_change", "follower_count_change", "reaction_rate_change",
            ]
            csv_fields, csv_rows = read_csv_objects(outputs["csv"])
            json_rows = json.loads(outputs["json"].read_text(encoding="utf-8"))
            self.assertEqual(len(csv_rows), 2)
            self.assertEqual(len(json_rows), 2)
            self.assertEqual(csv_fields, expected_fields)
            self.assertEqual(list(json_rows[0]), expected_fields)
            self.assertEqual(read_xlsx_header(outputs["xlsx"]), expected_fields)
            self.assertEqual(csv_rows[0]["collection_number"], "1")
            self.assertEqual(csv_rows[1]["collection_number"], "2")
            self.assertEqual(csv_rows[0]["video_duration_seconds"], "12.5")
            self.assertEqual(csv_rows[1]["video_duration_seconds"], "12.5")
            self.assertEqual(csv_rows[1]["days_since_previous"], "0.25")
            self.assertEqual(csv_rows[1]["view_count"], "1250")
            self.assertEqual(csv_rows[0]["reaction_rate"], "0.1")
            self.assertEqual(csv_rows[1]["reaction_rate"], "0.125")
            self.assertEqual(csv_rows[0]["view_count_change"], "")
            self.assertEqual(csv_rows[1]["view_count_change"], "250")
            self.assertEqual(csv_rows[1]["follower_count_change"], "0")
            self.assertAlmostEqual(float(csv_rows[1]["reaction_rate_change"]), 0.025)
            self.assertEqual(json_rows[0]["collection_number"], 1)
            self.assertEqual(json_rows[1]["collection_number"], 2)
            self.assertEqual(json_rows[0]["video_duration_seconds"], 12.5)
            self.assertEqual(json_rows[1]["video_duration_seconds"], 12.5)
            self.assertEqual(json_rows[1]["days_since_previous"], 0.25)
            self.assertEqual(json_rows[1]["view_count"], 1_250)
            self.assertEqual(json_rows[0]["reaction_rate"], 0.1)
            self.assertEqual(json_rows[1]["reaction_rate"], 0.125)
            self.assertIsNone(json_rows[0]["view_count_change"])
            self.assertEqual(json_rows[1]["view_count_change"], 250)
            self.assertEqual(json_rows[1]["follower_count_change"], 0)
            self.assertAlmostEqual(json_rows[1]["reaction_rate_change"], 0.025)
            self.assertEqual(
                read_reel_urls_from_xlsx(outputs["xlsx"]),
                ["https://www.instagram.com/reels/test_1/"],
            )

    async def test_reconcile_exports_merges_updated_workbook_before_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            history_path = data_dir / ".collector" / "reels_history_active.csv"
            store = await LongReelStore.create(history_path, 100, "columns")
            await store.append(reel_record(1, "2026-01-01T00:00:00.000Z"))
            await store.flush()
            await store.export_outputs()

            updated_record = {
                "collection_number": 1,
                **reel_record(2, "2026-01-02T00:00:00.000Z"),
            }
            updated_path = reels_browser.write_reel_xlsx(
                data_dir / "reels_updated.xlsx",
                [updated_record],
                ["collection_number", *CSV_FIELDS],
                layout="columns",
                sheet_name="reels",
            )

            reconciler = getattr(reels_browser, "reconcile_reel_exports", None)
            self.assertIsNotNone(reconciler)
            result = await reconciler(data_dir)
            _fields, history_rows = read_csv_objects(history_path)

            self.assertEqual(result["addedSnapshots"], 1)
            self.assertFalse(updated_path.exists())
            self.assertEqual(len(history_rows), 2)
            self.assertEqual(history_rows[1]["video_duration_seconds"], "12.5")
            self.assertEqual(
                read_reel_urls_from_xlsx(data_dir / "reels.xlsx"),
                [
                    "https://www.instagram.com/reels/test_1/",
                    "https://www.instagram.com/reels/test_2/",
                ],
            )

    async def test_follower_enricher_persists_profile_biography(self) -> None:
        collected_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        async def lookup(_payload: dict[str, str]) -> dict[str, object]:
            return {
                "status": "success",
                "followerCount": 37_293,
                "sourceField": "edge_followed_by.count",
                "biography": "🌟 영상은 릴스탭 눌러주세요🌟\n매일 신상 코디를 소개해요.",
                "profile_category": "의류(브랜드)",
                "postCount": 2_078,
            }

        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup, now=lambda: collected_at)
            await enricher.track_user(user_id="987654321", username="profile_user")
            await enricher.drain()
            fields, rows = read_csv_objects(user_history_path(directory))

        self.assertIn("biography", fields)
        self.assertEqual(rows[0].get("biography"), "🌟 영상은 릴스탭 눌러주세요🌟\n매일 신상 코디를 소개해요.")
        self.assertEqual(rows[0].get("profile_category"), "의류(브랜드)")
        self.assertEqual(rows[0].get("post_count"), "2078")
        self.assertEqual(rows[0].get("follower_count"), "37293")
        self.assertEqual(rows[0].get("collected_at"), "2026-01-02T03:04:05.000Z")

    async def test_follower_cache_is_six_hours_and_creates_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            current = datetime(2026, 1, 1, tzinfo=timezone.utc)
            lookup_calls = 0

            async def lookup(_payload: dict[str, str]) -> dict[str, object]:
                nonlocal lookup_calls
                lookup_calls += 1
                return {
                    "status": "success",
                    "followerCount": 123,
                    "postCount": 456,
                    "biography": "saved profile biography",
                    "error": "",
                    "source": "test",
                    "sourceField": "edge_followed_by.count",
                }

            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup, now=lambda: current)
            payload = {"user_id": "987654321", "username": "cached_user", "seen_at": current.isoformat()}
            await enricher.track_user(**payload)
            await enricher.drain()
            await enricher.track_user(**payload)
            await enricher.drain()
            self.assertEqual(lookup_calls, 1)
            current += timedelta(hours=6)
            await enricher.track_user(**payload)
            await enricher.drain()
            self.assertEqual(lookup_calls, 2)
            fields, rows = read_csv_objects(user_history_path(directory))
            self.assertEqual(rows[0]["2nd collect_follower_count"], "123")
            self.assertEqual(fields, [
                "user_id", "username", "biography", "profile_category", "post_count", "follower_count", "collected_at",
                "2nd collect_post_count", "2nd collect_follower_count", "2nd collect_collected_at",
            ])

    async def test_follower_enricher_does_not_requery_a_fresh_user_without_biography(self) -> None:
        current = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        calls = 0

        async def lookup(_payload: dict[str, str]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "status": "success",
                "followerCount": 1_000,
                "sourceField": "edge_followed_by.count",
                "biography": "영상은 릴스탭 눌러주세요~",
            }

        with tempfile.TemporaryDirectory() as directory:
            users_path = Path(directory) / "users.csv"
            users_path.write_text(
                "user_id,username,follower_count,collected_at\n"
                "1,legacy,1000,2026-01-01T00:00:00Z\n",
                encoding="utf-8",
            )
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup, now=lambda: current)
            await enricher.track_user(user_id="1", username="legacy")
            await enricher.drain()
            _fields, rows = read_csv_objects(user_history_path(directory))

        self.assertEqual(calls, 0)
        self.assertEqual(rows[0].get("biography"), "")

    async def test_immediate_follower_lookup_bypasses_a_fresh_legacy_cache(self) -> None:
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)
        calls = 0

        async def lookup(_payload: dict[str, str]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "status": "success",
                "followerCount": 37_293,
                "sourceField": "edge_followed_by.count",
            }

        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup, now=lambda: current)
            payload = {"user_id": "987654321", "username": "exact_user", "seen_at": current.isoformat()}
            await enricher.track_user(**payload)
            await enricher.drain()

            immediate_lookup = getattr(enricher, "lookup_user_now", None)
            self.assertIsNotNone(immediate_lookup)
            result = await immediate_lookup(**payload)
            await enricher.drain()

            self.assertEqual(calls, 2)
            self.assertEqual(result["follower_count"], "37293")
            self.assertEqual(result.get("followerCount"), 37_293)
            self.assertEqual(result.get("sourceField"), "edge_followed_by.count")

    async def test_immediate_follower_lookup_is_not_queued_again_when_optional_profile_fields_are_empty(self) -> None:
        current = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        calls = 0

        async def lookup(_payload: dict[str, str]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "status": "success",
                "followerCount": 8_191,
                "sourceField": "edge_followed_by.count",
            }

        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup, now=lambda: current)
            payload = {"user_id": "987654321", "username": "same_user", "seen_at": current.isoformat()}

            result = await enricher.lookup_user_now(**payload)
            await enricher.track_user(**payload)
            await enricher.drain()

            self.assertEqual(result["status"], "success")
            self.assertEqual(calls, 1)

    async def test_immediate_exact_follower_lookup_accepts_a_large_drop_from_legacy_data(self) -> None:
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)
        responses = iter([
            {
                "status": "success",
                "followerCount": 52_120_000,
                "sourceField": "edge_followed_by.count",
            },
            {
                "status": "success",
                "followerCount": 48_000,
                "sourceField": "edge_followed_by.count",
            },
        ])

        async def lookup(_payload: dict[str, str]) -> dict[str, object]:
            return next(responses)

        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup, now=lambda: current)
            payload = {"user_id": "987654321", "username": "corrected_user"}
            await enricher.track_user(**payload)
            await enricher.drain()

            result = await enricher.lookup_user_now(**payload)
            await enricher.drain()
            fields, rows = read_csv_objects(user_history_path(directory))

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["followerCount"], 48_000)
            self.assertEqual(result["sourceField"], "edge_followed_by.count")
            self.assertEqual(rows[0]["2nd collect_follower_count"], "48000")
            self.assertEqual(fields[-2:], ["2nd collect_follower_count", "2nd collect_collected_at"])

    async def test_immediate_follower_lookup_accepts_a_full_visible_profile_header_count(self) -> None:
        async def lookup(_payload: dict[str, str]) -> dict[str, object]:
            return {
                "status": "success",
                "followerCount": 37_293,
                "sourceField": "profile_header_text",
            }

        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup)
            result = await enricher.lookup_user_now(user_id="987654321", username="untrusted_user")
            await enricher.drain()
            fields, rows = read_csv_objects(user_history_path(directory))

            self.assertEqual(result["status"], "success")
            self.assertEqual(result.get("followerCount"), 37_293)
            self.assertEqual(result.get("sourceField"), "profile_header_text")
            self.assertEqual(rows[0]["follower_count"], "37293")

    async def test_immediate_follower_lookup_rejects_non_integer_network_counts(self) -> None:
        invalid_values: list[object] = ["37293", True, -1]
        for index, invalid_value in enumerate(invalid_values):
            with self.subTest(value=invalid_value):
                async def lookup(_payload: dict[str, str], value: object = invalid_value) -> dict[str, object]:
                    return {
                        "status": "success",
                        "followerCount": value,
                        "sourceField": "edge_followed_by.count",
                    }

                with tempfile.TemporaryDirectory() as directory:
                    enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup)
                    result = await enricher.lookup_user_now(
                        user_id=str(987654321 + index),
                        username=f"invalid_count_{index}",
                    )
                    await enricher.drain()
                    _fields, rows = read_csv_objects(user_history_path(directory))

                    self.assertEqual(result["status"], "invalid_follower_count")
                    self.assertIsNone(result.get("followerCount"))
                    self.assertEqual(result.get("sourceField"), "edge_followed_by.count")
                    self.assertEqual(rows[0]["follower_count"], "")

    async def test_exact_follower_refresh_enqueues_fresh_legacy_rows(self) -> None:
        calls = 0

        async def lookup(_payload: dict[str, str]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"status": "success", "followerCount": 37_293, "sourceField": "edge_followed_by.count"}

        with tempfile.TemporaryDirectory() as directory:
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup)
            await enricher.track_user(user_id="1", username="legacy_user")
            await enricher.drain()

            exact_refresh = getattr(enricher, "enqueue_all_exact", None)
            self.assertIsNotNone(exact_refresh)
            queued = await exact_refresh()
            await enricher.drain()

            self.assertEqual(queued, 1)
            self.assertEqual(calls, 2)

    async def test_follower_merge_updates_every_matching_row_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            users_path = Path(directory) / "users.csv"
            users_path.write_text(
                "user_id,username,follower_count,collected_at\n"
                "1,first,1000,2026-01-01T00:00:00Z\n"
                "2,second,2000,2026-01-01T00:00:00Z\n",
                encoding="utf-8",
            )
            rows = [
                {**reel_record(1), "user_id": "1", "username": "first"},
                {**reel_record(2), "user_id": "2", "username": "second"},
            ]

            changed = merge_follower_data_into_rows(rows, users_path)

            self.assertEqual(changed, 2)
            self.assertEqual([row["follower_count"] for row in rows], ["1000", "2000"])

    async def test_follower_merge_replaces_a_larger_legacy_estimate_with_latest_exact_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            users_path = Path(directory) / "users.csv"
            users_path.write_text(
                "user_id,username,follower_count,collected_at,2nd collect_follower_count,2nd collect_collected_at\n"
                "1,corrected,52120000,2026-01-01T00:00:00Z,48000,2026-01-01T06:00:00Z\n",
                encoding="utf-8",
            )
            row = {
                **reel_record(1, "2026-01-01T06:00:00Z"),
                "user_id": "1",
                "username": "corrected",
                "follower_count": "52120000",
            }

            changed = merge_follower_data_into_rows([row], users_path)

            self.assertEqual(changed, 1)
            self.assertEqual(row["follower_count"], "48000")

    async def test_legacy_follower_timestamp_is_renamed_to_collected_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            users_path = Path(directory) / "users.csv"
            users_path.write_text(
                "user_id,username,follower_count,follower_count_collected_at\n"
                "1,legacy,1000,2026-01-01T00:00:00Z\n",
                encoding="utf-8",
            )

            async def lookup(_payload: dict[str, str]) -> dict[str, object]:
                raise AssertionError("Fresh migrated data should use the cache")

            current = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup, now=lambda: current)
            await enricher.ready()
            fields, rows = read_csv_objects(user_history_path(directory))

            self.assertEqual(fields, ["user_id", "username", "biography", "profile_category", "post_count", "follower_count", "collected_at"])
            self.assertEqual(rows[0]["collected_at"], "2026-01-01T00:00:00Z")

    async def test_follower_queue_stops_after_five_web_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = 0

            async def lookup(_payload: dict[str, str]) -> dict[str, str]:
                nonlocal calls
                calls += 1
                return {"status": "web_error", "error": "page crashed", "source": "test"}

            enricher = FollowerEnricher(data_dir=directory, lookup_impl=lookup)
            for index in range(10):
                await enricher.track_user(user_id=str(index + 1), username=f"error_user_{index}")
            stats = await enricher.drain()
            self.assertEqual(stats["stopStatus"], "repeated_web_error")
            self.assertEqual(calls, 5)


if __name__ == "__main__":
    unittest.main()
