from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from src.reels.android_reel_metrics import (
    AdbAndroidUiDriver,
    AndroidMetricResult,
    AndroidMetricsError,
    AndroidReelMetricsEnricher,
    extract_recollection_metrics,
    extract_visible_metrics,
    extract_related_hashtag_post_counts,
    merge_android_metrics,
    parse_hashtag_post_count,
    summarize_related_hashtag_counts,
    write_hashtag_post_counts,
    android_result_matches_reel,
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
LIKES_PANEL_WITH_ZERO_LIKES_XML = """
<hierarchy>
  <node text="Likes and plays" resource-id="com.instagram.android:id/title_text_view" />
  <node text="482" resource-id="com.instagram.android:id/video_view_count_text" content-desc="482 views" />
  <node text="Liked by" resource-id="com.instagram.android:id/header_text" />
</hierarchy>
"""
LIKES_PANEL_WITH_EMPTY_LIKE_CONTROL_XML = """
<hierarchy>
  <node text="Likes and plays" resource-id="com.instagram.android:id/title_text_view" />
  <node text="482" resource-id="com.instagram.android:id/video_view_count_text" content-desc="482 views" />
  <node content-desc="Likes" resource-id="com.instagram.android:id/like_icon" />
</hierarchy>
"""


class FakeDriver:
    def __init__(self, xml: list[str]) -> None:
        self.xml = list(xml)
        self.opened_urls: list[str] = []
        self.tapped_bounds: list[str] = []
        self.back_count = 0
        self.center_tap_count = 0
        self.dump_count = 0

    def ensure_ready(self) -> None:
        return None

    def open_instagram_url(self, url: str) -> None:
        self.opened_urls.append(url)

    def copy_current_reel_url(self) -> str:
        return self.opened_urls[-1] if self.opened_urls else ""

    def dump_ui(self) -> str:
        self.dump_count += 1
        return self.xml.pop(0) if len(self.xml) > 1 else self.xml[0]

    def tap_bounds(self, bounds: str) -> bool:
        self.tapped_bounds.append(bounds)
        return True

    def tap_screen_center(self) -> None:
        self.center_tap_count += 1

    def press_back(self) -> None:
        self.back_count += 1


class AndroidReelMetricTests(unittest.TestCase):
    def test_verified_caption_fills_absent_counts_and_likes_panel_supplies_views(self):
        preview = REEL_XML.replace('text="32"', 'text=""').replace('text="138"', 'text=""')
        preview = preview.replace('text="11.3K"', 'text=""').replace('text="4"', 'text=""')
        preview = preview.replace("</hierarchy>", """
          <node resource-id="com.instagram.android:id/like_count" bounds="[0,100][100,200]" />
          <node resource-id="com.instagram.android:id/clips_caption_component">
            <node text="겨울 코디를 소개합니다 #패션" clickable="true" bounds="[0,800][100,900]" />
          </node>
        </hierarchy>""")
        url = "https://www.instagram.com/reels/CODE123/"
        driver = FakeDriver([preview])
        enricher = AndroidReelMetricsEnricher(driver=driver)
        result = enricher.enrich(url, expected_username="creator", expected_caption="겨울 코디를 소개합니다")
        self.assertEqual({k: result.metrics[k] for k in ("comment_count", "repost_count", "share_count", "saved_count")},
                         {"comment_count": 0, "repost_count": 0, "share_count": 0, "saved_count": 0})
        self.assertEqual(driver.tapped_bounds, [])
        driver.xml = [preview, LIKES_PANEL_XML]
        views = enricher.collect_profile_view_count(url, "creator", expected_caption="겨울 코디를 소개합니다")
        self.assertEqual(views.metrics, {"view_count": 624267})
        self.assertEqual(views.observed_caption, "겨울 코디를 소개합니다")
        self.assertEqual(driver.tapped_bounds, ["[0,100][100,200]"])

    def test_instagram_anr_restarts_only_after_one_minute_and_reopens_same_reel(self):
        dialog = """<hierarchy>
          <node text="Instagram isn't responding" resource-id="android:id/alertTitle" />
          <node text="Close app" resource-id="android:id/aerr_close" bounds="[0,0][100,100]" />
          <node text="Wait" resource-id="android:id/aerr_wait" bounds="[0,100][100,200]" />
        </hierarchy>"""
        verified = REEL_XML.replace("</hierarchy>", """
          <node resource-id="com.instagram.android:id/clips_caption_component">
            <node text="겨울 코디를 소개합니다 #패션" />
          </node>
        </hierarchy>""")
        url = "https://www.instagram.com/reels/CODE123/"
        for persists in (True, False):
            with self.subTest(persists=persists):
                driver = FakeDriver([dialog, verified])
                now = [0.0]
                restarts = []
                driver.focused_instagram_anr = lambda: "window" if persists or now[0] < 11 else ""
                driver.restart_unresponsive_instagram = lambda: restarts.append(now[0])
                with patch("src.reels.android_reel_metrics.time.monotonic", side_effect=lambda: now[0]), \
                     patch("src.reels.android_reel_metrics.time.sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
                    result = AndroidReelMetricsEnricher(driver=driver).enrich(
                        url, expected_username="creator", expected_caption="겨울 코디를 소개합니다",
                    )
                self.assertEqual(restarts, [61.0] if persists else [])
                self.assertEqual(driver.opened_urls, [url, url] if persists else [url])
                self.assertEqual(result.metrics["saved_count"], 4)

    def test_matching_author_and_caption_verify_without_copying_link(self):
        caption = "겨울 바지 고민 끝… 요거 하나면 " + "가" * 320
        title = caption[:300] + "..."
        xml = REEL_XML.replace('text="138"', 'text=""').replace("</hierarchy>", f"""
          <node resource-id="com.instagram.android:id/clips_caption_component">
            <node content-desc="겨울 바지 고민 끝… …" clickable="true" bounds="[0,800][100,900]" />
          </node>
        </hierarchy>""")
        url = "https://www.instagram.com/reels/CODE123/"
        driver = FakeDriver([xml])
        def read_words():
            self.assertEqual(driver.xml, [xml])
            return [
                {"text": "138", "x": 10, "right": 40, "y": 510, "bottom": 530},
            ]
        driver.read_screen_ocr = read_words
        from src.reels.collection_diagnostics import CollectorDiagnostics

        with tempfile.TemporaryDirectory() as data_dir, patch.object(
            driver, "copy_current_reel_url", return_value="",
        ) as copy_url:
            diagnostics = CollectorDiagnostics(data_dir, run_mode="background", component="android")
            enricher = AndroidReelMetricsEnricher(driver=driver, diagnostics=diagnostics)
            result = enricher.enrich(
                url, expected_username="creator", expected_caption=title,
            )
            driver.xml = [xml]
            mismatch = enricher.enrich(
                url, expected_username="creator", expected_caption="다른 영상",
            )
            author_events = [
                event for line in diagnostics.events_path.read_text(encoding="utf-8").splitlines()
                if (event := json.loads(line))["event"] == "REEL_IDENTITY_AUTHOR_VERIFIED"
            ]
        self.assertEqual(len(author_events), 1)
        self.assertEqual(author_events[0]["current_url"], url)
        self.assertEqual(author_events[0]["reason"], "CAPTION_MISMATCH_AUTHOR_ACCEPTED")
        self.assertEqual(author_events[0]["expected_username"], "creator")
        copy_url.assert_not_called()
        self.assertEqual(driver.tapped_bounds, [])
        self.assertEqual(mismatch.status, "collected")
        self.assertTrue(mismatch.identity_author_only)
        self.assertEqual(mismatch.metrics["saved_count"], 4)
        self.assertTrue(android_result_matches_reel(vars(mismatch), {"url": url, "username": "creator", "title": "다른 영상"}))
        self.assertEqual(result.metrics["repost_count"], 138)
        self.assertEqual(result.metrics["saved_count"], 4)
        row = {"url": url, "username": "creator", "title": title}
        self.assertTrue(android_result_matches_reel(vars(result), row))
        self.assertFalse(android_result_matches_reel(vars(result), {**row, "username": "other"}))
        self.assertEqual(result.observed_caption, "겨울 바지 고민 끝… …")
        self.assertTrue(android_result_matches_reel(vars(result), {**row, "title": caption + " 새 영상"}))
        self.assertFalse(android_result_matches_reel(vars(result), {**row, "title": "다른 영상 " + caption}))

    def test_see_more_only_uses_matching_author_when_web_title_exists(self):
        url = "https://www.instagram.com/reels/CODE123/"
        xml = REEL_XML.replace("</hierarchy>", """
          <node resource-id="com.instagram.android:id/clips_caption_component">
            <node text="See more" />
          </node>
        </hierarchy>""")
        driver = FakeDriver([xml])
        row = {"url": url, "username": "creator", "title": "Saved web title"}
        result = AndroidReelMetricsEnricher(driver=driver).enrich(
            url, expected_username=row["username"], expected_caption=row["title"],
        )
        self.assertEqual(result.metrics["saved_count"], 4)
        self.assertTrue(android_result_matches_reel(vars(result), row))
        self.assertFalse(android_result_matches_reel(vars(result), {**row, "username": "other"}))
        self.assertEqual(driver.tapped_bounds, [])

    def test_missing_title_uses_hashtags_or_empty_caption_with_matching_author(self):
        url = "https://www.instagram.com/reels/CODE123/"
        tagged = REEL_XML.replace("</hierarchy>", """
          <node resource-id="com.instagram.android:id/clips_caption_component">
            <node text="#Winter #코디" />
          </node>
        </hierarchy>""")
        for xml, hashtags in ((tagged, "#코디 #winter"), (REEL_XML, "")):
            with self.subTest(hashtags=hashtags):
                result = AndroidReelMetricsEnricher(driver=FakeDriver([xml])).enrich(
                    url, expected_username="creator", expected_hashtags=hashtags,
                )
                self.assertEqual(result.metrics["saved_count"], 4)
                row = {"url": url, "username": "creator", "title": "", "hashtags": hashtags}
                self.assertTrue(android_result_matches_reel(vars(result), row))
                self.assertFalse(android_result_matches_reel(vars(result), {**row, "username": "other"}))
                self.assertEqual(android_result_matches_reel(vars(result), {**row, "hashtags": "#different"}), not hashtags)
        for hashtags in ("#different", ""):
            result = AndroidReelMetricsEnricher(driver=FakeDriver([tagged])).enrich(
                url, expected_username="creator", expected_hashtags=hashtags,
            )
            self.assertEqual(result.metrics["saved_count"], 4)
            self.assertTrue(result.identity_author_only)

        driver = FakeDriver([tagged.replace("#코디", "#코디…")])
        result = AndroidReelMetricsEnricher(driver=driver).enrich(
            url, expected_username="other", expected_hashtags="#different",
        )
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(driver.tapped_bounds, [])

    def test_hashtag_only_preview_verifies_with_web_title_and_partial_last_tag(self):
        from src.reels.android_metric_worker import _android_job_identity

        row = {"url": "https://www.instagram.com/reels/CODE123/", "username": "creator",
               "title": "다른 제목", "hashtags": "#겨울 #가나다라"}
        identity = _android_job_identity(Path("."), row)
        self.assertEqual(identity, {"expected_caption": row["title"], "expected_hashtags": row["hashtags"]})
        for preview, verified in (
            ("#겨울 #가나다…", True),
            ("#겨울 #가나다...", True),
            ("#겨울 #가나다..", True),
            ("#겨울 #가나다라", True),
            ("#겨울", True),
            ("#겨울 #가나다", True),
            ("#다른태그 #가나다…", False),
            ("#겨울 #라마바…", False),
            ("다른 본문 #겨울", False),
        ):
            with self.subTest(preview=preview):
                xml = REEL_XML.replace("</hierarchy>", f"""
                  <node resource-id="com.instagram.android:id/clips_caption_component">
                    <node text="{preview}" clickable="true" bounds="[0,800][100,900]" />
                  </node>
                </hierarchy>""")
                driver = FakeDriver([xml])
                result = AndroidReelMetricsEnricher(driver=driver).enrich(
                    row["url"], expected_username=row["username"], **identity,
                )
                self.assertEqual(result.status == "collected", verified)
                self.assertEqual(android_result_matches_reel(vars(result), row), verified)
                self.assertFalse(android_result_matches_reel(vars(result), {**row, "username": "other"}))
                self.assertEqual(driver.tapped_bounds, [])

    def test_caption_preview_with_hashtag_is_verified_or_skipped_without_taps(self):
        preview = REEL_XML.replace("</hierarchy>", """
          <node resource-id="com.instagram.android:id/comment_button" bounds="[0,800][100,900]" />
          <node resource-id="com.instagram.android:id/clips_caption_component">
            <node content-desc="오늘 입은 겨울 코디 #데일리룩" clickable="true" bounds="[0,900][100,1000]" />
          </node>
        </hierarchy>""")
        driver = FakeDriver([preview])
        enricher = AndroidReelMetricsEnricher(driver=driver)
        result = enricher.enrich(
            "https://www.instagram.com/reels/CODE123/",
            expected_username="creator", expected_caption="오늘 입은 겨울 코디",
        )
        driver.xml = [preview.replace("#데일리룩", "#데일리룩 …")]
        truncated = enricher.enrich(
            "https://www.instagram.com/reels/CODE123/",
            expected_username="creator", expected_caption="오늘 입은 겨울 코디",
        )
        self.assertEqual(driver.tapped_bounds, [])
        self.assertEqual(truncated.metrics["saved_count"], 4)
        self.assertEqual(result.metrics["saved_count"], 4)
        driver.xml = [preview.replace("#데일리룩", "#데일리룩 …"), """<hierarchy>
          <node text="creator said 오늘 입은 겨울 코디 #데일리룩" />
          <node resource-id="com.instagram.android:id/layout_comment_thread_edittext_multiline" />
        </hierarchy>"""]
        driver.read_screen_ocr = lambda: [
            {"text": "..", "x": 72, "right": 84, "y": 950, "bottom": 956},
            {"text": "…", "x": 72, "right": 84, "y": 750, "bottom": 756},
        ]
        expanded = enricher.enrich(
            "https://www.instagram.com/reels/CODE123/",
            expected_username="creator", expected_hashtags="#데일리룩",
        )
        self.assertEqual(driver.tapped_bounds, ["[72,950][84,956]"])
        self.assertEqual(expanded.metrics["saved_count"], 4)

    def test_ocr_fills_missing_count_only_inside_verified_metric_region(self):
        xml = REEL_XML.replace('text="11.3K"', 'text=""').replace(
            'text="138"', 'text=""')
        driver = FakeDriver([xml])
        driver.read_screen_ocr = lambda: [
            {"text": "1,234", "x": 10, "right": 55, "y": 410, "bottom": 430},
            {"text": "9.2K", "x": 10, "right": 55, "y": 510, "bottom": 530},
            {"text": "99", "x": 10, "right": 55, "y": 610, "bottom": 630},
        ]
        url = "https://www.instagram.com/reels/CODE123/"
        result = AndroidReelMetricsEnricher(driver=driver).enrich(url, expected_username="creator")
        self.assertEqual(result.metrics["share_count"], 1234)
        self.assertNotIn("repost_count", result.metrics)
        self.assertEqual(result.metrics["saved_count"], 4)

        driver.copy_current_reel_url = lambda: "https://www.instagram.com/reels/OTHER/"
        with patch.object(driver, "read_screen_ocr", wraps=driver.read_screen_ocr) as ocr:
            result = AndroidReelMetricsEnricher(driver=driver).enrich(url, expected_username="creator")
        ocr.assert_not_called()
        self.assertEqual(result.status, "unavailable")

    def test_collaboration_author_is_accepted_only_with_matching_permalink(self):
        xml = REEL_XML.replace('text="creator"', 'text="creator and co.author"')
        for username in ("creator", "co.author"):
            with self.subTest(username=username):
                driver = FakeDriver([xml])
                result = AndroidReelMetricsEnricher(driver=driver).enrich(
                    "https://www.instagram.com/reels/CODE123/", expected_username=username)
                self.assertEqual(result.status, "collected")
                self.assertTrue(android_result_matches_reel(vars(result), {
                    "url": "https://www.instagram.com/reels/CODE123/", "username": username}))
                self.assertFalse(android_result_matches_reel(vars(result), {
                    "url": "https://www.instagram.com/reels/OTHER/", "username": username}))
                self.assertFalse(android_result_matches_reel(vars(result), {
                    "url": "https://www.instagram.com/reels/CODE123/", "username": "creat"}))
        driver = FakeDriver([xml])
        driver.copy_current_reel_url = lambda: "https://www.instagram.com/reels/OTHER/"
        result = AndroidReelMetricsEnricher(driver=driver).enrich(
            "https://www.instagram.com/reels/CODE123/", expected_username="creator")
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.metrics, {})

    def test_series_reel_resolves_author_from_profile_or_media_nodes(self):
        url = "https://www.instagram.com/reels/Dfds7OPPLBc/"
        caption = "꾸안꾸 색감맛집 🍂🩵 @laaube_kr 상의 -도버 니트 세트"
        for subtitle in ('content-desc="From youngseo.h"', 'content-desc="Honey Veil · Table For One"'):
            with self.subTest(subtitle=subtitle):
                xml = REEL_XML.replace(
                    '<node text="creator" resource-id="com.instagram.android:id/clips_author_username" bounds="[0,0][100,100]" />',
                    f"""
                    <node content-desc="Reel by youngseo.h. Double tap to play or pause." resource-id="com.instagram.android:id/clips_media_component" />
                    <node text="Watch this series" content-desc="Watch this series" />
                    <node resource-id="com.instagram.android:id/clips_author_info_component">
                      <node content-desc="Profile picture of youngseo.h" resource-id="com.instagram.android:id/clips_author_profile_pic" />
                      <node text="My outfit" content-desc="My outfit" resource-id="com.instagram.android:id/clips_author_username" bounds="[0,0][100,100]" />
                      <node {subtitle} />
                    </node>
                    <node resource-id="com.instagram.android:id/clips_caption_component">
                      <node content-desc="꾸안꾸 색감맛집 🍂🩵 …" />
                    </node>
                    """,
                )
                enricher = AndroidReelMetricsEnricher(driver=FakeDriver([xml]), ui_delay_seconds=0.01)
                result = enricher.enrich(url, expected_username="youngseo.h", expected_caption=caption)
                self.assertEqual(result.status, "collected")
                self.assertEqual(result.observed_username, "youngseo.h")
                self.assertEqual(result.metrics["saved_count"], 4)
                self.assertTrue(android_result_matches_reel(vars(result), {
                    "url": url, "username": "youngseo.h", "title": caption,
                }))
                now = [0.0]
                with patch("src.reels.android_reel_metrics.time.monotonic", side_effect=lambda: now[0]), \
                     patch("src.reels.android_reel_metrics.time.sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
                    mismatch = AndroidReelMetricsEnricher(driver=FakeDriver([xml]), ui_delay_seconds=0.5).enrich(
                        url, expected_username="other.user", expected_caption=caption,
                    )
                self.assertEqual(mismatch.status, "unavailable")

    def test_caption_save_price_does_not_create_duplicate_metric(self):
        xml = REEL_XML.replace('</hierarchy>',
            '<node resource-id="com.instagram.android:id/clips_caption_component">'
            '<node content-desc="(광고) 29,990원 코디 저장 꾹 꾹 #패션" /></node></hierarchy>')
        result = AndroidReelMetricsEnricher(driver=FakeDriver([xml])).enrich(
            "https://www.instagram.com/reels/CODE123/", recollection=True,
            expected_username="creator", expected_caption="(광고) 29,990원 코디 저장 꾹 꾹")
        self.assertEqual(result.status, "collected")
        self.assertEqual(result.metrics["saved_count"], 4)
        xml = xml.replace('  <node text="4" resource-id="com.instagram.android:id/save_count" bounds="[0,600][100,700]" />', '')
        self.assertNotIn("saved_count", extract_recollection_metrics(xml)[0])

    def test_caption_numbers_never_become_metrics(self):
        for caption in ("29,990원 저장 꾹", "댓글 29990 이벤트 참여", "공유 수 29990 목표!", "29990 views of the city today"):
            xml = f'<hierarchy><node text="{caption}" resource-id="com.instagram.android:id/clips_caption" /></hierarchy>'
            self.assertEqual(extract_visible_metrics(xml), {}, caption)
            self.assertEqual(extract_recollection_metrics(xml)[0], {}, caption)

    def test_resource_less_metric_accessibility_labels_remain_readable(self):
        xml = '<hierarchy><node content-desc="Reshare number is 41" /><node content-desc="Save number is 21" /></hierarchy>'
        self.assertEqual(extract_recollection_metrics(xml)[0], {"share_count": 41, "saved_count": 21})

    def test_view_likes_without_a_number_is_private(self):
        xml = REEL_XML.replace('content-desc="Like number is 4,699. View likes"',
            'text="View likes" content-desc="View likes" resource-id="com.instagram.android:id/like_count"')
        result = AndroidReelMetricsEnricher(driver=FakeDriver([xml])).enrich(
            "https://www.instagram.com/reels/CODE123/", recollection=True)
        self.assertTrue(result.like_count_private)
        self.assertNotIn("like_count", result.metrics)

    def test_genuinely_duplicate_metric_controls_still_fail_closed(self):
        xml = REEL_XML.replace('</hierarchy>', '<node text="999" resource-id="save_count" /></hierarchy>')
        result = AndroidReelMetricsEnricher(driver=FakeDriver([xml])).enrich(
            "https://www.instagram.com/reels/CODE123/")
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.metrics, {})

    def test_ui_dump_extracts_xml_from_exec_out_combined_stream(self):
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        combined = "UI hierchary dumped to: /sdcard/probe.xml\n" + REEL_XML
        with patch.object(driver, "_run", return_value=subprocess.CompletedProcess([], 0, combined, "")), patch.object(driver, "reset_ui_automation") as reset:
            self.assertEqual(driver.dump_ui(), REEL_XML.strip())
        reset.assert_not_called()

    def test_valid_loading_surface_resets_consecutive_dump_errors(self):
        from unittest.mock import Mock
        driver = Mock()
        error = AndroidMetricsError("UIAutomator returned an empty or unreadable screen.")
        driver.dump_ui.side_effect = [error, '<hierarchy><node text="Loading" /></hierarchy>', error, REEL_XML]
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        with patch("src.reels.android_reel_metrics.time.sleep"):
            self.assertEqual(enricher._wait_for_surface(lambda xml: "creator" in xml, attempts=4), REEL_XML)

    def test_ui_dump_preserves_idle_failure_and_records_invalid_output(self):
        from unittest.mock import Mock
        diagnostics = Mock()
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554", diagnostics=diagnostics)
        result = subprocess.CompletedProcess([], 0, "", "ERROR: could not get idle state.")
        with patch.object(driver, "_run", return_value=result), patch.object(driver, "reset_ui_automation", return_value=True):
            with self.assertRaisesRegex(AndroidMetricsError, "could not get idle state"):
                driver.dump_ui()
        self.assertTrue(any(c.args[0] == "UI_DUMP_FAILED" for c in diagnostics.emit.call_args_list))

    def test_ui_dump_retries_idle_failure_without_losing_the_original_error(self):
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        xml = "<hierarchy><node /></hierarchy>"
        with patch.object(driver, "_run", side_effect=[AndroidMetricsError("ERROR: could not get idle state."), subprocess.CompletedProcess([], 0, xml)]), patch.object(driver, "reset_ui_automation", return_value=True):
            self.assertEqual(driver.dump_ui(), xml)

    def test_app_recovery_attempts_to_stabilize_reel_before_dump(self):
        from unittest.mock import Mock
        driver = Mock()
        driver.dump_ui.return_value = REEL_XML
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        with patch("src.reels.android_reel_metrics.time.sleep"):
            self.assertEqual(enricher.recover_app("https://www.instagram.com/reels/ONE/"), "")
        self.assertLess(driver.mock_calls.index(call.tap_screen_center()), driver.mock_calls.index(call.dump_ui()))

    def test_surface_wait_recovers_transient_dump_failure_but_not_disconnection(self):
        from unittest.mock import Mock
        driver = Mock()
        driver.dump_ui.side_effect = [AndroidMetricsError("UIAutomator returned an empty or unreadable screen."), REEL_XML]
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        with patch("src.reels.android_reel_metrics.time.sleep"):
            self.assertEqual(enricher._wait_for_surface(lambda xml: bool(xml), attempts=2), REEL_XML)
        driver.dump_ui.side_effect = AndroidMetricsError("device offline")
        driver.dump_ui.reset_mock()
        with self.assertRaisesRegex(AndroidMetricsError, "device offline"):
            enricher._wait_for_surface(lambda xml: bool(xml), attempts=4)
        driver.dump_ui.assert_called_once_with()

    def test_restart_dismisses_instagram_card_then_stops_and_reopens_app(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        driver._cached_screen_size = (1080, 1920)
        with patch.object(driver, "_run") as run, patch.object(driver, "launch_instagram") as launch, patch.object(driver, "open_instagram_url") as open_url, patch("src.reels.android_reel_metrics.time.sleep"):
            driver.restart_instagram("https://www.instagram.com/reels/ONE/")
        self.assertEqual(run.call_args_list, [
            call("shell", "input", "keyevent", "187"),
            call("shell", "input", "swipe", "540", "1056", "540", "96", "300"),
            call("shell", "am", "force-stop", "com.instagram.android"),
            call("shell", "input", "keyevent", "3"),
        ])
        self.assertEqual(launch.call_count, 2)
        open_url.assert_called_once_with("https://www.instagram.com/reels/ONE/")

    def test_failed_url_launch_opens_app_and_retries_same_reel_once(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(driver, "_run", side_effect=[AndroidMetricsError("unable to resolve Intent"), result]) as run, patch.object(driver, "launch_instagram") as launch, patch("src.reels.android_reel_metrics.time.sleep"):
            driver.open_instagram_url("https://www.instagram.com/reels/ONE/")
        launch.assert_called_once_with()
        self.assertEqual(run.call_args_list[0], run.call_args_list[1])

    def test_launcher_uses_resolved_component_and_waits_for_package_readiness(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        result = subprocess.CompletedProcess([], 0, "No activity found", "")
        ready = subprocess.CompletedProcess([], 0, "priority=0\ncom.instagram.android/.activity.MainTabActivity\n", "")
        with patch.object(driver, "_run", side_effect=[result, ready, result]) as run, patch("src.reels.android_reel_metrics.time.sleep"):
            driver.launch_instagram()
        self.assertIn("com.instagram.android/.activity.MainTabActivity", run.call_args.args)
        self.assertIn("android.intent.category.LAUNCHER", run.call_args.args)

    def test_launcher_failure_is_bounded_and_does_not_retry_url(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        with patch.object(driver, "_run", side_effect=AndroidMetricsError("device offline")) as run, patch("src.reels.android_reel_metrics.time.sleep"):
            with self.assertRaisesRegex(AndroidMetricsError, "Instagram app launch failed"):
                driver.open_instagram_url("https://www.instagram.com/reels/ONE/")
        self.assertEqual(run.call_count, 4)

    def test_caption_number_is_not_a_like_count(self) -> None:
        xml = '<hierarchy><node text="3가지 코디 좋아요 눌러주세요" resource-id="caption" /></hierarchy>'
        self.assertNotIn("like_count", extract_visible_metrics(xml))
        self.assertNotIn("like_count", extract_recollection_metrics(xml)[0])

    def test_hidden_like_label_overrides_accessibility_number(self) -> None:
        xml = REEL_XML.replace(
            'content-desc="Like number is 4,699. View likes"',
            'text="Likes" resource-id="clips_like_count" content-desc="Like number is 3. View likes"',
        )
        result = AndroidReelMetricsEnricher(driver=FakeDriver([xml]), ui_delay_seconds=0).enrich(
            "https://www.instagram.com/reels/test/"
        )
        self.assertTrue(result.like_count_private)
        self.assertEqual(merge_android_metrics({"like_count": ""}, result)["like_count"], "X")
        self.assertEqual(merge_android_metrics({"like_count": 3}, result)["like_count"], 3)

    def test_public_three_likes_is_preserved(self) -> None:
        xml = '<hierarchy><node text="3" resource-id="clips_like_count" content-desc="Like number is 3. View likes" /></hierarchy>'
        self.assertEqual(extract_visible_metrics(xml)["like_count"], 3)
        self.assertEqual(extract_recollection_metrics(xml)[0]["like_count"], 3)

    def test_recollection_prefers_exact_content_desc_and_marks_compact_only_fields(self) -> None:
        xml = """<hierarchy>
          <node text="1.2K" content-desc="Like number is 1,234. View likes" resource-id="like_count_text" />
          <node text="3.4K" resource-id="comment_count_text" />
          <node text="9" content-desc="9 reposts" resource-id="repost_count_text" />
          <node text="5.6K" resource-id="share_count_text" />
          <node text="7.8K" resource-id="save_count_text" />
          <node text="99K" content-desc="99,001 views" resource-id="video_view_count_text" />
        </hierarchy>"""

        metrics, compact = extract_recollection_metrics(xml)

        self.assertEqual(metrics, {
            "like_count": 1234, "comment_count": 3400, "repost_count": 9,
            "share_count": 5600, "saved_count": 7800,
        })
        self.assertEqual(compact, ("comment_count", "saved_count", "share_count"))

    @patch.dict("os.environ", {"INSTAGRAM_ANDROID_DEVICE_ID": "emulator-5560"})
    def test_adb_driver_uses_device_selected_by_launcher(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"))

        self.assertEqual(driver.device_id, "emulator-5560")

    def test_reset_connection_recreates_the_builtin_adb_driver(self) -> None:
        enricher = AndroidReelMetricsEnricher(ui_delay_seconds=0.1)
        first_driver = object()
        enricher._driver = first_driver

        enricher.reset_connection()

        self.assertIsNone(enricher._driver)

    def test_ui_dump_cleans_up_before_retry_after_ten_second_timeout(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        timed_out = AndroidMetricsError("ADB command timed out after 10s: exec-out sh -c")
        with (
            patch.object(driver, "_run", side_effect=[timed_out, subprocess.CompletedProcess([], 0, "<hierarchy><node /></hierarchy>")]) as run,
            patch.object(driver, "reset_ui_automation") as reset,
        ):
            self.assertEqual(driver.dump_ui(), "<hierarchy><node /></hierarchy>")

        self.assertEqual(run.call_count, 2)
        self.assertEqual([entry.kwargs["timeout_seconds"] for entry in run.call_args_list], [15.0, 15.0])
        reset.assert_called_once_with()

    def test_ui_dump_raises_after_two_ten_second_timeouts(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        timed_out = AndroidMetricsError("ADB command timed out after 10s: exec-out sh -c")
        with (
            patch.object(driver, "_run", side_effect=timed_out) as run,
            patch.object(driver, "reset_ui_automation") as reset,
        ):
            with self.assertRaisesRegex(AndroidMetricsError, "timed out after 10s"):
                driver.dump_ui()

        self.assertEqual(run.call_count, 2)
        self.assertEqual(reset.call_count, 2)

    def test_ui_dump_rejects_empty_output_and_checks_cleanup_failure(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        with patch.object(driver, "_run", return_value=subprocess.CompletedProcess([], 0, "ERROR: could not get idle state")), patch.object(driver, "reset_ui_automation", return_value=False) as reset:
            with self.assertRaisesRegex(AndroidMetricsError, "cleanup failed"):
                driver.dump_ui()
            reset.assert_called_once_with()

    def test_ui_recovery_restarts_app_only_when_ui_probe_fails(self) -> None:
        from unittest.mock import Mock
        driver = Mock()
        driver.dump_ui.side_effect = [AndroidMetricsError("empty or unreadable screen"), REEL_XML]
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        with patch("src.reels.android_reel_metrics.time.sleep"):
            self.assertEqual(enricher.recover_ui("https://www.instagram.com/reels/ONE/"), "")
        driver.restart_instagram.assert_called_once_with("https://www.instagram.com/reels/ONE/")
        driver.dump_ui.side_effect = None
        driver.dump_ui.return_value = REEL_XML
        driver.restart_instagram.reset_mock()
        self.assertEqual(enricher.recover_ui("https://www.instagram.com/reels/ONE/"), "")
        driver.restart_instagram.assert_not_called()

    def test_app_recovery_uses_selected_timeout_until_read_success_is_reset(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        xml = REEL_XML
        with patch.object(driver, "restart_instagram") as restart, patch.object(driver, "tap_screen_center"), patch.object(driver, "_run", return_value=subprocess.CompletedProcess([], 0, xml)) as run, patch("src.reels.android_reel_metrics.time.sleep"), patch("src.reels.android_reel_metrics.time.monotonic", return_value=0):
            self.assertEqual(enricher.recover_app("https://www.instagram.com/reels/ONE/", timeout_seconds=30.0), "")
            driver.dump_ui()
            self.assertEqual([entry.kwargs["timeout_seconds"] for entry in run.call_args_list], [30.0, 30.0])
            enricher.reset_ui_read_timeout()
            driver.dump_ui()
            self.assertEqual(run.call_args.kwargs["timeout_seconds"], 15.0)
        restart.assert_called_once_with("https://www.instagram.com/reels/ONE/")

    def test_app_recovery_preserves_restart_failure(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        enricher = AndroidReelMetricsEnricher(driver=driver)
        with patch.object(driver, "restart_instagram", side_effect=AndroidMetricsError("device offline")), patch.object(driver, "dump_ui") as dump:
            self.assertEqual(enricher.recover_app("https://www.instagram.com/reels/ONE/"), "device offline")
        dump.assert_not_called()

    def test_ui_recovery_is_bounded_and_preserves_connection_errors(self) -> None:
        from unittest.mock import Mock
        driver = Mock()
        driver.dump_ui.side_effect = AndroidMetricsError("UIAutomator returned an empty or unreadable screen.")
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        with patch("src.reels.android_reel_metrics.time.sleep"):
            self.assertIn("empty or unreadable", enricher.recover_ui("https://www.instagram.com/reels/ONE/"))
        self.assertEqual(driver.dump_ui.call_count, 2)
        driver.ensure_ready.side_effect = AndroidMetricsError("device offline")
        driver.restart_instagram.reset_mock()
        self.assertEqual(enricher.recover_ui("https://www.instagram.com/reels/ONE/"), "device offline")
        driver.restart_instagram.assert_not_called()

    def test_ui_recovery_checks_connection_before_ui_without_restarting_device(self):
        from unittest.mock import Mock
        driver = Mock()
        driver.dump_ui.return_value = REEL_XML
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)
        self.assertEqual(enricher.recover_ui("https://www.instagram.com/reels/ONE/"), "")
        self.assertLess(driver.mock_calls.index(call.ensure_ready()), driver.mock_calls.index(call.dump_ui()))
        driver.restart_instagram.assert_not_called()
        driver.restart_emulator.assert_not_called()
        driver.wait_for_boot.assert_not_called()

    def test_boot_wait_stops_when_emulator_never_returns(self):
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        with patch("src.reels.android_reel_metrics.time.monotonic", side_effect=lambda: now[0]), patch("src.reels.android_reel_metrics.time.sleep", side_effect=sleep), patch.object(driver, "_run", side_effect=AndroidMetricsError("device offline")):
            with self.assertRaisesRegex(AndroidMetricsError, "boot recovery timed out after 6s"):
                driver.wait_for_boot(timeout_seconds=6)
        self.assertEqual(now[0], 6)

    def test_ui_dump_does_not_retry_other_adb_errors(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        with (
            patch.object(driver, "_run", side_effect=AndroidMetricsError("adb.exe: device offline")) as run,
            patch.object(driver, "reset_ui_automation") as reset,
        ):
            with self.assertRaisesRegex(AndroidMetricsError, "device offline"):
                driver.dump_ui()
        run.assert_called_once()
        reset.assert_called_once_with()

    def test_connection_reset_retains_selected_device_and_discards_cached_driver(self) -> None:
        enricher = AndroidReelMetricsEnricher(device_id="emulator-5554")
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5560")
        driver._cached_screen_size = (1080, 1920)
        enricher._driver = driver
        with patch("src.reels.android_reel_metrics.subprocess.run") as run:
            enricher.reset_connection()
        self.assertEqual(enricher._device_id, "emulator-5560")
        self.assertIsNone(enricher._driver)
        self.assertEqual(enricher._last_ready_check, float("-inf"))
        run.assert_not_called()

    def test_readiness_probe_on_physical_device_only_checks_boot_and_package(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="R3CN30ABCDE")
        with patch.object(driver, "_run", side_effect=[
            subprocess.CompletedProcess([], 0, "1", ""),
            subprocess.CompletedProcess([], 0, "package:/data/app/com.instagram.android/base.apk", ""),
        ]) as run:
            driver.ensure_ready()
        self.assertEqual(run.call_args_list, [
            call("shell", "getprop", "sys.boot_completed"),
            call("shell", "pm", "path", "com.instagram.android"),
        ])

    def test_app_recovery_failure_preserves_device_without_host_commands(self) -> None:
        driver = AdbAndroidUiDriver(Path("adb.exe"), device_id="emulator-5554")
        enricher = AndroidReelMetricsEnricher(driver=driver)
        with patch.object(driver, "restart_instagram", side_effect=AndroidMetricsError("device offline")), \
             patch("src.reels.android_reel_metrics.subprocess.run") as run:
            self.assertEqual(enricher.recover_app("https://www.instagram.com/reels/ONE/"), "device offline")
        self.assertEqual(driver.device_id, "emulator-5554")
        run.assert_not_called()

    def test_connection_reset_preserves_injected_driver_and_uses_its_reset_hook(self) -> None:
        from unittest.mock import Mock
        driver = Mock()
        enricher = AndroidReelMetricsEnricher(driver=driver)
        enricher._last_ready_check = 10
        enricher.reset_connection()
        self.assertIs(enricher.driver, driver)
        self.assertEqual(enricher._last_ready_check, float("-inf"))
        driver.reset_connection.assert_called_once_with()
        driver.restart_emulator.assert_not_called()

    def test_device_readiness_is_checked_again_after_a_transient_disconnect(self) -> None:
        class ReconnectingDriver(FakeDriver):
            def __init__(self) -> None:
                super().__init__([REEL_XML, LIKES_PANEL_XML])
                self.ready_attempts = 0

            def ensure_ready(self) -> None:
                self.ready_attempts += 1
                if self.ready_attempts == 1:
                    raise AndroidMetricsError("adb.exe: device 'emulator-5554' not found")

        driver = ReconnectingDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        disconnected = enricher.enrich("https://www.instagram.com/reel/CODE123/")
        reconnected = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(disconnected.status, "unavailable")
        self.assertEqual(reconnected.status, "collected")
        self.assertEqual(driver.ready_attempts, 2)
        self.assertEqual(driver.center_tap_count, 1)

    def test_queue_idle_pause_taps_the_visible_reel_once(self) -> None:
        playing = REEL_XML.replace('</hierarchy>', '<node resource-id="com.instagram.android:id/clips_pause_button" content-desc="Pause" bounds="[10,20][30,40]" /></hierarchy>')
        paused = playing.replace('content-desc="Pause"', 'content-desc="Play"')
        driver = FakeDriver([playing, paused])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        self.assertTrue(enricher.pause_current_reel())
        self.assertTrue(enricher.pause_current_reel())
        self.assertEqual(driver.tapped_bounds, ["[10,20][30,40]"])
        self.assertEqual(driver.center_tap_count, 0)

    def test_queued_reel_pauses_before_reading_metrics(self) -> None:
        paused = REEL_XML.replace('</hierarchy>', '<node resource-id="com.instagram.android:id/clips_pause_button" content-desc="Play"/></hierarchy>')
        driver = FakeDriver([paused])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with patch("src.reels.android_reel_metrics.time.sleep") as sleep:
            result = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(result.status, "collected")
        self.assertEqual(driver.center_tap_count, 1)
        sleep.assert_called_once_with(1.0)
        self.assertTrue(enricher.pause_current_reel())
        self.assertEqual(driver.center_tap_count, 1)

    def test_queue_idle_pause_ignores_non_reel_surfaces(self) -> None:
        driver = FakeDriver(["<hierarchy><node text=\"Home\" /></hierarchy>"])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        self.assertFalse(enricher.pause_current_reel())
        self.assertEqual(driver.center_tap_count, 0)

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
            patch("src.reels.android_reel_metrics._read_windows_clipboard_text", return_value=".\\collector.ps1 hashtag-posts --preset fashion-beauty"),
            patch("src.reels.android_reel_metrics._set_windows_clipboard_text") as set_clipboard,
            patch("src.reels.android_reel_metrics.time.sleep"),
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
            patch("src.reels.android_reel_metrics.time.sleep"),
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
            patch("src.reels.android_reel_metrics._read_windows_clipboard_text", return_value="before"),
            patch("src.reels.android_reel_metrics._set_windows_clipboard_text"),
            patch("src.reels.android_reel_metrics.time.sleep"),
        ):
            with self.assertRaisesRegex(AndroidMetricsError, "search input mismatch"):
                driver.open_instagram_search("#패션")

    def test_reel_link_reads_four_visible_counts_without_opening_metric_sheets(self) -> None:
        driver = FakeDriver([REEL_XML, LIKES_PANEL_XML])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        result = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(driver.opened_urls, ["https://www.instagram.com/reel/CODE123/"])
        self.assertEqual(result.metrics, {"like_count": 4_699, "share_count": 11_300, "repost_count": 138, "saved_count": 4})
        self.assertEqual(driver.tapped_bounds, [])
        self.assertEqual(driver.back_count, 0)

    def test_recollection_reads_comment_content_desc_and_flags_compact_values(self) -> None:
        xml = """<hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/clips_author_username" />
          <node text="1.2K" content-desc="1,234 likes" resource-id="like_count_text" />
          <node text="56" content-desc="56 comments" resource-id="comment_count_text" />
          <node text="2.3K" resource-id="repost_count_text" />
          <node text="4.5K" resource-id="share_count_text" />
          <node text="6" content-desc="6 saves" resource-id="save_count_text" />
        </hierarchy>"""
        enricher = AndroidReelMetricsEnricher(driver=FakeDriver([xml]), ui_delay_seconds=0.1)

        result = enricher.enrich("https://www.instagram.com/reel/CODE123/", recollection=True)

        self.assertTrue(result.recollection)
        self.assertEqual(result.metrics, {
            "like_count": 1234, "comment_count": 56, "repost_count": 2300,
            "share_count": 4500, "saved_count": 6,
        })
        self.assertEqual(result.compact_fields, ("repost_count", "share_count"))

    def test_verified_countless_buttons_are_zero_but_absent_buttons_stay_missing(self) -> None:
        xml = """<hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/clips_author_username" />
          <node content-desc="Like" resource-id="like_button" />
          <node content-desc="Comment" resource-id="comment_button" />
          <node content-desc="Repost" class="android.widget.Button" />
          <node content-desc="Share" resource-id="direct_share_button" />
          <node content-desc="Save" resource-id="save_button" />
        </hierarchy>"""
        for recollection in (False, True):
            with self.subTest(recollection=recollection):
                result = AndroidReelMetricsEnricher(driver=FakeDriver([xml]), ui_delay_seconds=0.1).enrich(
                    "https://www.instagram.com/reel/CODE123/", expected_username="creator", recollection=recollection)
                self.assertEqual(result.metrics, {
                    "like_count": 0, "comment_count": 0, "repost_count": 0, "share_count": 0, "saved_count": 0})
                without_share = xml.replace('<node content-desc="Share" resource-id="direct_share_button" />', '')
                result = AndroidReelMetricsEnricher(driver=FakeDriver([without_share]), ui_delay_seconds=0.1).enrich(
                    "https://www.instagram.com/reel/CODE123/", expected_username="creator", recollection=recollection)
                self.assertNotIn("share_count", result.metrics)

    def test_countless_buttons_never_create_zero_for_unverified_reel(self) -> None:
        xml = """<hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/clips_author_username" />
          <node content-desc="Share" resource-id="share_button" />
        </hierarchy>"""
        driver = FakeDriver([xml])
        driver.copy_current_reel_url = lambda: "https://www.instagram.com/reels/OTHER/"
        result = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1).enrich(
            "https://www.instagram.com/reel/CODE123/", expected_username="creator")
        self.assertEqual(result.metrics, {})
        self.assertEqual(result.status, "unavailable")

    def test_visible_share_and_save_icons_without_counts_are_zero(self) -> None:
        reel_xml = """<hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/clips_author_username" />
          <node text="33" resource-id="com.instagram.android:id/comment_count" />
          <node content-desc="Share" resource-id="com.instagram.android:id/share_button" />
          <node content-desc="Save" resource-id="com.instagram.android:id/save_button" />
        </hierarchy>"""
        result = AndroidReelMetricsEnricher(driver=FakeDriver([reel_xml]), ui_delay_seconds=0.1).enrich(
            "https://www.instagram.com/reel/CODE123/"
        )
        self.assertEqual(result.metrics, {"share_count": 0, "saved_count": 0})

        numbered = reel_xml.replace('content-desc="Share"', 'text="0" content-desc="0 shares"').replace(
            'content-desc="Save"', 'text="0" content-desc="0 saves"')
        result = AndroidReelMetricsEnricher(driver=FakeDriver([numbered]), ui_delay_seconds=0.1).enrich(
            "https://www.instagram.com/reel/CODE123/"
        )
        self.assertEqual(result.metrics, {"share_count": 0, "saved_count": 0})

    def test_countless_reaction_buttons_are_zero_without_inventing_views(self) -> None:
        xml = """<hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/clips_author_username" />
          <node content-desc="Likes" resource-id="like_button" />
          <node content-desc="Views" resource-id="view_count" />
          <node content-desc="Comment" resource-id="comment_button" />
          <node text="6" content-desc="6 reposts" resource-id="repost_count" />
          <node content-desc="Repost" resource-id="repost_button" />
          <node content-desc="Share" resource-id="share_button" />
          <node content-desc="Save" resource-id="save_button" />
        </hierarchy>"""
        for recollection in (False, True):
            with self.subTest(recollection=recollection):
                result = AndroidReelMetricsEnricher(driver=FakeDriver([xml]), ui_delay_seconds=0.1).enrich(
                    "https://www.instagram.com/reel/CODE123/", recollection=recollection,
                )
                self.assertEqual(result.metrics, {"like_count": 0, "comment_count": 0, "repost_count": 6, "share_count": 0, "saved_count": 0})
                merged = merge_android_metrics({"comment_count": "", "like_count": "", "view_count": ""}, result)
                self.assertEqual(merged["comment_count"], 0)
                self.assertEqual(merged["like_count"], 0)
                self.assertEqual(merged["view_count"], "")

    def test_numbered_share_and_save_keep_values_with_blank_repost_icon(self) -> None:
        reel_xml = """<hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/clips_author_username" />
          <node content-desc="Repost" resource-id="com.instagram.android:id/repost_button" />
          <node text="19" resource-id="com.instagram.android:id/share_count" />
          <node content-desc="Share" resource-id="com.instagram.android:id/share_button" />
          <node text="4" resource-id="com.instagram.android:id/save_count" />
          <node content-desc="Save" resource-id="com.instagram.android:id/save_button" />
        </hierarchy>"""
        result = AndroidReelMetricsEnricher(driver=FakeDriver([reel_xml]), ui_delay_seconds=0.1).enrich(
            "https://www.instagram.com/reel/CODE123/"
        )
        self.assertEqual(result.metrics, {"repost_count": 0, "share_count": 19, "saved_count": 4})
        merged = merge_android_metrics({"view_count": "", "repost_count": "", "share_count": "", "saved_count": ""}, result)
        self.assertEqual(
            {field: merged[field] for field in ("repost_count", "share_count", "saved_count")},
            {"repost_count": 0, "share_count": 19, "saved_count": 4},
        )

        numbered_repost_xml = reel_xml.replace(
            '<node content-desc="Repost" resource-id="com.instagram.android:id/repost_button" />',
            '<node text="5" resource-id="com.instagram.android:id/repost_count" />'
            '<node content-desc="Repost" resource-id="com.instagram.android:id/repost_button" />',
        )
        numbered = AndroidReelMetricsEnricher(driver=FakeDriver([numbered_repost_xml]), ui_delay_seconds=0.1).enrich(
            "https://www.instagram.com/reel/CODE123/"
        )
        self.assertEqual(numbered.metrics["repost_count"], 5)

    def test_metricless_reel_surface_is_reported_as_android_unavailable(self) -> None:
        metricless_reel = "<hierarchy><node text=\"creator\" resource-id=\"com.instagram.android:id/clips_author_username\" /></hierarchy>"
        driver = FakeDriver([metricless_reel])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with (
            patch("src.reels.android_reel_metrics._ANDROID_REEL_READY_TIMEOUT_SECONDS", 0.1),
            patch("src.reels.android_reel_metrics._ANDROID_RETRY_READY_TIMEOUT_SECONDS", 0.1),
        ):
            result = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(result.status, "unavailable")
        self.assertIn("did not render a recognizable Reel surface", result.error)
        self.assertEqual(len(driver.opened_urls), 2)

    def test_unrecognized_surface_reopens_the_reel_before_reporting_unavailable(self) -> None:
        loading_screen = "<hierarchy><node text=\"Home\" resource-id=\"com.instagram.android:id/home\" /></hierarchy>"
        driver = FakeDriver([loading_screen])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with (
            patch("src.reels.android_reel_metrics._ANDROID_REEL_READY_TIMEOUT_SECONDS", 0.1),
            patch("src.reels.android_reel_metrics._ANDROID_RETRY_READY_TIMEOUT_SECONDS", 0.1),
        ):
            result = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(len(driver.opened_urls), 2)
        self.assertEqual(driver.back_count, 1)

    def test_unreadable_reel_surface_uses_the_full_retry_window(self) -> None:
        driver = FakeDriver([""])
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with (
            patch("src.reels.android_reel_metrics._ANDROID_REEL_READY_TIMEOUT_SECONDS", 5.0),
            patch("src.reels.android_reel_metrics._ANDROID_RETRY_READY_TIMEOUT_SECONDS", 2.0),
            patch("src.reels.android_reel_metrics.time.sleep"),
        ):
            result = enricher.enrich("https://www.instagram.com/reel/CODE123/")

        self.assertEqual(result.status, "unavailable")
        self.assertIn("empty or unreadable", result.error)
        self.assertEqual(driver.dump_count, 8)
        self.assertEqual(driver.back_count, 1)

    def test_dynamic_cta_hint_does_not_skip_an_unreadable_reel(self) -> None:
        class DynamicCtaDriver(FakeDriver):
            def __init__(self) -> None:
                super().__init__([""])
                self.reset_count = 0

            def is_dynamic_cta_reel(self) -> bool:
                return True

            def reset_instagram_surface(self) -> None:
                self.reset_count += 1

        driver = DynamicCtaDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with patch("src.reels.android_reel_metrics.time.sleep"):
            result = enricher.enrich("https://www.instagram.com/reel/SHOP123/")

        self.assertEqual(result.status, "unavailable")
        self.assertIn("empty or unreadable", result.error)
        self.assertEqual(len(driver.opened_urls), 2)
        self.assertEqual(driver.reset_count, 1)

    def test_plain_reel_with_unreadable_hierarchy_resets_instagram_before_retry(self) -> None:
        class UnreadableDriver(FakeDriver):
            def __init__(self) -> None:
                super().__init__([""])
                self.reset_count = 0

            def is_dynamic_cta_reel(self) -> bool:
                return False

            def reset_instagram_surface(self) -> None:
                self.reset_count += 1

        driver = UnreadableDriver()
        enricher = AndroidReelMetricsEnricher(driver=driver, ui_delay_seconds=0.1)

        with patch("src.reels.android_reel_metrics.time.sleep"):
            result = enricher.enrich("https://www.instagram.com/reel/PLAIN123/")

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(len(driver.opened_urls), 2)
        self.assertEqual(driver.reset_count, 1)

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
            patch("src.reels.android_reel_metrics._ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS", 1),
            patch("src.reels.android_reel_metrics.time.sleep"),
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
        with patch("src.reels.android_reel_metrics._ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS", 1):
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

        with patch("src.reels.android_reel_metrics.time.sleep") as sleep:
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

        with patch("src.reels.android_reel_metrics._ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS", 1):
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

        with patch("src.reels.android_reel_metrics.time.sleep"):
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

        with patch("src.reels.android_reel_metrics.time.sleep"):
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

    def test_merge_preserves_web_values_when_android_count_is_missing(self) -> None:
        browser_record = {
            "url": "https://www.instagram.com/reel/CODE123/",
            "title": "browser caption",
            "hashtags": "#fashion",
            "location_name": "Seoul",
            "view_count": 1,
            "like_count": 100,
            "repost_count": 9,
            "audio_name": "browser audio",
        }

        merged = merge_android_metrics(
            browser_record,
            AndroidMetricResult(metrics={"view_count": 624_267, "share_count": 10}, audio_name="Android track"),
        )

        self.assertEqual(merged["title"], "browser caption")
        self.assertEqual(merged["hashtags"], "#fashion")
        self.assertEqual(merged["location_name"], "Seoul")
        self.assertEqual(merged["view_count"], 1)
        self.assertEqual(merged["like_count"], 100)
        self.assertEqual(merged["repost_count"], 9)
        self.assertEqual(merged["share_count"], 10)
        self.assertEqual(merged["audio_name"], "browser audio")

    def test_web_likes_and_comments_take_priority_in_recollection(self) -> None:
        merged = merge_android_metrics(
            {"view_count": 1_200, "like_count": 100, "comment_count": 123, "repost_count": 9,
             "share_count": "", "saved_count": "", "follower_count": 500},
            AndroidMetricResult(metrics={"view_count": 1_234, "like_count": 50, "comment_count": 125,
                                         "repost_count": 0, "share_count": 19, "saved_count": 4},
                                recollection=True),
        )

        self.assertEqual(merged["view_count"], 1_200)
        self.assertEqual(merged["like_count"], 100)
        self.assertEqual(merged["comment_count"], 123)
        self.assertEqual(merged["repost_count"], 0)
        self.assertEqual(merged["share_count"], 19)
        self.assertEqual(merged["saved_count"], 4)
        self.assertEqual(merged["follower_count"], 500)

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

    def test_web_counts_preserved_when_android_reports_hidden_or_disabled(self) -> None:
        merged = merge_android_metrics(
            {"like_count": "999", "comment_count": "0", "view_count": 39_240},
            AndroidMetricResult(
                metrics={"view_count": 39_240},
                like_count_private=True,
                comment_count_disabled=True,
            ),
        )

        self.assertEqual(merged["like_count"], "999")
        self.assertEqual(merged["comment_count"], "0")
        self.assertEqual(merged["view_count"], 39_240)

    def test_low_like_reel_does_not_invent_missing_comment_count(self) -> None:
        merged = merge_android_metrics(
            {
                "view_count": 1_234,
                "like_count": 12,
                "comment_count": "",
                "repost_count": "",
                "share_count": "",
                "saved_count": "",
                "ad": "false",
            },
            AndroidMetricResult(),
        )

        self.assertEqual(
            {field: merged[field] for field in ("comment_count", "repost_count", "share_count", "saved_count")},
            {"comment_count": "", "repost_count": "", "share_count": "", "saved_count": ""},
        )

    def test_unavailable_android_result_does_not_invent_low_like_zeroes(self) -> None:
        merged = merge_android_metrics(
            {
                "like_count": 12,
                "comment_count": 2,
                "repost_count": "",
                "share_count": "",
                "saved_count": "",
                "ad": "false",
            },
            AndroidMetricResult(status="unavailable", error="UI hierarchy unavailable"),
        )

        self.assertEqual(merged["comment_count"], 2)
        self.assertEqual(merged["repost_count"], "")
        self.assertEqual(merged["share_count"], "")
        self.assertEqual(merged["saved_count"], "")

    def test_exact_view_does_not_invent_missing_share_and_saved_counts(self) -> None:
        merged = merge_android_metrics(
            {
                "view_count": 987_654,
                "like_count": 20_000,
                "comment_count": 15,
                "repost_count": "",
                "share_count": "",
                "saved_count": "",
                "ad": "false",
            },
            AndroidMetricResult(status="unavailable", error="detail panel closed"),
        )

        self.assertEqual(merged["repost_count"], "")
        self.assertEqual(merged["share_count"], "")
        self.assertEqual(merged["saved_count"], "")
        self.assertEqual(merged["comment_count"], 15)

    def test_zero_view_is_still_an_exact_collected_view_count(self) -> None:
        merged = merge_android_metrics(
            {"view_count": 0, "repost_count": "", "share_count": "", "saved_count": ""},
            AndroidMetricResult(status="unavailable"),
        )

        self.assertEqual(merged["repost_count"], "")
        self.assertEqual(merged["share_count"], "")
        self.assertEqual(merged["saved_count"], "")

    def test_ad_without_saved_count_remains_missing(self) -> None:
        merged = merge_android_metrics(
            {"like_count": 12, "saved_count": "", "ad": "true"},
            AndroidMetricResult(),
        )

        self.assertEqual(merged["saved_count"], "")

    def test_disabled_comments_remain_x_even_when_like_count_is_low(self) -> None:
        merged = merge_android_metrics(
            {"like_count": 12, "comment_count": "", "ad": "false"},
            AndroidMetricResult(comment_count_disabled=True),
        )

        self.assertEqual(merged["comment_count"], "X")


if __name__ == "__main__":
    unittest.main()
