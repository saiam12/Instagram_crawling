from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reels import instagram_reels_browser as browser


class RateLimitRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_session_batches_do_not_overlap(self):
        runtime = SimpleNamespace(chromium=object(), stop=AsyncMock())
        manager = SimpleNamespace(start=AsyncMock(return_value=runtime))
        options = [SimpleNamespace(profile_dir=Path("profile"), no_login=False,
                                   data_dir=Path(name)) for name in ("fashion", "beauty")]
        active = 0
        peak = 0

        async def collect(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return 0

        with (patch.object(browser, "locate_browser_executable", return_value="browser"),
              patch.object(browser, "load_playwright", return_value=lambda: manager),
              patch.object(browser, "launch_collection_context", new=AsyncMock(return_value=(object(), object()))),
              patch.object(browser, "safe_close", new=AsyncMock()),
              patch.object(browser, "run_collector", new=collect)):
            self.assertEqual(await browser.run_collectors_in_shared_context(options), [0, 0])
        self.assertEqual(peak, 1)

    async def test_disabled_direct_api_does_not_open_retry_pages(self):
        detail = AsyncMock(return_value={})
        retry = AsyncMock(return_value={})
        with (patch.object(browser, "DIRECT_REEL_INFO_REQUESTS_ENABLED", False),
              patch.object(browser, "read_reel_detail_metadata", new=detail),
              patch.object(browser, "request_reel_info_metadata_from_reel_page", new=retry)):
            await browser.resolve_exact_reel_metrics(
                object(), "ABC123", {}, AsyncMock(return_value=SimpleNamespace(is_closed=lambda: False)),
                retry_delay_seconds=0,
            )
        retry.assert_not_awaited()
        detail.assert_awaited_once()

    async def test_first_429_waits_before_reopening_and_respects_retry_after(self):
        for retry_after, expected in ((None, 1200), (1800, 1800)):
            with self.subTest(retry_after=retry_after):
                events = []

                async def run_once(*args, **kwargs):
                    events.append("run")
                    if len(events) == 1:
                        raise browser.CrawlerAccessError(
                            "rate_limited", "HTTP 429", retry_after_seconds=retry_after)
                    return 0

                async def wait(event, seconds):
                    events.append(seconds)
                    return False

                with (patch.object(browser, "_run_collector_once", new=run_once),
                      patch.object(browser, "wait_for_stop_or_timeout", new=wait)):
                    self.assertEqual(await browser.run_collector(SimpleNamespace()), 0)
                self.assertEqual(events, ["run", expected, "run"])

    async def test_rate_limit_state_preserves_retry_after_on_exception(self):
        state = browser.InstagramRateLimitState()
        state.observe(SimpleNamespace(url="https://www.instagram.com/reel/ABC123/",
                                      status=429, headers={"retry-after": "1800"}))
        with self.assertRaises(browser.CrawlerAccessError) as raised:
            state.raise_if_limited()
        self.assertEqual(raised.exception.retry_after_seconds, 1800)

    async def test_second_429_after_manual_resume_is_terminal(self):
        state = browser.InstagramRateLimitState(manual_pause=True)
        first = SimpleNamespace(
            url="https://www.instagram.com/reel/first/",
            status=429,
            headers={},
        )
        second = SimpleNamespace(
            url="https://www.instagram.com/reel/second/",
            status=429,
            headers={},
        )
        with patch.object(browser.collection_pause, "pause") as pause, patch.object(
            browser.collection_pause, "wait", new=AsyncMock()
        ):
            state.observe(first)
            await state.wait_if_limited()
            self.assertEqual(state.resume_count, 1)
            state.observe(second)

        pause.assert_called_once()
        self.assertTrue(state.terminate_after_resume)
        with self.assertRaises(browser.CrawlerAccessError) as raised:
            await state.wait_if_limited()
        self.assertTrue(raised.exception.terminate_after_resume)

    async def test_terminal_rate_limit_does_not_enter_cooldown_loop(self):
        async def run_once(*args, **kwargs):
            raise browser.CrawlerAccessError(
                "rate_limited",
                "HTTP 429 after resume",
                terminate_after_resume=True,
            )

        with (
            patch.object(browser, "_run_collector_once", new=run_once),
            patch.object(browser, "wait_for_stop_or_timeout", new=AsyncMock()) as wait,
        ):
            result = await browser.run_collector(SimpleNamespace())

        self.assertEqual(result, 2)
        wait.assert_not_awaited()
