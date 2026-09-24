from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from android_collector.models import AccessBlockedError, CollectorError, Metric, ObservedProfile, ObservedReel
from android_collector.diagnostics import CollectorDiagnostics
from android_collector.store import CollectionStore
from android_collector.workflows import (
    CollectorOptions,
    _metric_field_progress_value,
    capture_current_reel,
    preflight,
    run_feed,
    run_hashtag,
    run_refresh,
)


REEL_XML = (Path(__file__).parent / "fixtures" / "reel_visible.xml").read_text(encoding="utf-8")


class FakeDriver:
    def __init__(
        self,
        ui_xml: list[str],
        *,
        metric_panel_available: bool = False,
        profile_available: bool = False,
        account_country_available: bool = False,
        hashtag_grid_available: bool = False,
        caption_detail_available: bool = False,
        comment_panel_available: bool = False,
        share_available: bool = False,
        clipboard_text: str = "",
        clipboard_error: bool = False,
    ) -> None:
        self.ui_xml = list(ui_xml)
        self.swipe_count = 0
        self.entered_text: list[str] = []
        self.opened_urls: list[str] = []
        self.tapped_label_sets: list[tuple[str, ...]] = []
        self.tapped_resource_sets: list[tuple[str, ...]] = []
        self.launched = False
        self.back_count = 0
        self.metric_panel_available = metric_panel_available
        self.profile_available = profile_available
        self.account_country_available = account_country_available
        self.hashtag_grid_available = hashtag_grid_available
        self.caption_detail_available = caption_detail_available
        self.comment_panel_available = comment_panel_available
        self.share_available = share_available
        self.clipboard_text = clipboard_text
        self.clipboard_error = clipboard_error
        self.mutating_operation_called = False

    def ensure_ready(self) -> None:
        return None

    def launch_instagram(self) -> None:
        self.launched = True

    def dump_ui(self) -> str:
        return self.ui_xml.pop(0) if len(self.ui_xml) > 1 else self.ui_xml[0]

    def tap_text(
        self,
        labels: tuple[str, ...] | list[str],
        *,
        ui_xml: str | None = None,
    ) -> bool:
        captured = tuple(labels)
        self.tapped_label_sets.append(captured)
        return (
            "Reel by" in captured
            or (self.metric_panel_available and "Like number is" in captured)
            or (self.account_country_available and "Options" in captured)
            or (self.account_country_available and "About this account" in captured)
            or (self.share_available and ("Share" in captured or "Copy link" in captured))
        )

    def input_text(self, value: str) -> None:
        self.entered_text.append(value)

    def tap_resource_id(
        self,
        markers: tuple[str, ...] | list[str],
        *,
        ui_xml: str | None = None,
    ) -> bool:
        captured = tuple(markers)
        self.tapped_resource_sets.append(captured)
        return (
            (self.profile_available and "clips_author_username" in captured)
            or (self.hashtag_grid_available and "grid_card_layout_container" in captured)
            or (self.caption_detail_available and "clips_caption_component" in captured)
            or (self.comment_panel_available and "comment_button" in captured)
            or (self.share_available and ("share_button" in captured or "copy_link" in captured))
        )

    def swipe_up(self) -> None:
        self.swipe_count += 1

    def press_back(self) -> None:
        self.back_count += 1

    def open_instagram_url(self, url: str) -> None:
        self.opened_urls.append(url)

    def read_clipboard(self) -> str:
        if self.clipboard_error:
            raise CollectorError("clipboard access denied")
        return self.clipboard_text

    def capture_screenshot(self, path: Path) -> None:
        path.write_bytes(b"\x89PNG\r\n\x1a\n")


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = CollectionStore(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_run_feed_skips_a_known_reel_and_continues_to_the_next_new_reel(self) -> None:
        known_driver = FakeDriver([REEL_XML, REEL_XML])
        self.assertEqual(run_feed(CollectorOptions(max_items=1, delay_seconds=0), known_driver, self.store), 1)
        next_reel_xml = REEL_XML.replace("odi.pigi", "next.creator")
        # The first dump is the read-only preflight check, then the collection
        # encounters the known Reel before reaching the new one.
        driver = FakeDriver([REEL_XML, REEL_XML, next_reel_xml, next_reel_xml])

        stored = run_feed(CollectorOptions(max_items=1, delay_seconds=0), driver, self.store)

        self.assertEqual(stored, 1)
        self.assertEqual(driver.swipe_count, 1)
        self.assertEqual(len(list((self.store.data_dir / "evidence").glob("*.xml"))), 2)

    def test_run_refresh_reopens_url_and_appends_a_second_observation(self) -> None:
        url = "https://www.instagram.com/reel/CODE123/?igsh=old"
        existing = ObservedReel(
            source_mode="hashtag",
            source_query="fashion",
            reel_url=url,
            reel_fingerprint="old-fingerprint",
            collected_at="2026-08-29T00:00:00Z",
            username="odi.pigi",
            caption="saved caption",
            audio_name="saved audio",
            uploaded_at="2026-04-28",
            profile=ObservedProfile(biography="saved bio", follower_count=1_169),
            metrics={"like_count": Metric("like", 4_000, "4000")},
        )
        self.store.append(existing)
        self.store.export()
        driver = FakeDriver([REEL_XML, REEL_XML])

        refreshed = run_refresh(CollectorOptions(max_items=1, delay_seconds=0), driver, self.store)

        self.assertEqual(refreshed, 1)
        self.assertEqual(driver.opened_urls, [url])
        self.assertEqual(len(self.store.rows), 2)
        self.assertEqual(self.store.rows[-1]["source_mode"], "refresh")
        self.assertEqual(self.store.rows[-1]["biography"], "saved bio")
        self.assertEqual(self.store.rows[-1]["follower_count"], 1_169)
        self.assertEqual(self.store.rows[-1]["like_count"], 4_699)

    def test_run_refresh_rejects_a_history_without_reel_urls(self) -> None:
        self.store.append(
            ObservedReel(
                source_mode="hashtag",
                source_query="fashion",
                reel_url="",
                reel_fingerprint="old-fingerprint",
                collected_at="2026-08-29T00:00:00Z",
                username="odi.pigi",
            )
        )
        self.store.export()

        with self.assertRaisesRegex(CollectorError, "No Instagram Reel URLs"):
            run_refresh(CollectorOptions(max_items=1, delay_seconds=0), FakeDriver([REEL_XML]), self.store)

    def test_run_refresh_exports_successes_before_a_later_failure(self) -> None:
        first_url = "https://www.instagram.com/reel/FIRST123/"
        second_url = "https://www.instagram.com/reel/SECOND456/"

        class FailingOpenDriver(FakeDriver):
            def open_instagram_url(self, url: str) -> None:
                if self.opened_urls:
                    raise CollectorError("device offline")
                super().open_instagram_url(url)

        driver = FailingOpenDriver([REEL_XML])
        input_workbook = self.store.data_dir / "selected.xlsx"
        input_workbook.write_bytes(b"placeholder")
        with patch(
            "android_collector.workflows.read_reel_urls_from_xlsx",
            return_value=[first_url, second_url],
        ), self.assertRaisesRegex(CollectorError, "device offline"):
            run_refresh(
                CollectorOptions(max_items=2, delay_seconds=0),
                driver,
                self.store,
                input_xlsx=input_workbook,
            )

        self.assertTrue((self.store.data_dir / "reels.csv").exists())
        self.assertEqual(len(self.store.rows), 1)

    def test_run_feed_stops_after_a_bounded_number_of_repeated_known_reels(self) -> None:
        driver = FakeDriver([REEL_XML, REEL_XML, REEL_XML])

        stored = run_feed(CollectorOptions(max_items=10, delay_seconds=0), driver, self.store)

        self.assertEqual(stored, 1)
        self.assertGreater(driver.swipe_count, 1)
        self.assertEqual(len(list((self.store.data_dir / "evidence").glob("*.xml"))), 1)
        self.assertTrue(driver.launched)
        self.assertFalse(driver.mutating_operation_called)

    def test_new_run_skips_a_reel_saved_in_the_existing_data_directory(self) -> None:
        first_driver = FakeDriver([REEL_XML, REEL_XML])
        self.assertEqual(run_feed(CollectorOptions(max_items=1, delay_seconds=0), first_driver, self.store), 1)

        second_store = CollectionStore(self.store.data_dir)
        second_driver = FakeDriver([REEL_XML, REEL_XML])

        self.assertEqual(run_feed(CollectorOptions(max_items=1, delay_seconds=0), second_driver, second_store), 0)

    def test_run_hashtag_enters_each_query_and_collects_visible_reels(self) -> None:
        driver = FakeDriver([REEL_XML, REEL_XML, REEL_XML, REEL_XML])

        stored = run_hashtag(
            CollectorOptions(hashtags=("패션", "ootd"), max_items=2, delay_seconds=0),
            driver,
            self.store,
        )

        self.assertEqual(stored, 1)
        self.assertEqual(driver.entered_text, [])
        self.assertEqual(
            driver.opened_urls,
            [
                "https://www.instagram.com/explore/tags/%ED%8C%A8%EC%85%98/",
                "https://www.instagram.com/explore/tags/ootd/",
            ],
        )
        self.assertIn(("Reel by", "릴스"), driver.tapped_label_sets)

    def test_run_feed_prints_progress_for_each_saved_reel(self) -> None:
        driver = FakeDriver([REEL_XML, REEL_XML])
        output = io.StringIO()

        with redirect_stdout(output):
            run_feed(
                CollectorOptions(max_items=1, delay_seconds=0),
                driver,
                self.store,
            )

        self.assertIn("[1/1]", output.getvalue())
        self.assertIn("@odi.pigi", output.getvalue())

    def test_run_feed_records_media_stages_and_stats_when_diagnostics_are_enabled(self) -> None:
        driver = FakeDriver([REEL_XML, REEL_XML])
        diagnostics = CollectorDiagnostics(self.store.data_dir, run_mode="foreground")
        output = io.StringIO()

        with redirect_stdout(output):
            self.assertEqual(
                run_feed(
                    CollectorOptions(max_items=1, delay_seconds=0, diagnostics=diagnostics),
                    driver,
                    self.store,
                ),
                1,
            )
            diagnostics.finish("completed")

        events_path = next(self.store.data_dir.joinpath("logs").glob("instagram_events_*.jsonl"))
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        names = {event["event"] for event in events}
        result = next(event for event in events if event["event"] == "MEDIA_RESULT")

        self.assertTrue({"MEDIA_START", "MEDIA_RENDER_OK", "STAGE_START", "STAGE_SUCCESS", "STATS", "COLLECTOR_FINISH"} <= names)
        self.assertTrue(any(event.get("stage") == "SAVE_RESULT" for event in events))
        self.assertTrue(any(event.get("stage") == "READ_REPOST_COUNT" for event in events))
        self.assertTrue(any(event.get("stage") == "READ_SHARE_COUNT" for event in events))
        self.assertEqual(result["media_index"], 1)
        self.assertEqual(result["result"], "SUCCESS")
        self.assertTrue(result["success"])

    def test_verbose_progress_marks_collected_and_unavailable_fields(self) -> None:
        driver = FakeDriver([REEL_XML, REEL_XML])
        output = io.StringIO()

        with redirect_stdout(output):
            run_feed(
                CollectorOptions(max_items=1, delay_seconds=0, verbose_progress=True),
                driver,
                self.store,
            )

        text = output.getvalue()
        self.assertIn("username=collected(odi.pigi)", text)
        self.assertIn("view_count=unavailable", text)
        self.assertIn("account_country=unavailable", text)

    def test_preflight_stops_at_a_login_wall(self) -> None:
        with self.assertRaisesRegex(AccessBlockedError, "login_required"):
            preflight(FakeDriver(['<hierarchy><node text="Log in to continue"/></hierarchy>']))

    def test_capture_merges_exact_likes_and_views_from_the_detail_panel(self) -> None:
        panel_xml = """
        <hierarchy>
          <node text="Likes and plays" resource-id="com.instagram.android:id/title_text_view" />
          <node text="15,691" resource-id="com.instagram.android:id/like_count_text" content-desc="15691 likes" />
          <node text="624,267" resource-id="com.instagram.android:id/video_view_count_text" content-desc="624267 views" />
        </hierarchy>
        """
        driver = FakeDriver([REEL_XML, panel_xml], metric_panel_available=True)

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.metrics["like_count"].value, 15_691)
        self.assertEqual(observed.metrics["view_count"].value, 624_267)
        self.assertFalse(observed.like_count_is_private)
        self.assertEqual(driver.back_count, 1)
        self.assertNotEqual(observed.detail_evidence_paths.xml_path, observed.evidence_paths.xml_path)
        self.assertTrue(Path(observed.detail_evidence_paths.xml_path).exists())
        self.assertTrue(Path(observed.detail_evidence_paths.png_path).exists())

    def test_capture_reads_metrics_before_opening_the_author_profile(self) -> None:
        panel_xml = """
        <hierarchy>
          <node text="Likes and plays" resource-id="com.instagram.android:id/title_text_view" />
          <node text="15,691" resource-id="com.instagram.android:id/like_count_text" content-desc="15691 likes" />
          <node text="624,267" resource-id="com.instagram.android:id/video_view_count_text" content-desc="624267 views" />
        </hierarchy>
        """
        profile_xml = """
        <hierarchy>
          <node text="odi.pigi" resource-id="com.instagram.android:id/action_bar_title" />
          <node text="7" resource-id="com.instagram.android:id/profile_header_familiar_post_count_value" />
        </hierarchy>
        """
        # This sequence is only valid when the Likes and plays panel is opened
        # before the profile.  It prevents a profile round trip from making
        # the exact view-count control disappear.
        driver = FakeDriver(
            [REEL_XML, panel_xml, REEL_XML, profile_xml, REEL_XML],
            metric_panel_available=True,
            profile_available=True,
        )

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.metrics["like_count"].value, 15_691)
        self.assertEqual(observed.metrics["view_count"].value, 624_267)
        self.assertEqual(observed.profile.post_count, 7)
        self.assertEqual(driver.back_count, 2)

    def test_capture_uses_visible_share_copy_link_to_store_a_reel_url(self) -> None:
        share_sheet_xml = '<hierarchy><node text="Copy link" resource-id="com.instagram.android:id/copy_link_button" /></hierarchy>'
        driver = FakeDriver(
            [REEL_XML, share_sheet_xml, share_sheet_xml, REEL_XML],
            share_available=True,
            clipboard_text="https://www.instagram.com/reel/CODE123/?igsh=abc",
        )

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.reel_url, "https://www.instagram.com/reel/CODE123/?igsh=abc")
        self.assertEqual(driver.back_count, 1)

    def test_capture_keeps_the_reel_when_android_denies_clipboard_reading(self) -> None:
        share_sheet_xml = '<hierarchy><node text="Copy link" resource-id="com.instagram.android:id/copy_link_button" /></hierarchy>'
        driver = FakeDriver(
            [REEL_XML, share_sheet_xml, share_sheet_xml, REEL_XML],
            share_available=True,
            clipboard_error=True,
        )

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.reel_url, "")
        self.assertEqual(observed.username, "odi.pigi")
        self.assertEqual(driver.back_count, 1)

    def test_capture_reads_the_caption_sheet_upload_date_then_returns_to_reel(self) -> None:
        caption_sheet_xml = '<hierarchy><node text="April 28" /></hierarchy>'
        driver = FakeDriver(
            [REEL_XML, caption_sheet_xml, REEL_XML],
            caption_detail_available=True,
        )

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.uploaded_at, "2026-04-28")
        self.assertIn(("clips_caption_component", "caption_component"), driver.tapped_resource_sets)
        self.assertEqual(driver.back_count, 1)

    def test_capture_opens_comment_sheet_to_store_an_empty_thread_as_zero(self) -> None:
        reel_without_comment_count = re.sub(
            r'<node(?=[^>]*comment_count)[^>]*/>',
            '',
            REEL_XML,
        )
        comments_xml = '<hierarchy><node text="No comments yet" /><node text="Start the conversation." /></hierarchy>'
        driver = FakeDriver(
            [reel_without_comment_count, comments_xml, reel_without_comment_count],
            comment_panel_available=True,
        )

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.metrics["comment_count"].value, 0)
        self.assertIn(("comment_button", "comment_count", "comments_count"), driver.tapped_resource_sets)
        self.assertEqual(driver.back_count, 1)

    def test_capture_marks_a_disabled_comment_thread_as_unavailable(self) -> None:
        reel_without_comment_count = re.sub(
            r'<node(?=[^>]*comment_count)[^>]*/>',
            '',
            REEL_XML,
        )
        comments_xml = '<hierarchy><node text="Comments are turned off." /></hierarchy>'
        driver = FakeDriver(
            [reel_without_comment_count, comments_xml, reel_without_comment_count],
            comment_panel_available=True,
        )

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertNotIn("comment_count", observed.metrics)
        self.assertEqual(observed.visible_metrics["comment_count"], "comments_disabled")
        self.assertEqual(
            _metric_field_progress_value(observed, "comment_count"),
            "comment_count=unavailable(disabled)",
        )

    def test_likes_panel_return_recovers_a_reel_when_hashtag_navigation_lands_on_grid(self) -> None:
        panel_xml = """
        <hierarchy>
          <node text="Likes and plays" resource-id="com.instagram.android:id/title_text_view" />
          <node text="624,267" resource-id="com.instagram.android:id/video_view_count_text" content-desc="624267 views" />
        </hierarchy>
        """
        hashtag_grid_xml = """
        <hierarchy>
          <node resource-id="com.instagram.android:id/grid_card_layout_container" content-desc="Reel by creator at row 0, column 0" />
        </hierarchy>
        """
        driver = FakeDriver(
            [REEL_XML, panel_xml, hashtag_grid_xml, REEL_XML],
            metric_panel_available=True,
            hashtag_grid_available=True,
        )

        observed = capture_current_reel(
            CollectorOptions(delay_seconds=0, source_mode="hashtag", source_query="패션"),
            driver,
            self.store,
        )

        self.assertEqual(observed.metrics["view_count"].value, 624_267)
        self.assertIn(("grid_card_layout_container",), driver.tapped_resource_sets)
        self.assertEqual(driver.back_count, 1)

    def test_capture_reads_public_profile_fields_then_returns_to_reel(self) -> None:
        profile_xml = """
        <hierarchy>
          <node text="odi.pigi" resource-id="com.instagram.android:id/action_bar_title" />
          <node text="소개 문구" resource-id="com.instagram.android:id/profile_header_bio_text" />
          <node text="Artist" resource-id="com.instagram.android:id/profile_header_category_text" />
          <node text="7" resource-id="com.instagram.android:id/profile_header_familiar_post_count_value" />
          <node text="321" resource-id="com.instagram.android:id/profile_header_familiar_following_value" />
          <node text="1,169" resource-id="com.instagram.android:id/profile_header_familiar_followers_value" />
        </hierarchy>
        """
        driver = FakeDriver([REEL_XML, profile_xml, REEL_XML], profile_available=True)

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.profile.username, "odi.pigi")
        self.assertEqual(observed.profile.biography, "소개 문구")
        self.assertEqual(observed.profile.profile_category, "Artist")
        self.assertEqual(observed.profile.post_count, 7)
        self.assertEqual(observed.profile.following_count, 321)
        self.assertEqual(observed.profile.follower_count, 1_169)
        self.assertEqual(driver.back_count, 1)

    def test_capture_reuses_a_same_run_profile_without_opening_the_profile_again(self) -> None:
        cached_profile = ObservedProfile(
            biography="cached bio",
            profile_category="Clothing (Brand)",
            account_country="South Korea",
            post_count=101,
            following_count=29,
            follower_count=1_242,
        )
        driver = FakeDriver([REEL_XML])

        observed = capture_current_reel(
            CollectorOptions(delay_seconds=0, capture_screenshots=False),
            driver,
            self.store,
            profile_cache={"odi.pigi": cached_profile},
        )

        self.assertEqual(observed.profile.biography, "cached bio")
        self.assertEqual(observed.profile.follower_count, 1_242)
        self.assertNotIn(("clips_author_username", "author_username"), driver.tapped_resource_sets)
        self.assertEqual(observed.evidence_paths.png_path, "")

    def test_profile_return_recovers_a_reel_when_hashtag_navigation_lands_on_grid(self) -> None:
        profile_xml = """
        <hierarchy>
          <node text="odi.pigi" resource-id="com.instagram.android:id/action_bar_title" />
          <node text="7" resource-id="com.instagram.android:id/profile_header_familiar_post_count_value" />
        </hierarchy>
        """
        hashtag_grid_xml = """
        <hierarchy>
          <node resource-id="com.instagram.android:id/grid_card_layout_container" content-desc="Reel by creator at row 0, column 0" />
        </hierarchy>
        """
        driver = FakeDriver(
            [REEL_XML, profile_xml, hashtag_grid_xml, REEL_XML],
            profile_available=True,
            hashtag_grid_available=True,
        )

        observed = capture_current_reel(
            CollectorOptions(delay_seconds=0, source_mode="hashtag", source_query="패션"),
            driver,
            self.store,
        )

        self.assertEqual(observed.profile.post_count, 7)
        self.assertIn(("grid_card_layout_container",), driver.tapped_resource_sets)
        self.assertEqual(driver.back_count, 1)

    def test_capture_reads_account_country_through_the_profile_about_menu(self) -> None:
        profile_xml = """
        <hierarchy>
          <node text="odi.pigi" resource-id="com.instagram.android:id/action_bar_title" />
          <node text="7" resource-id="com.instagram.android:id/profile_header_familiar_post_count_value" />
        </hierarchy>
        """
        menu_xml = '<hierarchy><node text="About this account" /></hierarchy>'
        about_xml = """
        <hierarchy>
          <node text="Account based in" />
          <node text="Argentina" />
        </hierarchy>
        """
        driver = FakeDriver(
            [REEL_XML, profile_xml, menu_xml, about_xml, REEL_XML],
            profile_available=True,
            account_country_available=True,
        )

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.profile.account_country, "Argentina")
        # Current Android returns from About this account directly to the
        # profile.  The only remaining Back is profile -> Reel.
        self.assertEqual(driver.back_count, 2)

    def test_country_flow_dismisses_an_options_menu_only_when_it_remains(self) -> None:
        profile_xml = """
        <hierarchy>
          <node text="odi.pigi" resource-id="com.instagram.android:id/action_bar_title" />
          <node text="7" resource-id="com.instagram.android:id/profile_header_familiar_post_count_value" />
        </hierarchy>
        """
        menu_xml = '<hierarchy><node text="About this account" /></hierarchy>'
        about_xml = """
        <hierarchy>
          <node text="Account based in" />
          <node text="Argentina" />
        </hierarchy>
        """
        # Some app versions return About -> options menu.  In that variant the
        # helper has to close the menu before the profile helper returns to
        # the Reel, so three Back operations are correct.
        driver = FakeDriver(
            [REEL_XML, profile_xml, menu_xml, about_xml, menu_xml, REEL_XML],
            profile_available=True,
            account_country_available=True,
        )

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.profile.account_country, "Argentina")
        self.assertEqual(driver.back_count, 3)

    def test_capture_returns_to_reel_when_metric_sheet_is_still_loading(self) -> None:
        loading_sheet = '<hierarchy><node text="Likes and plays" /></hierarchy>'
        driver = FakeDriver([REEL_XML, loading_sheet], metric_panel_available=True)

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(observed.username, "odi.pigi")
        self.assertEqual(driver.back_count, 1)
        self.assertEqual(observed.metrics["like_count"].value, 4_699)
        self.assertNotIn("view_count", observed.metrics)

    def test_run_feed_does_not_save_a_loading_screen_without_an_author(self) -> None:
        empty = '<hierarchy><node text="Likes and plays" /></hierarchy>'
        driver = FakeDriver([empty])

        with self.assertRaises(CollectorError):
            run_feed(CollectorOptions(max_items=1, delay_seconds=0), driver, self.store)

        self.assertEqual(self.store.known_reel_fingerprints(), set())


if __name__ == "__main__":
    unittest.main()
