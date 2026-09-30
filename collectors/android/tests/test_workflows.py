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
        self.assertEqual(self.store.rows[-1]["like_count"], 4_000)

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
        self.assertTrue(any(event.get("stage") == "READ_SAVED_COUNT" for event in events))
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

    def test_capture_reads_only_share_and_saved_without_detail_or_profile_taps(self) -> None:
        reel_xml = REEL_XML.replace(
            "</hierarchy>",
            '<node text="4" resource-id="com.instagram.android:id/save_count" /></hierarchy>',
        )
        driver = FakeDriver([reel_xml], metric_panel_available=True, profile_available=True,
                            caption_detail_available=True, comment_panel_available=True)

        observed = capture_current_reel(CollectorOptions(delay_seconds=0), driver, self.store)

        self.assertEqual(set(observed.metrics), {"share_count", "save_count"})
        self.assertEqual(observed.metrics["save_count"].value, 4)
        self.assertFalse(any("Like number is" in labels for labels in driver.tapped_label_sets))
        self.assertFalse(any("comment_button" in markers or "clips_author_username" in markers
                             for markers in driver.tapped_resource_sets))
        self.assertEqual(driver.back_count, 0)

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

    def test_run_feed_does_not_save_a_loading_screen_without_an_author(self) -> None:
        empty = '<hierarchy><node text="Likes and plays" /></hierarchy>'
        driver = FakeDriver([empty])

        with self.assertRaises(CollectorError):
            run_feed(CollectorOptions(max_items=1, delay_seconds=0), driver, self.store)

        self.assertEqual(self.store.known_reel_fingerprints(), set())


if __name__ == "__main__":
    unittest.main()
