from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from collectors.android_reel_metrics import (
    AdbAndroidUiDriver,
    AndroidMetricResult,
    AndroidMetricsError,
    AndroidReelMetricsEnricher,
    extract_related_hashtag_post_counts,
    merge_android_metrics,
    parse_hashtag_post_count,
    summarize_related_hashtag_counts,
    write_hashtag_post_counts,
)


REEL_XML = """
<hierarchy>
  <node text="creator" resource-id="com.instagram.android:id/clips_author_username" bounds="[0,0][100,100]" />
  <node content-desc="Like number is 4,699. View likes" bounds="[0,100][100,200]" />
  <node text="15.6K" resource-id="com.instagram.android:id/video_view_count" bounds="[0,200][100,300]" />
  <node text="32" resource-id="com.instagram.android:id/comment_count" bounds="[0,300][100,400]" />
  <node text="11.3K" resource-id="com.instagram.android:id/share_count" bounds="[0,400][100,500]" />
  <node text="138" resource-id="com.instagram.android:id/repost_count" bounds="[0,500][100,600]" />
  <node text="4" resource-id="com.instagram.android:id/save_count" bounds="[0,600][100,700]" />
  <node content-desc="Artist · Track name" bounds="[0,700][100,800]" />
</hierarchy>
"""
LIKES_PANEL_XML = """
<hierarchy>
  <node text="Likes and plays" resource-id="com.instagram.android:id/title_text_view" />
  <node text="15,691" resource-id="com.instagram.android:id/like_count_text" content-desc="15691 likes" />
  <node text="624,267" resource-id="com.instagram.android:id/video_view_count_text" content-desc="624267 views" />
</hierarchy>
"""


class FakeDriver:
    def __init__(self, xml: list[str]) -> None:
        self.xml = list(xml)
        self.opened_urls: list[str] = []
        self.tapped_bounds: list[str] = []
        self.back_count = 0

    def ensure_ready(self) -> None:
        return None

    def open_instagram_url(self, url: str) -> None:
        self.opened_urls.append(url)

    def dump_ui(self) -> str:
        return self.xml.pop(0) if len(self.xml) > 1 else self.xml[0]

    def tap_bounds(self, bounds: str) -> bool:
        self.tapped_bounds.append(bounds)
        return True

    def press_back(self) -> None:
        self.back_count += 1


class AndroidReelMetricTests(unittest.TestCase):
    def test_android_related_hashtag_summary_counts_unique_rendered_tags(self) -> None:
        rows = [
            {"collected_at": "2026-09-01T00:00:00Z", "query_hashtag": "fashion", "hashtag": "fashion", "status": "collected"},
            {"collected_at": "2026-09-01T00:00:00Z", "query_hashtag": "fashion", "hashtag": "fashionstyle", "status": "collected"},
            {"collected_at": "2026-09-01T00:00:00Z", "query_hashtag": "fashion", "hashtag": "FashionStyle", "status": "collected"},
        ]

        summaries = summarize_related_hashtag_counts(["fashion"], rows)

        self.assertEqual(summaries[0]["query_hashtag"], "fashion")
        self.assertEqual(summaries[0]["related_hashtag_count"], 2)
        self.assertEqual(summaries[0]["status"], "collected")

    def test_android_15_clipboard_fallback_replaces_stale_command_text(self) -> None:
        empty_search = """
        <hierarchy>
          <node text="" resource-id="com.instagram.android:id/action_bar_search_edit_text"
                class="android.widget.EditText" bounds="[100,100][900,200]" />
        </hierarchy>
        """
        filled_search = empty_search.replace('text=""', 'text="#패션"')
        driver = object.__new__(AdbAndroidUiDriver)
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        unsupported = subprocess.CompletedProcess(
            [], 0, stdout="No shell command implementation.\n", stderr=""
        )

        with (
            patch.object(driver, "open_instagram_url"),
            patch.object(driver, "dump_ui", side_effect=[empty_search, filled_search]),
            patch.object(driver, "tap_bounds", return_value=True),
            patch.object(
                driver,
                "_run",
                side_effect=lambda *args: unsupported if args[:4] == ("shell", "cmd", "clipboard", "set") else completed,
            ) as run,
            patch("collectors.android_reel_metrics._read_windows_clipboard_text", return_value=".\\collector.ps1 hashtag-posts --preset fashion-beauty"),
            patch("collectors.android_reel_metrics._set_windows_clipboard_text") as set_clipboard,
            patch("collectors.android_reel_metrics.time.sleep"),
        ):
            driver.open_instagram_search("#패션")

        set_clipboard.assert_any_call("#패션")
        set_clipboard.assert_any_call(".\\collector.ps1 hashtag-posts --preset fashion-beauty")
        self.assertIn(
            call("shell", "input", "keycombination", "113", "29"),
            run.call_args_list,
        )
        self.assertIn(
            call("shell", "input", "keyevent", "279"),
            run.call_args_list,
        )
        self.assertIn(
            call("shell", "input", "keyevent", "66"),
            run.call_args_list,
        )

    def test_android_search_retries_when_the_previous_tag_remains_selected(self) -> None:
        empty_search = """
        <hierarchy>
          <node text="" resource-id="com.instagram.android:id/action_bar_search_edit_text"
                class="android.widget.EditText" bounds="[100,100][900,200]" />
        </hierarchy>
        """
        stale_search = empty_search.replace('text=""', 'text="#패션스타그램"')
        expected_search = empty_search.replace('text=""', 'text="#패션"')
        driver = object.__new__(AdbAndroidUiDriver)
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with (
            patch.object(driver, "open_instagram_url"),
            patch.object(driver, "dump_ui", side_effect=[empty_search, stale_search, expected_search]),
            patch.object(driver, "tap_bounds", return_value=True),
            patch.object(driver, "_run", return_value=completed) as run,
            patch("collectors.android_reel_metrics.time.sleep"),
        ):
            driver.open_instagram_search("#패션")

        self.assertEqual(
            run.call_args_list.count(call("shell", "input", "keycombination", "113", "29")),
            2,
        )
        self.assertEqual(
            run.call_args_list.count(call("shell", "input", "keyevent", "279")),
            2,
        )

    def test_android_search_rejects_a_persistently_mismatched_pasted_query(self) -> None:
        empty_search = """
        <hierarchy>
          <node text="" resource-id="com.instagram.android:id/action_bar_search_edit_text"
                class="android.widget.EditText" bounds="[100,100][900,200]" />
        </hierarchy>
        """
        stale_search = empty_search.replace('text=""', 'text="hashtag-posts --preset fashion-beauty"')
        driver = object.__new__(AdbAndroidUiDriver)
        unsupported = subprocess.CompletedProcess(
            [], 0, stdout="No shell command implementation.\n", stderr=""
        )

        with (
            patch.object(driver, "open_instagram_url"),
            patch.object(driver, "dump_ui", side_effect=[empty_search, stale_search, stale_search, stale_search]),
            patch.object(driver, "tap_bounds", return_value=True),
            patch.object(driver, "_run", return_value=unsupported),
            patch("collectors.android_reel_metrics._read_windows_clipboard_text", return_value="before"),
            patch("collectors.android_reel_metrics._set_windows_clipboard_text"),
            patch("collectors.android_reel_metrics.time.sleep"),
        ):
            with self.assertRaisesRegex(AndroidMetricsError, "search input mismatch"):
                driver.open_instagram_search("#패션")

    def test_reel_detail_overrides_compact_counts_and_keeps_app_only_metrics(self) -> None:
        driver = FakeDriver([REEL_XML, LIKES_PANEL_XML])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        result = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(driver.opened_urls, ["https://www.instagram.com/reel/CODE123/"])
        self.assertEqual(result.metrics["like_count"], 15_691)
        self.assertEqual(result.metrics["view_count"], 624_267)
        self.assertEqual(result.metrics["comment_count"], 32)
        self.assertEqual(result.metrics["share_count"], 11_300)
        self.assertEqual(result.metrics["repost_count"], 138)
        self.assertEqual(result.metrics["saved_count"], 4)
        self.assertEqual(result.audio_name, "Artist · Track name")
        self.assertEqual(driver.back_count, 1)

    def test_empty_comment_sheet_is_saved_as_zero(self) -> None:
        reel_without_comment = REEL_XML.replace(
            '<node text="32" resource-id="com.instagram.android:id/comment_count" bounds="[0,300][100,400]" />',
            '<node content-desc="View comments" bounds="[0,300][100,400]" />',
        )
        comment_xml = "<hierarchy><node text=\"No comments yet\" /></hierarchy>"
        driver = FakeDriver([reel_without_comment, LIKES_PANEL_XML, comment_xml])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        result = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(result.metrics["comment_count"], 0)
        self.assertEqual(driver.back_count, 2)

    def test_metricless_reel_surface_is_reported_as_android_unavailable(self) -> None:
        metricless_reel = "<hierarchy><node text=\"creator\" resource-id=\"com.instagram.android:id/clips_author_username\" /></hierarchy>"
        driver = FakeDriver([metricless_reel])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with (
            patch("collectors.android_reel_metrics._ANDROID_REEL_READY_TIMEOUT_SECONDS", 0.1),
            patch("collectors.android_reel_metrics._ANDROID_RETRY_READY_TIMEOUT_SECONDS", 0.1),
        ):
            result = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(result.status, "unavailable")
        self.assertIn("no readable metric", result.error)
        self.assertEqual(len(driver.opened_urls), 1)

    def test_unrecognized_surface_reopens_the_reel_before_reporting_unavailable(self) -> None:
        loading_screen = "<hierarchy><node text=\"Home\" resource-id=\"com.instagram.android:id/home\" /></hierarchy>"
        driver = FakeDriver([loading_screen])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with (
            patch("collectors.android_reel_metrics._ANDROID_REEL_READY_TIMEOUT_SECONDS", 0.1),
            patch("collectors.android_reel_metrics._ANDROID_RETRY_READY_TIMEOUT_SECONDS", 0.1),
        ):
            result = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(len(driver.opened_urls), 2)

    def test_hashtag_post_count_accepts_compact_android_display(self) -> None:
        count, raw = parse_hashtag_post_count('<hierarchy><node text="15.6M posts" /></hierarchy>')

        self.assertEqual(count, 15_600_000)
        self.assertEqual(raw, "15.6M posts")

    def test_hashtag_post_count_exports_are_written_in_all_requested_formats(self) -> None:
        row = {
            "collected_at": "2026-08-31T00:00:00.000Z",
            "hashtag": "fashion",
            "media_count": "15.6M",
            "post_count": 15_600_000,
            "raw_post_count": "15.6M posts",
            "source": "android",
            "status": "collected",
            "error": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_hashtag_post_counts(Path(temporary), [row])

            self.assertTrue(all(path.is_file() for path in paths.values()))
            self.assertEqual(json.loads(paths["json"].read_text(encoding="utf-8"))[0]["media_count"], 15_600_000)

    def test_related_hashtag_search_keeps_the_compact_media_count(self) -> None:
        xml = """
        <hierarchy>
          <node text="#ootd" bounds="[100,200][400,250]" />
          <node text="54.1M posts" bounds="[100,250][400,300]" />
        </hierarchy>
        """

        rows = extract_related_hashtag_post_counts(xml, "ootd")

        self.assertEqual(rows[0]["hashtag"], "ootd")
        self.assertEqual(rows[0]["media_count"], "54.1M")
        self.assertEqual(rows[0]["post_count"], 54_100_000)

    def test_related_hashtag_search_pairs_each_tag_with_its_post_count(self) -> None:
        xml = """
        <hierarchy>
          <node text="Tags" bounds="[300,100][400,160]" />
          <node text="#오오티디" bounds="[100,200][400,250]" />
          <node text="54.1M posts" bounds="[100,250][400,300]" />
          <node text="#오오티디룩" bounds="[100,350][400,400]" />
          <node text="1.4M posts" bounds="[100,400][400,450]" />
        </hierarchy>
        """

        rows = extract_related_hashtag_post_counts(xml, "오오티디", collected_at="2026-08-31T00:00:00.000Z")

        self.assertEqual(
            [(row["query_hashtag"], row["hashtag"], row["post_count"]) for row in rows],
            [("오오티디", "오오티디", 54_100_000), ("오오티디", "오오티디룩", 1_400_000)],
        )

    def test_related_hashtag_search_skips_tags_under_one_thousand_posts(self) -> None:
        xml = """
        <hierarchy>
          <node text="#ootd" bounds="[100,200][400,250]" />
          <node text="1000+ posts" bounds="[100,250][400,300]" />
          <node text="#smalltag" bounds="[100,350][400,400]" />
          <node text="100+ posts" bounds="[100,400][400,450]" />
          <node text="#tiny" bounds="[100,500][400,550]" />
          <node text="fewer than 100 posts" bounds="[100,550][400,600]" />
        </hierarchy>
        """

        class SearchDriver:
            def ensure_ready(self) -> None:
                return None

            def open_instagram_search(self, _query: str) -> None:
                return None

            def dump_ui(self) -> str:
                return xml

            def tap_bounds(self, _bounds: str) -> bool:
                return True

            def scroll_down(self) -> None:
                return None

        enricher = AndroidReelMetricsEnricher(driver=SearchDriver(), ui_delay_seconds=0.1)
        published: list[dict[str, object]] = []
        with (
            patch("collectors.android_reel_metrics._ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS", 1),
            patch("collectors.android_reel_metrics.time.sleep"),
            patch("builtins.print") as printed,
        ):
            rows = enricher.collect_related_hashtag_post_counts(
                ["ootd"],
                on_rows=published.extend,
                progress_index=10,
                progress_total=30,
            )

        self.assertEqual([row["hashtag"] for row in rows], ["ootd"])
        self.assertEqual([row["post_count"] for row in rows], [1_000])
        self.assertEqual(rows[0]["media_count"], "1000")
        self.assertEqual(published, rows)
        printed.assert_any_call("[ANDROID hashtag 10/30] #ootd -> related 1")

    def test_related_hashtag_search_scrolls_beyond_its_first_page(self) -> None:
        first_page = """
        <hierarchy>
          <node text="Tags" bounds="[300,100][400,160]" />
          <node text="#ootd" bounds="[100,200][400,250]" />
          <node text="54.1M posts" bounds="[100,250][400,300]" />
        </hierarchy>
        """
        second_page = """
        <hierarchy>
          <node text="#ootd" bounds="[100,200][400,250]" />
          <node text="54.1M posts" bounds="[100,250][400,300]" />
          <node text="#ootdlook" bounds="[100,350][400,400]" />
          <node text="1.4M posts" bounds="[100,400][400,450]" />
        </hierarchy>
        """

        class SearchDriver:
            def __init__(self) -> None:
                self.opened_queries: list[str] = []
                self.scrolls = 0
                self.page = 0

            def ensure_ready(self) -> None:
                return None

            def open_instagram_url(self, _url: str) -> None:
                return None

            def open_instagram_search(self, query: str) -> None:
                self.opened_queries.append(query)

            def dump_ui(self) -> str:
                return first_page if self.page == 0 else second_page

            def tap_bounds(self, _bounds: str) -> bool:
                return True

            def press_back(self) -> None:
                return None

            def scroll_down(self) -> None:
                self.scrolls += 1
                self.page = 1

        driver = SearchDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        with patch("collectors.android_reel_metrics._ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS", 1):
            rows = enricher.collect_related_hashtag_post_counts(["ootd"])

        self.assertEqual(driver.opened_queries, ["#ootd"])
        self.assertGreaterEqual(driver.scrolls, 1)
        self.assertEqual([row["hashtag"] for row in rows], ["ootd", "ootdlook"])

    def test_related_hashtag_search_uses_the_first_ready_page_without_a_fixed_settle_wait(self) -> None:
        tags_results = """
        <hierarchy>
          <node text="Tags" bounds="[810,100][1080,200]" />
          <node text="#ootd" bounds="[100,250][400,300]" />
          <node text="54.1M posts" bounds="[100,300][400,350]" />
        </hierarchy>
        """

        class SearchDriver:
            def __init__(self) -> None:
                self.dumps = 0

            def ensure_ready(self) -> None:
                return None

            def open_instagram_search(self, _query: str) -> None:
                return None

            def dump_ui(self) -> str:
                self.dumps += 1
                return tags_results

            def tap_bounds(self, _bounds: str) -> bool:
                return True

            def scroll_down(self) -> None:
                return None

        driver = SearchDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with patch("collectors.android_reel_metrics.time.sleep") as sleep:
            xml = enricher._open_hashtag_search_results("ootd")

        self.assertEqual(xml, tags_results)
        self.assertEqual(driver.dumps, 1)
        sleep.assert_not_called()

    def test_android_scroll_caches_the_emulator_screen_size(self) -> None:
        driver = object.__new__(AdbAndroidUiDriver)
        driver.device_id = "emulator-5554"
        driver._cached_screen_size = None
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with patch.object(
            driver,
            "_run",
            side_effect=[
                subprocess.CompletedProcess([], 0, stdout="Physical size: 1080x2400\n", stderr=""),
                completed,
                completed,
            ],
        ) as run:
            driver.scroll_down()
            driver.scroll_down()

        self.assertEqual(
            [call.args[:3] for call in run.call_args_list].count(("shell", "wm", "size")),
            1,
        )
        self.assertEqual(
            [call.args[:3] for call in run.call_args_list].count(("shell", "input", "swipe")),
            2,
        )

    def test_related_hashtag_search_retries_when_instagram_overrides_the_first_tags_tap(self) -> None:
        for_you = """
        <hierarchy>
          <node content-desc="For you, 1 of 4." class="android.widget.TabWidget" bounds="[0,100][270,200]" />
          <node content-desc="Tags, 4 of 4." class="android.widget.TabWidget" bounds="[810,100][1080,200]" />
          <node text="Tags" class="android.widget.TextView" bounds="[905,130][985,170]" />
        </hierarchy>
        """
        tags_results = """
        <hierarchy>
          <node content-desc="Tags, 4 of 4." class="android.widget.TabWidget" bounds="[810,100][1080,200]" />
          <node text="#ootd" bounds="[100,250][400,300]" />
          <node text="54.1M posts" bounds="[100,300][400,350]" />
        </hierarchy>
        """

        class SearchDriver:
            def __init__(self) -> None:
                self.taps = 0

            def ensure_ready(self) -> None:
                return None

            def open_instagram_search(self, _query: str) -> None:
                return None

            def dump_ui(self) -> str:
                return tags_results if self.taps >= 2 else for_you

            def tap_bounds(self, bounds: str) -> bool:
                if bounds != "[810,100][1080,200]":
                    raise AssertionError(f"Expected the full Tags tab, got {bounds}")
                self.taps += 1
                return True

            def scroll_down(self) -> None:
                return None

        driver = SearchDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with patch("collectors.android_reel_metrics._ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS", 1):
            rows = enricher.collect_related_hashtag_post_counts(["ootd"])

        self.assertEqual(driver.taps, 2)
        self.assertEqual(rows[0]["hashtag"], "ootd")
        self.assertEqual(rows[0]["post_count"], 54_100_000)

    def test_related_hashtag_search_taps_the_clickable_tags_container(self) -> None:
        for_you = """
        <hierarchy>
          <node clickable="true" bounds="[810,100][1080,200]">
            <node text="Tags" class="android.widget.TextView" bounds="[905,130][985,170]" />
          </node>
        </hierarchy>
        """
        tags_results = """
        <hierarchy>
          <node text="#ootd" bounds="[100,250][400,300]" />
          <node text="54.1M posts" bounds="[100,300][400,350]" />
        </hierarchy>
        """

        class SearchDriver:
            def __init__(self) -> None:
                self.tapped_bounds: list[str] = []

            def ensure_ready(self) -> None:
                return None

            def open_instagram_search(self, _query: str) -> None:
                return None

            def dump_ui(self) -> str:
                return tags_results if self.tapped_bounds else for_you

            def tap_bounds(self, bounds: str) -> bool:
                self.tapped_bounds.append(bounds)
                return True

            def scroll_down(self) -> None:
                return None

        driver = SearchDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        rows = enricher.collect_related_hashtag_post_counts(["ootd"])

        self.assertEqual(driver.tapped_bounds, ["[810,100][1080,200]"])
        self.assertEqual(rows[0]["hashtag"], "ootd")

    def test_related_hashtag_search_uses_the_full_tab_hitbox_after_a_label_tap_fails(self) -> None:
        for_you = """
        <hierarchy>
          <node class="android.widget.EditText" text="#ootd" bounds="[100,50][900,120]" />
          <node text="Tags" class="android.widget.TextView" bounds="[915,150][985,180]" />
          <node class="android.view.View" bounds="[0,0][1080,2400]" />
        </hierarchy>
        """
        tags_results = """
        <hierarchy>
          <node text="#ootd" bounds="[100,250][400,300]" />
          <node text="54.1M posts" bounds="[100,300][400,350]" />
        </hierarchy>
        """

        class SearchDriver:
            def __init__(self) -> None:
                self.tapped_bounds: list[str] = []

            def ensure_ready(self) -> None:
                return None

            def open_instagram_search(self, _query: str) -> None:
                return None

            def dump_ui(self) -> str:
                return tags_results if len(self.tapped_bounds) >= 2 else for_you

            def tap_bounds(self, bounds: str) -> bool:
                self.tapped_bounds.append(bounds)
                return True

            def scroll_down(self) -> None:
                return None

        driver = SearchDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        rows = enricher.collect_related_hashtag_post_counts(["ootd"])

        self.assertEqual(driver.tapped_bounds[:2], ["[915,150][985,180]", "[810,120][1080,204]"])
        self.assertEqual(rows[0]["hashtag"], "ootd")

    def test_related_hashtag_search_waits_for_a_slow_tags_response(self) -> None:
        loading = """
        <hierarchy>
          <node content-desc="Tags, 4 of 4." class="android.widget.TabWidget" bounds="[810,100][1080,200]" />
        </hierarchy>
        """
        tags_results = """
        <hierarchy>
          <node content-desc="Tags, 4 of 4." class="android.widget.TabWidget" bounds="[810,100][1080,200]" />
          <node text="#ootd" bounds="[100,250][400,300]" />
          <node text="54.1M posts" bounds="[100,300][400,350]" />
        </hierarchy>
        """

        class SlowSearchDriver:
            def __init__(self) -> None:
                self.polls = 0
                self.taps = 0

            def ensure_ready(self) -> None:
                return None

            def open_instagram_search(self, _query: str) -> None:
                return None

            def dump_ui(self) -> str:
                self.polls += 1
                return tags_results if self.polls > 55 else loading

            def tap_bounds(self, _bounds: str) -> bool:
                self.taps += 1
                return True

            def scroll_down(self) -> None:
                return None

        driver = SlowSearchDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with patch("collectors.android_reel_metrics.time.sleep"):
            rows = enricher.collect_related_hashtag_post_counts(["ootd"])

        self.assertEqual(rows[0]["hashtag"], "ootd")
        self.assertEqual(rows[0]["post_count"], 54_100_000)
        self.assertEqual(rows[0]["status"], "collected")
        self.assertGreater(driver.taps, 0)

    def test_related_hashtag_search_waits_for_the_full_initial_tags_page(self) -> None:
        partial_results = """
        <hierarchy>
          <node text="Tags" bounds="[810,100][1080,200]" />
          <node text="#ootd" bounds="[100,250][400,300]" />
          <node text="54.1M posts" bounds="[100,300][400,350]" />
          <node text="#ootdlook" bounds="[100,400][400,450]" />
          <node text="1.4M posts" bounds="[100,450][400,500]" />
          <node text="#ootdstyle" bounds="[100,550][400,600]" />
          <node text="1.1M posts" bounds="[100,600][400,650]" />
        </hierarchy>
        """
        full_results = """
        <hierarchy>
          <node text="Tags" bounds="[810,100][1080,200]" />
          <node text="#ootd" bounds="[100,250][400,300]" />
          <node text="54.1M posts" bounds="[100,300][400,350]" />
          <node text="#ootdlook" bounds="[100,400][400,450]" />
          <node text="1.4M posts" bounds="[100,450][400,500]" />
          <node text="#ootdstyle" bounds="[100,550][400,600]" />
          <node text="1.1M posts" bounds="[100,600][400,650]" />
          <node text="#ootdfashion" bounds="[100,700][400,750]" />
          <node text="900K posts" bounds="[100,750][400,800]" />
          <node text="#ootdideas" bounds="[100,850][400,900]" />
          <node text="800K posts" bounds="[100,900][400,950]" />
          <node text="#ootdphotos" bounds="[100,1000][400,1050]" />
          <node text="700K posts" bounds="[100,1050][400,1100]" />
        </hierarchy>
        """

        class DelayedPageDriver:
            def __init__(self) -> None:
                self.polls = 0

            def ensure_ready(self) -> None:
                return None

            def open_instagram_search(self, _query: str) -> None:
                return None

            def dump_ui(self) -> str:
                self.polls += 1
                return full_results if self.polls > 20 else partial_results

            def tap_bounds(self, _bounds: str) -> bool:
                return True

            def scroll_down(self) -> None:
                return None

        driver = DelayedPageDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with patch("collectors.android_reel_metrics.time.sleep"):
            rows = enricher.collect_related_hashtag_post_counts(["ootd"])

        self.assertEqual([row["hashtag"] for row in rows], ["ootd", "ootdlook", "ootdstyle", "ootdfashion", "ootdideas", "ootdphotos"])

    def test_related_hashtag_search_uses_the_fourth_tab_when_tags_is_not_accessible(self) -> None:
        loading = """
        <hierarchy>
          <node class="android.widget.EditText" text="#ootd" bounds="[100,100][900,200]" />
          <node class="android.view.View" bounds="[0,0][1080,2400]" />
        </hierarchy>
        """
        tags_results = """
        <hierarchy>
          <node text="#ootd" bounds="[100,250][400,300]" />
          <node text="54.1M posts" bounds="[100,300][400,350]" />
        </hierarchy>
        """

        class SearchDriver:
            def __init__(self) -> None:
                self.tapped_bounds: list[str] = []

            def ensure_ready(self) -> None:
                return None

            def open_instagram_search(self, _query: str) -> None:
                return None

            def dump_ui(self) -> str:
                return tags_results if self.tapped_bounds else loading

            def tap_bounds(self, bounds: str) -> bool:
                self.tapped_bounds.append(bounds)
                return True

            def scroll_down(self) -> None:
                return None

        driver = SearchDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        rows = enricher.collect_related_hashtag_post_counts(["ootd"])

        self.assertEqual(driver.tapped_bounds, ["[810,200][1080,320]"])
        self.assertEqual(rows[0]["post_count"], 54_100_000)

    def test_exact_hashtag_post_count_uses_tags_search_and_keeps_only_the_query_tag(self) -> None:
        xml = """
        <hierarchy>
          <node text="Tags" bounds="[300,100][400,160]" />
          <node text="#ootd" bounds="[100,200][400,250]" />
          <node text="54.1M posts" bounds="[100,250][400,300]" />
          <node text="#ootdlook" bounds="[100,350][400,400]" />
          <node text="1.4M posts" bounds="[100,400][400,450]" />
        </hierarchy>
        """

        class SearchDriver:
            def __init__(self) -> None:
                self.opened_queries: list[str] = []
                self.taps = 0

            def ensure_ready(self) -> None:
                return None

            def open_instagram_url(self, _url: str) -> None:
                return None

            def open_instagram_search(self, query: str) -> None:
                self.opened_queries.append(query)

            def dump_ui(self) -> str:
                return xml

            def tap_bounds(self, _bounds: str) -> bool:
                self.taps += 1
                return True

            def press_back(self) -> None:
                return None

            def scroll_down(self) -> None:
                raise AssertionError("The exact first search result should not need a scroll")

        driver = SearchDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        rows = enricher.collect_hashtag_post_counts(["ootd"])

        self.assertEqual(driver.opened_queries, ["#ootd"])
        self.assertEqual(driver.taps, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["query_hashtag"], "ootd")
        self.assertEqual(rows[0]["hashtag"], "ootd")
        self.assertEqual(rows[0]["post_count"], 54_100_000)
        self.assertEqual(rows[0]["source"], "android_search_tags_exact")

    def test_hashtag_post_count_exports_append_new_rows(self) -> None:
        row = {
            "collected_at": "2026-08-31T00:00:00.000Z", "query_hashtag": "ootd",
            "hashtag": "ootd", "post_count": 54_100_000, "raw_post_count": "54.1M posts",
            "source": "android_search_tags", "status": "collected", "error": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            write_hashtag_post_counts(Path(temporary), [row])
            second = {**row, "collected_at": "2026-08-31T00:01:00.000Z", "hashtag": "ootdlook"}
            paths = write_hashtag_post_counts(Path(temporary), [second])

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertEqual([item["hashtag"] for item in saved], ["ootd", "ootdlook"])

    def test_hashtag_checkpoint_replaces_the_same_in_progress_snapshot(self) -> None:
        first = {
            "collected_at": "2026-08-31T00:00:00.000Z", "query_hashtag": "ootd",
            "hashtag": "ootd", "post_count": 54_100_000, "raw_post_count": "54.1M posts",
            "source": "android_search_tags", "status": "collected", "error": "",
        }
        corrected = {**first, "post_count": 54_123_456, "media_count": 54_123_456}
        with tempfile.TemporaryDirectory() as temporary:
            write_hashtag_post_counts(Path(temporary), [first])
            paths = write_hashtag_post_counts(
                Path(temporary), [corrected], replace_matching_snapshots=True,
            )
            saved = json.loads(paths["json"].read_text(encoding="utf-8"))

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["media_count"], 54_123_456)

    def test_hashtag_exports_number_snapshots_and_compare_media_counts(self) -> None:
        first = {
            "collected_at": "2026-09-01T00:00:00Z", "query_hashtag": "ootd", "related_hashtag_count": 1,
            "hashtag": "ootd", "media_count": "10K", "post_count": 10_000, "raw_post_count": "10K posts",
            "source": "android_search_tags", "status": "collected", "error": "",
        }
        second = {**first, "collected_at": "2026-09-01T02:30:00Z", "post_count": 10_025}
        with tempfile.TemporaryDirectory() as temporary:
            write_hashtag_post_counts(Path(temporary), [first])
            paths = write_hashtag_post_counts(Path(temporary), [second])
            saved = json.loads(paths["json"].read_text(encoding="utf-8"))

        self.assertEqual([row["collection_number"] for row in saved], [1, 2])
        self.assertEqual(saved[1]["hours_since_previous"], "2.5")
        self.assertEqual(saved[1]["media_count_change"], 25)

    def test_merge_overlays_only_android_owned_fields(self) -> None:
        browser_record = {
            "url": "https://www.instagram.com/reel/CODE123/",
            "title": "browser caption",
            "hashtags": "#fashion",
            "location_name": "Seoul",
            "view_count": 1,
            "audio_name": "browser audio",
        }

        merged = merge_android_metrics(
            browser_record,
            AndroidMetricResult(metrics={"view_count": 624_267, "share_count": 10}, audio_name="Android track"),
        )

        self.assertEqual(merged["title"], "browser caption")
        self.assertEqual(merged["hashtags"], "#fashion")
        self.assertEqual(merged["location_name"], "Seoul")
        self.assertEqual(merged["view_count"], 624_267)
        self.assertEqual(merged["share_count"], 10)
        self.assertEqual(merged["audio_name"], "Android track")

    def test_unavailable_android_result_preserves_python_metrics(self) -> None:
        browser_record = {
            "url": "https://www.instagram.com/reel/CODE123/",
            "view_count": 624_267,
            "like_count": 15_691,
            "comment_count": 32,
            "share_count": "",
            "repost_count": 138,
        }

        merged = merge_android_metrics(
            browser_record,
            AndroidMetricResult(
                status="unavailable",
                error="Instagram did not render a recognizable Reel surface before the timeout.",
            ),
        )

        self.assertEqual(merged["view_count"], 624_267)
        self.assertEqual(merged["like_count"], 15_691)
        self.assertEqual(merged["comment_count"], 32)
        self.assertEqual(merged["repost_count"], 138)


if __name__ == "__main__":
    unittest.main()
