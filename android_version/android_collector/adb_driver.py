from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from .driver import AndroidDriver
from .models import CollectorError
from .ui_parser import parse_ui_xml


INSTAGRAM_PACKAGE = "com.instagram.android"
Runner = Callable[..., Any]


def _environment(adb_user_home: Path) -> dict[str, str]:
    android_user_home = adb_user_home.resolve()
    home_base = android_user_home.parent
    return {
        **os.environ,
        "ANDROID_USER_HOME": str(android_user_home),
        "ANDROID_SDK_HOME": str(home_base),
        "HOME": str(home_base),
        "USERPROFILE": str(home_base),
    }


def _run_adb(
    adb_path: Path,
    arguments: Sequence[str],
    adb_user_home: Path,
    runner: Runner,
    *,
    binary: bool = False,
) -> Any:
    keyword_arguments: dict[str, object] = {
        "capture_output": True,
        "text": not binary,
        "env": _environment(adb_user_home),
        "check": False,
    }
    if not binary:
        keyword_arguments.update({"encoding": "utf-8", "errors": "replace"})
    result = runner([str(adb_path), *arguments], **keyword_arguments)
    if int(getattr(result, "returncode", 0)) != 0:
        error = str(getattr(result, "stderr", "") or getattr(result, "stdout", "")).strip()
        raise CollectorError(error or f"ADB command failed: {' '.join(arguments)}")
    return result


def select_online_device(
    adb_path: Path,
    device_id: str | None,
    adb_user_home: Path,
    runner: Runner = subprocess.run,
) -> str:
    """Select exactly one online device without modifying global ADB settings."""
    result = _run_adb(adb_path, ("devices", "-l"), adb_user_home, runner)
    devices = []
    for line in str(getattr(result, "stdout", "")).splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[1] == "device":
            devices.append(columns[0])
    if device_id:
        if device_id not in devices:
            raise CollectorError(f"ADB device '{device_id}' is not online.")
        return device_id
    if not devices:
        raise CollectorError("No online Android emulator was found. Start an Android Studio emulator first.")
    if len(devices) > 1:
        raise CollectorError("More than one Android device is online; pass --device-id.")
    return devices[0]


class AdbDriver(AndroidDriver):
    """ADB/UIAutomator implementation limited to read-only navigation actions."""

    def __init__(
        self,
        adb_path: Path,
        device_id: str,
        adb_user_home: Path,
        runner: Runner = subprocess.run,
    ) -> None:
        self.adb_path = adb_path
        self.device_id = device_id
        self.adb_user_home = adb_user_home
        self.runner = runner

    def _run(self, *arguments: str, binary: bool = False) -> Any:
        return _run_adb(
            self.adb_path,
            ("-s", self.device_id, *arguments),
            self.adb_user_home,
            self.runner,
            binary=binary,
        )

    def ensure_ready(self) -> None:
        package_path = self._run("shell", "pm", "path", INSTAGRAM_PACKAGE)
        if not str(getattr(package_path, "stdout", "")).strip():
            raise CollectorError("Instagram is not installed on the selected Android device.")

    def launch_instagram(self) -> None:
        self._run("shell", "monkey", "-p", INSTAGRAM_PACKAGE, "1")

    def dump_ui(self) -> str:
        # Keep the Android-side temporary file, but create it and stream it
        # back through one ADB transport.  The earlier implementation paid a
        # separate host-to-device round trip for `uiautomator dump` and then
        # another one for `cat`; this method is called at every Reel/panel
        # transition, so combining those commands is a material speed-up that
        # does not weaken any parser or evidence data.
        command = "uiautomator dump --compressed /sdcard/window.xml >/dev/null && cat /sdcard/window.xml"
        return str(self._run("exec-out", "sh", "-c", command).stdout)

    @staticmethod
    def _center(bounds: str) -> tuple[int, int] | None:
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if match is None:
            return None
        left, top, right, bottom = (int(value) for value in match.groups())
        return (left + right) // 2, (top + bottom) // 2

    def tap_text(self, labels: Sequence[str], *, ui_xml: str | None = None) -> bool:
        """Tap a matching accessible control, optionally from a fresh UI dump.

        The compact Reel controls fade after a short delay.  A caller that has
        just dumped the UI can supply that exact XML and tap its bounds without
        losing the control while a second ADB dump or screenshot is running.
        """
        wanted = tuple(label.casefold() for label in labels)
        for node in parse_ui_xml(ui_xml if ui_xml is not None else self.dump_ui()):
            visible = node.visible_text.casefold()
            if visible and any(label in visible for label in wanted):
                point = self._center(node.bounds)
                if point is not None:
                    self._run("shell", "input", "tap", str(point[0]), str(point[1]))
                    return True
        return False

    def tap_resource_id(self, markers: Sequence[str], *, ui_xml: str | None = None) -> bool:
        """Tap a control selected by its stable Android resource-id fragment."""
        wanted = tuple(marker.casefold() for marker in markers)
        for node in parse_ui_xml(ui_xml if ui_xml is not None else self.dump_ui()):
            resource_id = node.resource_id.casefold()
            if any(marker in resource_id for marker in wanted):
                point = self._center(node.bounds)
                if point is not None:
                    self._run("shell", "input", "tap", str(point[0]), str(point[1]))
                    return True
        return False

    def input_text(self, value: str) -> None:
        """Enter search text, including Korean hashtags that input text cannot type reliably."""
        if not value.isascii():
            raise CollectorError(
                "This Android image cannot type non-ASCII text through ADB; use the hashtag deep link workflow."
            )
        escaped = value.replace(" ", "%s")
        self._run("shell", "input", "text", escaped)

    def swipe_up(self) -> None:
        self._run("shell", "input", "swipe", "540", "1750", "540", "400", "350")

    def press_back(self) -> None:
        self._run("shell", "input", "keyevent", "4")

    def open_instagram_url(self, url: str) -> None:
        self._run("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)

    def read_clipboard(self) -> str:
        """Return Android's current clipboard text after Instagram Copy link.

        This is intentionally read only.  The Copy link tap is initiated in
        the visible Instagram share sheet; the driver never injects or
        replaces clipboard contents itself.
        """
        return str(getattr(self._run("shell", "cmd", "clipboard", "get"), "stdout", "")).strip()

    def open_reel_url(self, url: str) -> None:
        """Backward-compatible name for callers that only open Reel detail URLs."""
        self.open_instagram_url(url)

    def capture_screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run("exec-out", "screencap", "-p", binary=True)
        output = getattr(result, "stdout", b"")
        if isinstance(output, str):
            output = output.encode("latin-1", errors="replace")
        path.write_bytes(output)
