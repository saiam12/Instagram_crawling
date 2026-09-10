from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from collectors import instagram_reels_browser as browser


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
