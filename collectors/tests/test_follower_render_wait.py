import unittest

from src.reels.instagram_reels_browser import request_web_follower_count


class FollowerRenderWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_header_is_read_before_failure(self):
        class Page:
            url = "https://www.instagram.com/example/"
            reads = 0
            waits = []

            async def goto(self, *_args, **_kwargs):
                return None

            async def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

            async def evaluate(self, script, *_args):
                self.assert_selector = "document.querySelector('header')" not in script
                self.reads += 1
                return {"profileText": "123 followers" if self.reads >= 3 else ""}

        page = Page()
        result = await request_web_follower_count(page, "example")
        self.assertEqual(result["followerCount"], 123)
        self.assertEqual(page.waits, [250, 250])
        self.assertTrue(page.assert_selector)

    async def test_empty_profile_wait_is_bounded_and_reports_body(self):
        class Page:
            url = "https://www.instagram.com/example/"
            reads = 0

            async def goto(self, *_args, **_kwargs):
                return None

            async def wait_for_timeout(self, _milliseconds):
                pass

            async def evaluate(self, *_args):
                self.reads += 1
                return {"profileText": "", "bodyExcerpt": "Please try again later"}

        page = Page()
        result = await request_web_follower_count(page, "example")
        self.assertEqual(result["status"], "web_unavailable")
        self.assertEqual(page.reads, 25)
        self.assertIn("Please try again later", result["error"])
