import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from collectors import collection_pause as pause


class ManualPauseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        marker = patch.object(pause, "PAUSE_FILE", Path(self.directory.name) / "paused")
        marker.start()
        self.addCleanup(marker.stop)

    async def test_only_resume_releases_the_pause(self):
        pause.pause()
        self.assertFalse(pause.resume("anything"))
        self.assertTrue(pause.is_paused())
        self.assertTrue(pause.resume("resume"))
        self.assertFalse(pause.is_paused())

    async def test_browser_stays_open_until_explicit_resume(self):
        calls = []
        async def goto():
            calls.append("goto")
            return SimpleNamespace(status=429 if len(calls) == 1 else 200,
                                   url="https://www.instagram.com/reel/test/")
        async def wait_without_terminal():
            while pause.is_paused():
                await asyncio.sleep(0.001)
        with patch.object(pause, "wait", new=wait_without_terminal):
            task = asyncio.create_task(pause.guarded(goto)())
            try:
                await asyncio.sleep(0.02)
                self.assertTrue(pause.is_paused())
                self.assertFalse(task.done())
                self.assertEqual(calls, ["goto"])
                pause.resume("resume")
                result = await asyncio.wait_for(task, 1)
                self.assertEqual(result.status, 200)
                self.assertEqual(calls, ["goto", "goto"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_scheduler_does_not_cancel_a_paused_collector(self):
        entered = asyncio.Event()
        async def collect():
            pause.pause()
            entered.set()
            while pause.is_paused():
                await asyncio.sleep(0.001)
            return 7
        async def wait_without_terminal():
            while pause.is_paused():
                await asyncio.sleep(0.001)
        with patch.object(pause, "wait", new=wait_without_terminal):
            task = asyncio.create_task(pause.wait_for_active_time(collect(), timeout=0.01))
            try:
                await entered.wait()
                await asyncio.sleep(0.04)
                self.assertFalse(task.done())
                pause.resume("resume")
                self.assertEqual(await asyncio.wait_for(task, 1), 7)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_network_is_aborted_during_pause(self):
        context = SimpleNamespace(on=lambda *args: None, route=AsyncMock())
        await pause.install_context(context)
        handler = context.route.call_args.args[1]
        route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
        pause.pause()
        await handler(route)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()
        pause.resume("resume")
        await handler(route)
        route.continue_.assert_awaited_once()

    async def test_android_checks_pause_before_adb_command(self):
        from collectors.android_reel_metrics import AdbAndroidUiDriver
        driver = AdbAndroidUiDriver(Path("adb"), device_id="test-device")
        events = []
        def wait_sync():
            events.append("pause_gate")
        def run(*args, **kwargs):
            events.append("adb")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(pause, "wait_sync", new=wait_sync), patch("collectors.android_reel_metrics.subprocess.run", new=run):
            driver._run("shell", "input", "tap", "1", "1")
        self.assertEqual(events, ["pause_gate", "adb"])
