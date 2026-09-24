from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
import tempfile

from android_collector.adb_driver import AdbDriver, select_online_device
from android_collector.models import CollectorError


class FakeRunner:
    def __init__(self, devices_output: str = "") -> None:
        self.devices_output = devices_output
        self.arguments: list[list[str]] = []
        self.keyword_arguments: list[dict[str, object]] = []
        self.screenshot_bytes = b"\x89PNG\r\n\x1a\nraw"
        self.clipboard_text = ""

    def __call__(self, arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        self.arguments.append(arguments)
        self.keyword_arguments.append(_kwargs)
        output = self.devices_output if arguments[-2:] == ["devices", "-l"] else ""
        if arguments[-4:] == ["shell", "cmd", "clipboard", "get"]:
            output = self.clipboard_text
        if arguments[-3:] == ["exec-out", "screencap", "-p"]:
            output = self.screenshot_bytes
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


class AdbDriverTests(unittest.TestCase):
    def test_select_online_device_returns_the_only_emulator(self) -> None:
        runner = FakeRunner("List of devices attached\nemulator-5554\tdevice product:sdk\n")

        actual = select_online_device(Path("adb"), None, Path(".adb-user"), runner)

        self.assertEqual(actual, "emulator-5554")
        environment = runner.keyword_arguments[0]["env"]
        self.assertEqual(environment["ANDROID_USER_HOME"], str(Path(".adb-user").resolve()))
        self.assertEqual(environment["ANDROID_SDK_HOME"], str(Path(".adb-user").resolve().parent))
        self.assertEqual(environment["HOME"], str(Path(".adb-user").resolve().parent))
        self.assertEqual(environment["USERPROFILE"], str(Path(".adb-user").resolve().parent))

    def test_select_online_device_accepts_space_aligned_adb_output(self) -> None:
        runner = FakeRunner(
            "List of devices attached\nemulator-5554          device product:sdk_gphone64_x86_64\n"
        )

        actual = select_online_device(Path("adb"), None, Path(".adb-user"), runner)

        self.assertEqual(actual, "emulator-5554")

    def test_select_online_device_requires_device_id_for_multiple_devices(self) -> None:
        runner = FakeRunner("List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n")

        with self.assertRaisesRegex(RuntimeError, "--device-id"):
            select_online_device(Path("adb"), None, Path(".adb-user"), runner)

    def test_open_reel_url_uses_an_android_view_intent(self) -> None:
        runner = FakeRunner()
        driver = AdbDriver(Path("adb"), "emulator-5554", Path(".adb-user"), runner)

        driver.open_reel_url("https://www.instagram.com/reel/ABC/")

        self.assertIn(
            ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "https://www.instagram.com/reel/ABC/"],
            [arguments[-7:] for arguments in runner.arguments],
        )

    def test_dump_ui_uses_one_compressed_adb_round_trip_for_playing_reels(self) -> None:
        runner = FakeRunner()
        driver = AdbDriver(Path("adb"), "emulator-5554", Path(".adb-user"), runner)

        driver.dump_ui()

        self.assertEqual(len(runner.arguments), 1)
        self.assertEqual(runner.arguments[0][-4:-1], ["exec-out", "sh", "-c"])
        self.assertIn("uiautomator dump --compressed /sdcard/window.xml", runner.arguments[0][-1])
        self.assertIn("cat /sdcard/window.xml", runner.arguments[0][-1])

    def test_press_back_uses_only_android_navigation(self) -> None:
        runner = FakeRunner()
        driver = AdbDriver(Path("adb"), "emulator-5554", Path(".adb-user"), runner)

        driver.press_back()

        self.assertIn(
            ["shell", "input", "keyevent", "4"],
            [arguments[-4:] for arguments in runner.arguments],
        )

    def test_read_clipboard_reads_android_clipboard_without_writing_it(self) -> None:
        runner = FakeRunner()
        runner.clipboard_text = "https://www.instagram.com/reel/ABC/"
        driver = AdbDriver(Path("adb"), "emulator-5554", Path(".adb-user"), runner)

        self.assertEqual(driver.read_clipboard(), "https://www.instagram.com/reel/ABC/")
        self.assertIn(
            ["shell", "cmd", "clipboard", "get"],
            [arguments[-4:] for arguments in runner.arguments],
        )

    def test_driver_commands_have_no_account_mutating_operations(self) -> None:
        runner = FakeRunner()
        driver = AdbDriver(Path("adb"), "emulator-5554", Path(".adb-user"), runner)

        driver.launch_instagram()
        driver.open_reel_url("https://www.instagram.com/reel/ABC/")
        driver.swipe_up()

        command_text = " ".join(" ".join(arguments) for arguments in runner.arguments)
        self.assertNotRegex(command_text, r"(?i)\b(like|follow|comment|send|post)\b")

    def test_capture_screenshot_keeps_adb_binary_output_unchanged(self) -> None:
        runner = FakeRunner()
        driver = AdbDriver(Path("adb"), "emulator-5554", Path(".adb-user"), runner)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "screen.png"
            driver.capture_screenshot(path)

            self.assertEqual(path.read_bytes(), runner.screenshot_bytes)
        screencap_call = next(
            values
            for arguments, values in zip(runner.arguments, runner.keyword_arguments)
            if arguments[-3:] == ["exec-out", "screencap", "-p"]
        )
        self.assertFalse(screencap_call["text"])

    def test_input_text_rejects_non_ascii_text_instead_of_pasting_the_clipboard(self) -> None:
        runner = FakeRunner()
        driver = AdbDriver(Path("adb"), "emulator-5554", Path(".adb-user"), runner)

        with self.assertRaisesRegex(CollectorError, "deep link"):
            driver.input_text("패션")

        self.assertEqual(runner.arguments, [])


if __name__ == "__main__":
    unittest.main()
