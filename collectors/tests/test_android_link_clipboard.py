import unittest
from unittest.mock import patch

from src.reels.android_reel_metrics import AdbAndroidUiDriver


class ReelLinkClipboardTests(unittest.TestCase):
    def copy_link(self, values):
        driver = object.__new__(AdbAndroidUiDriver)
        reel = '<hierarchy><node resource-id="com.instagram.android:id/direct_share_button" bounds="[0,0][10,10]" /></hierarchy>'
        menu = '<hierarchy><node text="Copy link" bounds="[20,20][30,30]" /></hierarchy>'
        with (
            patch.object(driver, 'dump_ui', side_effect=[reel, menu, reel]),
            patch.object(driver, 'tap_bounds', return_value=True),
            patch('src.reels.android_reel_metrics._read_windows_clipboard_text', side_effect=values),
            patch('src.reels.android_reel_metrics._set_windows_clipboard_text') as write,
            patch('src.reels.android_reel_metrics.time.sleep') as sleep,
        ):
            result = driver.copy_current_reel_url()
        self.assertEqual(write.call_args.args, ('original clipboard',))
        return result, sleep.call_count

    def test_temporary_empty_clipboard_does_not_reject_a_later_valid_link(self):
        url = 'https://www.instagram.com/reel/ACTUAL123/'
        for empty in (None, ''):
            with self.subTest(empty=empty):
                actual, polls = self.copy_link(['original clipboard', empty, url])
                self.assertEqual(actual, url)
                self.assertEqual(polls, 2)

    def test_persistently_empty_clipboard_uses_bounded_wait_and_returns_no_link(self):
        actual, polls = self.copy_link(['original clipboard'] + [None] * 5)
        self.assertEqual(actual, '')
        self.assertEqual(polls, 5)

    def test_non_instagram_link_is_still_rejected(self):
        actual, _ = self.copy_link(['original clipboard', 'https://example.com/reel/ACTUAL123/'])
        self.assertEqual(actual, '')
