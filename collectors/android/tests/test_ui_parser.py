from __future__ import annotations

import unittest
from pathlib import Path

from android_collector.ui_parser import (
    detect_access_block,
    detect_rate_limit_signal,
    parse_account_country,
    parse_exact_count,
    parse_uploaded_at,
    parse_visible_profile,
    parse_visible_reel,
)


FIXTURE_XML = (Path(__file__).parent / "fixtures" / "reel_visible.xml").read_text(encoding="utf-8")


class UiParserTests(unittest.TestCase):
    def test_parse_exact_count_accepts_full_and_compact_visible_counts(self) -> None:
        self.assertEqual(parse_exact_count("32,357"), 32_357)
        self.assertEqual(parse_exact_count("4 699"), 4_699)
        self.assertEqual(parse_exact_count("5.7K"), 5_700)
        self.assertEqual(parse_exact_count("1.2M"), 1_200_000)
        self.assertEqual(parse_exact_count("1,5K"), 1_500)
        self.assertEqual(parse_exact_count("2.3만"), 23_000)
        self.assertIsNone(parse_exact_count("좋아요 4,699"))

    def test_detect_access_block_recognises_login_and_rate_limit(self) -> None:
        self.assertEqual(detect_access_block('<node text="Log in to continue"/>'), "login_required")
        self.assertEqual(detect_access_block('<node text="잠시 후 다시 시도하세요"/>'), "rate_limited")
        self.assertEqual(detect_rate_limit_signal('<node text="Too Many Requests (429)"/>'), "429")
        self.assertIsNone(detect_access_block('<node text="Reels"/>'))

    def test_parse_visible_reel_preserves_exact_metric_panel_label(self) -> None:
        observed = parse_visible_reel(
            FIXTURE_XML,
            source_mode="feed",
            source_query="",
            reel_url="",
            collected_at="2026-08-30T00:00:00Z",
        )

        self.assertEqual(observed.username, "odi.pigi")
        self.assertEqual(observed.caption, "갈비뼈를 표현해 봤습니다 ...")
        self.assertEqual(observed.metrics["like_count"].value, 4_699)
        self.assertEqual(observed.metrics["comment_count"].value, 32)
        self.assertEqual(observed.metrics["likes_and_plays_count"].value, 32_357)
        self.assertEqual(observed.metrics["likes_and_plays_count"].raw_text, "32,357")
        self.assertEqual(observed.reel_url, "")
        self.assertTrue(observed.reel_fingerprint)

    def test_parse_android_reel_accessibility_labels_keeps_exact_counts_and_caption(self) -> None:
        xml = """
        <hierarchy>
          <node text="yenoium" resource-id="com.instagram.android:id/clips_author_username" content-desc="yenoium" />
          <node text="" resource-id="com.instagram.android:id/clips_caption_component" content-desc="">
            <node text="" resource-id="" content-desc="#패션 #데일리룩" />
          </node>
          <node text="Like number is16393. View likes" resource-id="com.instagram.android:id/like_count" content-desc="Like number is16393. View likes" />
          <node text="Comment number is53. View comments" resource-id="com.instagram.android:id/comment_count" content-desc="Comment number is53. View comments" />
          <node text="Reposted 341 times" resource-id="com.instagram.android:id/repost_count" content-desc="Reposted 341 times" />
          <node text="Reshare number is7511" resource-id="com.instagram.android:id/ufi_text_component" content-desc="Reshare number is7511" />
        </hierarchy>
        """

        observed = parse_visible_reel(xml, "hashtag", "패션", "", "2026-08-30T00:00:00Z")

        self.assertEqual(observed.caption, "#패션 #데일리룩")
        self.assertEqual(observed.metrics["like_count"].value, 16_393)
        self.assertEqual(observed.metrics["like_count"].raw_text, "Like number is16393. View likes")
        self.assertEqual(observed.metrics["comment_count"].value, 53)
        self.assertEqual(observed.metrics["repost_count"].value, 341)
        self.assertEqual(observed.metrics["share_count"].value, 7_511)

    def test_parse_visible_reel_reads_audio_from_author_info_sibling(self) -> None:
        xml = """
        <hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/clips_author_username" />
          <node content-desc="Snoh Aalegra · Nothing Burns Like The Cold" />
          <node text="Follow" resource-id="com.instagram.android:id/inline_follow_button" />
          <node resource-id="com.instagram.android:id/clips_caption_component">
            <node content-desc="outfit #fashion" />
          </node>
          <node content-desc="Audio" resource-id="com.instagram.android:id/media_album_art_button" />
        </hierarchy>
        """

        observed = parse_visible_reel(xml, "hashtag", "fashion", "", "2026-08-30T00:00:00Z")

        self.assertEqual(observed.audio_name, "Snoh Aalegra · Nothing Burns Like The Cold")

    def test_parse_likes_and_plays_panel_reads_exact_likes_views_and_visibility(self) -> None:
        xml = """
        <hierarchy>
          <node text="Likes and plays" resource-id="com.instagram.android:id/title_text_view" />
          <node text="15,691" resource-id="com.instagram.android:id/like_count_text" content-desc="15691 likes" />
          <node text="624,267" resource-id="com.instagram.android:id/video_view_count_text" content-desc="624267 views" />
        </hierarchy>
        """

        observed = parse_visible_reel(xml, "feed", "", "", "2026-08-30T00:00:00Z")

        self.assertEqual(observed.metrics["like_count"].value, 15_691)
        self.assertEqual(observed.metrics["view_count"].value, 624_267)
        self.assertFalse(observed.like_count_is_private)

    def test_parse_likes_and_plays_panel_marks_explicitly_private_like_count(self) -> None:
        xml = """
        <hierarchy>
          <node text="Likes and plays" resource-id="com.instagram.android:id/title_text_view" />
          <node text="Only eunii.yamn can see the total number of likes on this reel." />
        </hierarchy>
        """

        observed = parse_visible_reel(xml, "feed", "", "", "2026-08-30T00:00:00Z")

        self.assertTrue(observed.like_count_is_private)

    def test_parse_comment_sheet_distinguishes_empty_from_disabled(self) -> None:
        empty_xml = '<hierarchy><node text="No comments yet" /><node text="Start the conversation." /></hierarchy>'
        disabled_xml = '<hierarchy><node text="Comments are turned off." /></hierarchy>'

        empty = parse_visible_reel(empty_xml, "feed", "", "", "2026-08-30T00:00:00Z")
        disabled = parse_visible_reel(disabled_xml, "feed", "", "", "2026-08-30T00:00:00Z")

        self.assertEqual(empty.metrics["comment_count"].value, 0)
        self.assertEqual(empty.metrics["comment_count"].raw_text, "No comments yet")
        self.assertNotIn("comment_count", disabled.metrics)
        self.assertEqual(disabled.visible_metrics["comment_count"], "comments_disabled")

    def test_parse_visible_profile_reads_exact_public_counts(self) -> None:
        xml = """
        <hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/action_bar_title" />
          <node text="daily style" resource-id="com.instagram.android:id/profile_header_bio_text" />
          <node text="Digital creator" resource-id="com.instagram.android:id/profile_header_category_text" />
          <node text="42" resource-id="com.instagram.android:id/profile_header_familiar_post_count_value" />
          <node text="321" resource-id="com.instagram.android:id/profile_header_familiar_following_value" />
          <node text="12,345" resource-id="com.instagram.android:id/profile_header_familiar_followers_value" />
        </hierarchy>
        """

        profile = parse_visible_profile(xml)

        self.assertEqual(profile.username, "creator")
        self.assertEqual(profile.biography, "daily style")
        self.assertEqual(profile.profile_category, "Digital creator")
        self.assertEqual(profile.post_count, 42)
        self.assertEqual(profile.following_count, 321)
        self.assertEqual(profile.follower_count, 12_345)

    def test_parse_visible_profile_and_reel_keep_compact_count_values(self) -> None:
        profile_xml = """
        <hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/action_bar_title" />
          <node text="10K" resource-id="com.instagram.android:id/profile_header_familiar_followers_value" />
        </hierarchy>
        """
        reel_xml = """
        <hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/clips_author_username" />
          <node text="1.2M" resource-id="com.instagram.android:id/save_count" />
        </hierarchy>
        """

        profile = parse_visible_profile(profile_xml)
        reel = parse_visible_reel(reel_xml, "feed", "", "", "2026-08-30T00:00:00Z")

        self.assertEqual(profile.follower_count, 10_000)
        self.assertEqual(reel.metrics["save_count"].value, 1_200_000)

    def test_parse_current_android_profile_business_category_and_compose_bio(self) -> None:
        xml = """
        <hierarchy>
          <node text="e__ensemble" resource-id="com.instagram.android:id/action_bar_title" />
          <node text="Clothing (Brand)" resource-id="com.instagram.android:id/profile_header_business_category" />
          <node resource-id="com.instagram.android:id/profile_user_info_compose_view">
            <node text="[ ensemble : 함께 ] 함께하는 쇼핑몰&#10;일상에 자연스럽게 함께하는 옷" />
            <node text="See translation" resource-id="ig_text" />
          </node>
          <node resource-id="com.instagram.android:id/profile_links_view" />
        </hierarchy>
        """

        profile = parse_visible_profile(xml)

        self.assertEqual(profile.biography, "[ ensemble : 함께 ] 함께하는 쇼핑몰 일상에 자연스럽게 함께하는 옷")
        self.assertEqual(profile.profile_category, "Clothing (Brand)")

    def test_parse_visible_reel_marks_the_standalone_ad_disclosure(self) -> None:
        xml = """
        <hierarchy>
          <node text="creator" resource-id="com.instagram.android:id/clips_author_username" />
          <node text="Ad" resource-id="com.instagram.android:id/clips_ad_label" />
        </hierarchy>
        """

        observed = parse_visible_reel(xml, "feed", "", "", "2026-08-30T00:00:00Z")

        self.assertTrue(observed.is_ad)

    def test_parse_account_country_reads_the_about_page_value_after_its_label(self) -> None:
        xml = """
        <hierarchy>
          <node text="Account based in" />
          <node text="Argentina" />
        </hierarchy>
        """

        self.assertEqual(parse_account_country(xml), "Argentina")

    def test_parse_uploaded_at_uses_collection_year_only_when_instagram_omits_it(self) -> None:
        current_year_xml = '<hierarchy><node text="April 28" /></hierarchy>'
        explicit_year_xml = '<hierarchy><node text="April 28, 2024" /></hierarchy>'

        self.assertEqual(
            parse_uploaded_at(current_year_xml, collected_at="2026-08-30T00:00:00Z"),
            "2026-04-28",
        )
        self.assertEqual(
            parse_uploaded_at(explicit_year_xml, collected_at="2026-08-30T00:00:00Z"),
            "2024-04-28",
        )


if __name__ == "__main__":
    unittest.main()
