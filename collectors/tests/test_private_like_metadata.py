import unittest

from src.reels import instagram_reels_browser as browser


class PrivateLikeMetadataTests(unittest.TestCase):
    def test_hidden_count_overrides_numeric_placeholder_through_merges(self):
        for flag in ("like_and_view_counts_disabled", "hide_like_and_view_counts"):
            with self.subTest(flag=flag):
                hidden = browser.metadata_from_media({"like_count": 3, flag: True})
                self.assertTrue(browser.should_mark_like_count_unavailable({}, hidden))
                record = {"url": "https://www.instagram.com/reels/DOSKHEFgc3E/",
                          "shortcode": "DOSKHEFgc3E"}
                self.assertEqual(browser.build_collected_record(record, hidden)["like_count"], "X")
                for merged in (
                    browser.merge_reel_metadata({"likeCount": 3}, hidden),
                    browser.merge_reel_metadata(hidden, {"likeCount": 3}),
                    browser.merge_direct_reel_metadata(hidden, {"likeCount": 3}),
                ):
                    self.assertTrue(merged["likeCountPrivate"])
                    self.assertIsNone(merged["likeCount"])

    def test_public_three_likes_remain_valid(self):
        public = browser.metadata_from_media({"like_count": 3})
        self.assertFalse(browser.should_mark_like_count_unavailable({}, public))
        self.assertEqual(browser.merge_reel_metadata({}, public)["likeCount"], 3)
