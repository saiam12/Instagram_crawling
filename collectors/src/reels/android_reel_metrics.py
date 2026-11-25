"""Android-app enrichment for the browser collector's Reel records.

The browser owns identity and static Reel fields.  This module opens that
already-collected URL in the logged-in Instagram Android app and reads only
the app-visible metrics that are unreliable or absent in browser responses.
No account-changing action is issued: all ADB commands are navigation,
UIAutomator inspection, or taps on visible read-only controls.
"""

from __future__ import annotations

from .reel_records import caption_without_hashtags, exact_nonnegative_integer, normalize_search_grid_reel_url

import csv
import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Callable, Protocol, Sequence
from xml.etree import ElementTree
from urllib.parse import urlparse

from . import collection_pause
from .android_metric_queue import ANDROID_PROFILE_VIEW_FALLBACK_ENABLED
from .collection_diagnostics import CollectorDiagnostics


INSTAGRAM_PACKAGE = "com.instagram.android"
ANDROID_COLLECTION_FIELDS = ("like_count", "share_count", "repost_count", "saved_count")
ANDROID_OWNED_FIELDS = ANDROID_COLLECTION_FIELDS
WEB_PREFERRED_COUNT_FIELDS = ("like_count", "comment_count")
_RATE_LIMIT_SIGNAL = re.compile(
    r"429|too\s+many\s+requests|rate[_\s-]?limit|throttled|please\s+wait\s+a\s+few\s+minutes|"
    r"try\s+again\s+later|요청을\s*처리할\s*수\s*없습니다|잠시\s*후\s*다시",
    re.I,
)
_COMPACT_COUNT = re.compile(r"(?P<number>\d+(?:[.,]\d+)?)\s*(?P<unit>[KMBkmb만천])")
_EXACT_COUNT = re.compile(r"\d[\d,\s]*")
_POST_COUNT_PATTERNS = (
    re.compile(r"(?P<count>\d[\d,\s]*)\+\s+posts?\b", re.I),
    re.compile(r"fewer\s+than\s+(?P<count>\d[\d,\s]*)\s+posts?\b", re.I),
    re.compile(r"(?P<count>\d+(?:[,.]\d+)?\s*(?:[KMBkmb만천])?|\d[\d,\s]*)\s+posts?\b", re.I),
    re.compile(r"(?P<count>\d+(?:[,.]\d+)?\s*(?:[KMBkmb만천])?|\d[\d,\s]*)\s*개?\s*게시물"),
    re.compile(r"게시물\s*(?P<count>\d+(?:[,.]\d+)?\s*(?:[KMBkmb만천])?|\d[\d,\s]*)"),
)
_METRIC_RESOURCE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("like_count", ("like_count", "likes_count")),
    ("view_count", ("video_view_count", "view_count", "play_count")),
    ("comment_count", ("comment_count", "comments_count")),
    ("share_count", ("share_count", "shares_count", "share_number", "send_count", "share_button", "send_button")),
    ("repost_count", ("repost_count", "reposts_count", "reshare_count", "reshare_number")),
    ("saved_count", ("save_count", "saved_count", "saves_count", "bookmark_count", "save_button", "bookmark_button")),
)
_LABEL_COUNT = r"\d[\d,]*(?:\.\d+)?\s*[kmb만천]?"
_METRIC_TEXT_PATTERNS = (
    ("like_count", rf"(?:like number is {_LABEL_COUNT}(?:\. view likes)?|{_LABEL_COUNT} likes|좋아요\s*{_LABEL_COUNT}개?)"),
    ("view_count", rf"(?:view count(?: is)? {_LABEL_COUNT}|{_LABEL_COUNT} views|조회수\s*{_LABEL_COUNT}회?)"),
    ("comment_count", rf"(?:comment number is {_LABEL_COUNT}(?:\. view comments)?|{_LABEL_COUNT} comments|댓글\s*{_LABEL_COUNT}개?)"),
    ("share_count", rf"(?:(?:re)?share number is {_LABEL_COUNT}|share count {_LABEL_COUNT}|{_LABEL_COUNT} shares|공유 수\s*{_LABEL_COUNT})"),
    ("repost_count", rf"(?:repost number is {_LABEL_COUNT}|reposted {_LABEL_COUNT} times|{_LABEL_COUNT} reposts?|리포스트\s*{_LABEL_COUNT})"),
    ("saved_count", rf"(?:save number is {_LABEL_COUNT}|{_LABEL_COUNT} saves|저장\s*{_LABEL_COUNT}개?)"),
)
_COUNTLESS_ICON_MARKERS = {
    "like_count": ("like_button", "like_icon"),
    "comment_count": ("comment_button", "comments_button", "comment_count", "comments_count"),
    "repost_count": ("repost_button", "repost_count", "reposts_count"),
    "share_count": ("share_button", "send_button", "share_count", "shares_count"),
    "saved_count": ("save_button", "bookmark_button", "save_count", "saved_count"),
}
_COUNTLESS_ICON_LABELS = {
    "like_count": {"like", "좋아요"},
    "comment_count": {"comment", "comments", "댓글"},
    "repost_count": {"repost", "리포스트"},
    "share_count": {"share", "send", "reshare", "공유", "보내기"},
    "saved_count": {"save", "bookmark", "저장"},
}
_ANDROID_REEL_READY_TIMEOUT_SECONDS = 12.0
_ANDROID_RETRY_READY_TIMEOUT_SECONDS = 12.0
_ANDROID_TAG_SEARCH_SCROLL_ATTEMPTS = 120
_ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS = 3
_ANDROID_TAG_SEARCH_READY_TIMEOUT_SECONDS = 15.0
_ANDROID_TAG_SEARCH_INITIAL_WAIT_SECONDS = 0.2
_ANDROID_TAG_SEARCH_PAGE_CHANGE_TIMEOUT_SECONDS = 0.75
_ANDROID_TAG_SEARCH_UI_DELAY_SECONDS = 0.2
_MIN_HASHTAG_POST_COUNT = 1_000
_ADB_COMMAND_TIMEOUT_SECONDS = 15.0
_ADB_UI_DUMP_TIMEOUT_SECONDS = 15.0
_WINDOWS_CLIPBOARD_SYNC_SECONDS = 0.8
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


class AndroidMetricsError(RuntimeError):
    """The app is unavailable or did not render a usable public surface."""


class InstagramAnrRecovered(AndroidMetricsError):
    """Instagram was relaunched after its system dialog was dismissed."""


@dataclass(frozen=True)
class UiNode:
    text: str
    content_desc: str
    resource_id: str
    bounds: str
    class_name: str = ""
    selected: bool = False

    @property
    def visible_text(self) -> str:
        if self.text == self.content_desc:
            return self.text
        return " ".join(part for part in (self.text, self.content_desc) if part).strip()


@dataclass(frozen=True)
class AndroidMetricResult:
    metrics: dict[str, int] = field(default_factory=dict)
    compact_fields: tuple[str, ...] = ()
    recollection: bool = False
    audio_name: str = ""
    like_count_private: bool | None = None
    comment_count_disabled: bool | None = None
    status: str = "collected"
    error: str = ""
    observed_reel_url: str = ""
    identity_like_fallback: bool = False
    observed_username: str = ""
    observed_caption: str = ""
    observed_hashtags: str | None = None
    identity_author_only: bool = False
    caption_modified: bool = False

    def display_value(self, field_name: str) -> str:
        if field_name == "audio_name":
            return self.audio_name or "unavailable"
        value = self.metrics.get(field_name)
        return f"{value:,}" if value is not None else "unavailable"


def _reel_author_matches(expected: object, observed: object) -> bool:
    expected = str(expected or "").strip().lstrip('@').casefold()
    # Collaboration headers expose several usernames in one author node.
    authors = re.split(r"\s+and\s+|\s*&\s*|,\s*", str(observed or "").strip().casefold())
    return bool(authors[0]) and (not expected or expected in [author.lstrip('@') for author in authors])


def _likes_match(expected, observed):
    if isinstance(expected, str) and re.fullmatch(r"[0-9]+", expected.strip()):
        expected = int(expected)
    left = exact_nonnegative_integer(expected)
    right = exact_nonnegative_integer(observed)
    return left is not None and right is not None and abs(left - right) <= 10


def reel_caption_key(value: object) -> str:
    # Instagram adds a trailing ellipsis to its collapsed caption preview.
    return " ".join(str(value or "").split()).removesuffix(" …").strip()


def _caption_preview_prefix(value: object) -> str:
    preview = re.sub(r"(?:#\s*)?(?:…|\.\.\.)$", "", str(value or "").strip())
    return caption_without_hashtags(preview)


def _hashtag_identity(value: object) -> str:
    normalized = unicodedata.normalize("NFC", str(value or "")).casefold()
    return " ".join(sorted(set(re.findall(r"#[\w]+", normalized))))


def _hashtag_preview_matches(preview: object, expected_hashtags: object) -> bool:
    text = unicodedata.normalize("NFC", str(preview or "")).strip().casefold()
    visible = re.sub(r"(?:…|\.{2,})$", "", text).strip()
    if not re.fullmatch(r"(?:#[\w]+\s*)+", visible):
        return False
    tags = re.findall(r"#[\w]+", visible)
    expected = set(_hashtag_identity(expected_hashtags).split())
    # The collapsed preview can cut its last hashtag without exposing an ellipsis.
    return all(tag in expected for tag in tags[:-1]) and any(
        tag.startswith(tags[-1]) for tag in expected
    )


def _reel_caption_nodes(xml: str) -> list[ElementTree.Element]:
    component = ElementTree.fromstring(xml).find(
        './/*[@resource-id="com.instagram.android:id/clips_caption_component"]'
    )
    return [node for node in component.iter()
            if (node.get("content-desc") or node.get("text") or "").strip()] if component is not None else []


def android_result_matches_reel(result: dict[str, object], row: dict[str, object]) -> bool:
    observed_url = str(result.get("observed_reel_url") or "")
    host = urlparse(observed_url).hostname or ""
    observed = normalize_search_grid_reel_url(observed_url)
    target = normalize_search_grid_reel_url(row.get("url"))
    expected = str(row.get("username") or "").strip().lstrip('@').casefold()
    actual = str(result.get("observed_username") or "").strip().lstrip('@').casefold()
    caption = _caption_preview_prefix(result.get("observed_caption"))
    if result.get("identity_author_only") is True and not observed_url:
        return bool(target and expected and _reel_author_matches(expected, actual))
    if result.get("observed_hashtags") is not None:
        return bool(target and expected and _reel_author_matches(expected, actual)
                    and _hashtag_preview_matches(result["observed_hashtags"], row.get("hashtags")))
    if not observed_url and caption and target and expected and _reel_author_matches(expected, actual):
        return reel_caption_key(row.get("title")).startswith(caption)
    if result.get("identity_like_fallback") is True and not observed_url and target and expected and _reel_author_matches(expected, actual):
        metrics = result.get("metrics") or {}
        reference = result.get("python_metrics") or {}
        if isinstance(metrics, dict) and isinstance(reference, dict) and "like_count" not in (result.get("compact_fields") or ()):
            return _likes_match(reference.get("like_count"), metrics.get("like_count"))
    return bool((host == "instagram.com" or host.endswith(".instagram.com"))
                and observed and target and observed["shortcode"] == target["shortcode"]
                and _reel_author_matches(expected, actual))


class AndroidUiDriver(Protocol):
    def ensure_ready(self) -> None: ...

    def open_instagram_url(self, url: str) -> None: ...

    def open_instagram_search(self, query: str) -> None: ...

    def dump_ui(self) -> str: ...

    def tap_bounds(self, bounds: str) -> bool: ...

    def tap_screen_center(self) -> None: ...

    def press_back(self) -> None: ...

    def scroll_down(self) -> None: ...


def default_adb_path() -> Path:
    configured = os.environ.get("INSTAGRAM_ANDROID_ADB_PATH", "").strip()
    if configured:
        return Path(os.path.expandvars(configured.strip('"'))).expanduser()
    executable = "adb.exe" if os.name == "nt" else "adb"
    sdk_roots = [os.environ.get(name, "") for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT")]
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        sdk_roots.append(str(Path(local_app_data) / "Android" / "Sdk"))
    for root in sdk_roots:
        candidate = Path(os.path.expandvars(root.strip('"'))).expanduser() / "platform-tools" / executable
        if root and candidate.is_file():
            return candidate
    return Path(shutil.which(executable) or (Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / executable))


def _windows_clipboard_libraries() -> tuple[object, object]:
    if os.name != "nt":
        raise OSError("The Android emulator clipboard fallback is only available on Windows.")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    return user32, kernel32


def _open_windows_clipboard(user32: object) -> None:
    for _ in range(10):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise OSError("Windows clipboard is busy.")


def _read_windows_clipboard_text() -> str | None:
    """Snapshot plain clipboard text so Android search does not consume stale text."""
    user32, kernel32 = _windows_clipboard_libraries()
    if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
        return None
    _open_windows_clipboard(user32)
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_windows_clipboard_text(value: str) -> None:
    """Put Unicode text on the host clipboard used by the Android emulator."""
    user32, kernel32 = _windows_clipboard_libraries()
    encoded = (value + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(encoded))
    if not handle:
        raise OSError("Could not allocate Windows clipboard memory.")
    transferred = False
    try:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError("Could not lock Windows clipboard memory.")
        try:
            ctypes.memmove(pointer, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(handle)
        _open_windows_clipboard(user32)
        try:
            if not user32.EmptyClipboard():
                raise OSError("Could not clear the Windows clipboard.")
            if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
                raise OSError("Could not set Unicode Windows clipboard text.")
            transferred = True
        finally:
            user32.CloseClipboard()
    finally:
        if not transferred:
            kernel32.GlobalFree(handle)


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    return str(result.stderr or result.stdout or "ADB command failed.").strip()


class AdbAndroidUiDriver:
    """Minimal ADB driver owned by the hybrid collector folder."""

    def __init__(
        self,
        adb_path: Path,
        device_id: str | None = None,
        diagnostics: CollectorDiagnostics | None = None,
    ) -> None:
        self.adb_path = Path(adb_path)
        self.diagnostics = diagnostics
        self.device_id = (
            device_id
            or os.environ.get("INSTAGRAM_ANDROID_DEVICE_ID", "").strip()
            or self._select_online_device()
        )
        self._cached_screen_size: tuple[int, int] | None = None

    def _run(
        self,
        *arguments: str,
        timeout_seconds: float = _ADB_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        collection_pause.wait_sync()
        command = [str(self.adb_path), "-s", self.device_id, *arguments]
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            rendered = " ".join(arguments[:3])
            if self.diagnostics is not None:
                self.diagnostics.adb_command(
                    arguments,
                    duration=time.monotonic() - started,
                    status="timeout",
                    error=f"ADB command timed out after {timeout_seconds:g}s: {rendered}",
                )
            raise AndroidMetricsError(
                f"ADB command timed out after {timeout_seconds:g}s: {rendered}"
            ) from error
        if result.returncode:
            if self.diagnostics is not None:
                self.diagnostics.adb_command(
                    arguments,
                    duration=time.monotonic() - started,
                    status="failed",
                    error=_command_error(result),
                )
            raise AndroidMetricsError(_command_error(result))
        if self.diagnostics is not None:
            self.diagnostics.adb_command(
                arguments,
                duration=time.monotonic() - started,
                status="success",
            )
        return result

    def _select_online_device(self) -> str:
        if not self.adb_path.is_file():
            raise AndroidMetricsError(
                f"ADB executable was not found: {self.adb_path}. Start Android Studio and pass --android-adb-path if needed."
            )
        try:
            result = subprocess.run(
                [str(self.adb_path), "devices", "-l"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_ADB_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise AndroidMetricsError(
                f"ADB device discovery timed out after {_ADB_COMMAND_TIMEOUT_SECONDS:g}s."
            ) from error
        if result.returncode:
            raise AndroidMetricsError(_command_error(result))
        devices = [
            columns[0]
            for line in result.stdout.splitlines()
            if len(columns := line.split()) >= 2 and columns[1] == "device"
        ]
        if not devices:
            raise AndroidMetricsError("No online Android emulator was found. Start the Android Studio emulator first.")
        if len(devices) > 1:
            raise AndroidMetricsError("More than one Android device is online; pass --android-device-id.")
        return devices[0]

    def ensure_ready(self) -> None:
        boot_completed = self._run("shell", "getprop", "sys.boot_completed")
        if boot_completed.stdout.strip() != "1":
            raise AndroidMetricsError(
                "The Android emulator is online but has not finished booting."
            )
        package = self._run("shell", "pm", "path", INSTAGRAM_PACKAGE)
        if not package.stdout.strip():
            raise AndroidMetricsError("Instagram is not installed on the selected Android device.")

    def open_instagram_url(self, url: str) -> None:
        # Without the package constraint an emulator can send the https URL
        # to Chrome/the system resolver.  A successful ``am start`` then
        # looks like an Instagram failure because UIAutomator never sees the
        # Reel.  Keep the normal VIEW intent but force Instagram to handle it.
        # NEW_TASK | CLEAR_TASK destroys previous Reel activities in this task
        # before opening the next link. It does not clear app data or login.
        arguments = (
            "shell", "am", "start", "-W", "-p", INSTAGRAM_PACKAGE,
            "-f", "0x10008000",
            "-a", "android.intent.action.VIEW", "-d", url,
        )
        try:
            self._run(*arguments)
        except AndroidMetricsError:
            # Reopen through the launcher entry point, like tapping the app
            # icon, before retrying the target URL once.
            self.launch_instagram()
            time.sleep(1.0)
            self._run(*arguments)

    def launch_instagram(self) -> None:
        """Open the installed app without relying on home-screen coordinates."""
        error = "Instagram launcher activity is not ready."
        for attempt in range(3):
            try:
                result = self._run(
                    "shell", "cmd", "package", "resolve-activity", "--brief",
                    "-a", "android.intent.action.MAIN",
                    "-c", "android.intent.category.LAUNCHER", "-p", INSTAGRAM_PACKAGE,
                )
                component = next((line.strip() for line in result.stdout.splitlines()
                                  if re.fullmatch(r"com\.instagram\.android/[\w.$]+", line.strip())), "")
                if not component:
                    raise AndroidMetricsError("Instagram launcher activity is not ready.")
                self._run(
                    "shell", "am", "start", "-W", "-n", component,
                    "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
                )
                return
            except AndroidMetricsError as exception:
                error = str(exception)
                if attempt < 2:
                    time.sleep(1.0)
        raise AndroidMetricsError(f"Instagram app launch failed: {error}")

    def dump_ui(self, *, timeout_seconds: float | None = None) -> str:
        command_timeout = getattr(self, "_ui_dump_timeout_seconds", _ADB_UI_DUMP_TIMEOUT_SECONDS)
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        # A bounded surface wait owns retries; do not nest two full ADB waits.
        attempts = 1 if deadline is not None else 2
        remote = f"/sdcard/view_window_{uuid.uuid4().hex}.xml"
        command = f"uiautomator dump --compressed {remote} 1>&2 && cat {remote}; result=$?; rm -f {remote}; exit $result"
        for attempt in range(attempts):
            xml = ""
            stderr = ""
            try:
                result = self._run(
                    "exec-out",
                    "sh",
                    "-c",
                    command,
                    timeout_seconds=min(command_timeout, max(0.1, deadline - time.monotonic())) if deadline is not None else command_timeout,
                )
                output, stderr = result.stdout or "", result.stderr or ""
                # adb exec-out merges remote stderr into stdout. Keep the
                # dump diagnostic, but parse only the hierarchy that follows.
                xml_start = output.find("<?xml")
                if xml_start < 0:
                    xml_start = output.find("<hierarchy")
                diagnostic = output[:xml_start] if xml_start >= 0 else output
                xml = output[xml_start:].strip() if xml_start >= 0 else output
                detail = (stderr or diagnostic).strip()[:500]
                try:
                    root = ElementTree.fromstring(xml)
                except ElementTree.ParseError as error:
                    raise AndroidMetricsError(f"UIAutomator returned an empty or unreadable screen. output_bytes={len(xml.encode('utf-8'))}; {detail or str(error)}") from error
                if root.tag != "hierarchy" or root.find(".//node") is None:
                    raise AndroidMetricsError(f"UIAutomator returned an empty or unreadable screen. root={root.tag}; {detail}")
                return xml
            except AndroidMetricsError as error:
                read_failure = any(message in str(error).casefold() for message in (
                    "adb command timed out", "empty or unreadable", "could not get idle state", "null root node",
                ))
                if read_failure and self.diagnostics is not None:
                    self.diagnostics.emit("UI_DUMP_FAILED", attempt=attempt + 1,
                                          output_bytes=len(xml.encode("utf-8")), error=str(error)[:700])
                reset_ok = self.reset_ui_automation()
                if read_failure and reset_ok is False:
                    raise AndroidMetricsError(f"UIAutomator cleanup failed after UI read failure: {error}") from error
                if attempt == attempts - 1 or not read_failure:
                    if read_failure and "uiautomator" not in str(error).casefold() and "adb command timed out" not in str(error).casefold():
                        raise AndroidMetricsError(f"UIAutomator returned an empty or unreadable screen. {error}") from error
                    raise
        raise AssertionError("Unreachable UI dump retry state")

    def read_screen_ocr(self) -> list[dict]:
        if os.name != "nt":
            return []
        script = Path(__file__).resolve().parents[2] / "scripts" / "android" / "read-screen-ocr.ps1"
        try:
            png = subprocess.run(
                [str(self.adb_path), "-s", self.device_id, "exec-out", "screencap", "-p"],
                capture_output=True, check=True, timeout=4,
            ).stdout
            with tempfile.TemporaryDirectory() as directory:
                image = Path(directory) / "screen.png"
                image.write_bytes(png)
                output = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script), str(image)],
                    capture_output=True, check=True, timeout=8, encoding="utf-8-sig",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                ).stdout
            return json.loads(output)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            if self.diagnostics is not None:
                self.diagnostics.ui_event("METRIC_OCR_FAILED", reason=type(error).__name__)
            return []

    def reset_ui_automation(self) -> bool:
        """Remove a device-side UIAutomator left behind by a host timeout."""
        try:
            result = subprocess.run(
                [str(self.adb_path), "-s", self.device_id, "shell", "pkill", "uiautomator"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=5,
            )
            success = result.returncode == 0 or (result.returncode == 1 and not result.stderr.strip())
            error = "" if success else _command_error(result)
        except (OSError, subprocess.TimeoutExpired) as exception:
            success, error = False, str(exception)
        if self.diagnostics is not None:
            self.diagnostics.emit("UI_AUTOMATOR_RESET", success=success, error=error)
        return success

    def focused_instagram_anr(self) -> str:
        result = self._run("shell", "dumpsys", "window", timeout_seconds=5)
        match = re.search(
            r"^\s*mCurrentFocus=Window\{(\S+) [^\r\n]*?Application Not Responding:\s*"
            r"com\.instagram\.android\s*\}", result.stdout, re.M | re.I,
        )
        return match[1] if match else ""

    def restart_unresponsive_instagram(self) -> None:
        self._run("shell", "am", "force-stop", INSTAGRAM_PACKAGE, timeout_seconds=5)
        self.launch_instagram()

    def restart_instagram(self, reel_url: str) -> None:
        # Bring Instagram's card to the center of Overview before dismissing it.
        self.launch_instagram()
        time.sleep(1.0)
        self._run("shell", "input", "keyevent", "187")
        time.sleep(1.0)
        width, height = self._screen_size()
        self._run(
            "shell", "input", "swipe", str(width // 2), str(int(height * 0.55)),
            str(width // 2), str(int(height * 0.05)), "300",
        )
        time.sleep(1.0)
        # Removing a recent task alone does not guarantee process termination.
        self._run("shell", "am", "force-stop", INSTAGRAM_PACKAGE)
        self._run("shell", "input", "keyevent", "3")
        self.launch_instagram()
        time.sleep(1.0)
        self.open_instagram_url(reel_url)
        self._ui_dump_timeout_seconds = 15.0

    def wait_for_boot(self, timeout_seconds: float = 120.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        error = "Android did not finish booting"
        while time.monotonic() < deadline:
            try:
                result = self._run("shell", "getprop", "sys.boot_completed", timeout_seconds=min(5.0, max(0.1, deadline - time.monotonic())))
                if result.stdout.strip() == "1":
                    return
            except AndroidMetricsError as exception:
                error = str(exception)
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
        raise AndroidMetricsError(f"Android boot recovery timed out after {timeout_seconds:g}s: {error}")

    def is_dynamic_cta_reel(self) -> bool:
        """Detect View shop and similar CTA Reels without UIAutomator."""
        try:
            hierarchy = self._run("shell", "dumpsys", "activity", "top").stdout
        except AndroidMetricsError:
            return False
        return any(
            "clips_viewer_cta_button" in line and re.search(r"\{[0-9a-f]+\s+V", line, re.I)
            for line in hierarchy.splitlines()
        )

    def reset_instagram_surface(self) -> None:
        """Clear a poisoned accessibility session without stopping Instagram."""
        self.reset_ui_automation()

    def tap_bounds(self, bounds: str) -> bool:
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if match is None:
            return False
        left, top, right, bottom = (int(value) for value in match.groups())
        self._run("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
        return True

    def tap_screen_center(self) -> None:
        width, height = self._screen_size()
        self._run("shell", "input", "tap", str(width // 2), str(height // 2))

    def press_back(self) -> None:
        self._run("shell", "input", "keyevent", "4")

    def _screen_size(self) -> tuple[int, int]:
        if self._cached_screen_size is not None:
            return self._cached_screen_size
        output = self._run("shell", "wm", "size").stdout
        match = re.search(r"(\d+)x(\d+)", output)
        self._cached_screen_size = (
            (int(match.group(1)), int(match.group(2))) if match else (1080, 1920)
        )
        return self._cached_screen_size

    def scroll_down(self) -> None:
        width, height = self._screen_size()
        x = width // 2
        self._run(
            "shell", "input", "swipe",
            str(x), str(int(height * 0.82)), str(x), str(int(height * 0.30)), "180",
        )

    def scroll_profile_grid(self, xml: str) -> None:
        """Nudge toward the top, then scroll down inside the current grid."""
        nodes = parse_ui_xml(xml)
        if _is_reel_detail_surface(xml):
            raise AndroidMetricsError("Refused profile scroll on Reel detail")
        grid = next((n for n in nodes if "clips_grid_recyclerview" in n.resource_id), None)
        if grid is None:
            raise AndroidMetricsError("Profile Reels grid bounds unavailable")
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", grid.bounds)
        if match is None:
            raise AndroidMetricsError("Invalid profile grid bounds")
        left, top, right, bottom = map(int, match.groups())
        if right <= left or bottom - top < 120:
            raise AndroidMetricsError("Profile grid is too small to scroll safely")
        x = (left + right) // 2
        height = bottom - top
        def swipe(start: float, end: float, duration: int) -> None:
            self._run("shell", "input", "swipe", str(x), str(top + int(height * start)),
                      str(x), str(top + int(height * end)), str(duration))
        # Finger down slightly = content toward earlier rows; finger up = later rows.
        swipe(0.40, 0.52, 120)
        swipe(0.88, 0.15, 300)

    def open_instagram_search(self, query: str) -> None:
        """Open Instagram's in-app search and paste a Unicode hashtag query."""
        self.open_instagram_url("https://www.instagram.com/explore/")
        xml = self.dump_ui()
        input_node = _first_matching_node(parse_ui_xml(xml), _is_search_input)
        if input_node is None:
            trigger = _first_matching_node(parse_ui_xml(xml), _is_search_trigger)
            if trigger is None or not self.tap_bounds(trigger.bounds):
                raise AndroidMetricsError("Instagram search control was not visible.")
            xml = self.dump_ui()
            input_node = _first_matching_node(parse_ui_xml(xml), _is_search_input)
        if input_node is None or not self.tap_bounds(input_node.bounds):
            raise AndroidMetricsError("Instagram search text field was not visible.")
        clipboard_result = self._run("shell", "cmd", "clipboard", "set", query)
        clipboard_output = f"{clipboard_result.stdout}\n{clipboard_result.stderr}".casefold()
        use_windows_clipboard = "no shell command implementation" in clipboard_output
        previous_clipboard: str | None = None
        if use_windows_clipboard:
            # Android 15 reports exit code 0 for this unsupported command. In
            # that case KEYCODE_PASTE would paste stale host text (often the
            # collector command itself), so explicitly stage the Unicode tag
            # on the Windows clipboard shared by the emulator.
            previous_clipboard = _read_windows_clipboard_text()
            try:
                _set_windows_clipboard_text(query)
                time.sleep(_WINDOWS_CLIPBOARD_SYNC_SECONDS)
            except OSError as error:
                raise AndroidMetricsError(f"Could not stage the Android search text: {error}") from error
        actual_query = ""
        try:
            for attempt in range(3):
                # Instagram occasionally keeps the previous result selected
                # even though the EditText is visible. Re-focus and clear on
                # every retry so a stale tag cannot be collected as this one.
                if attempt and not self.tap_bounds(input_node.bounds):
                    raise AndroidMetricsError("Instagram search text field was not visible for retry.")
                self._run("shell", "input", "keycombination", "113", "29")  # Ctrl+A
                self._run("shell", "input", "keyevent", "67")  # Delete
                self._run("shell", "input", "keyevent", "279")  # Paste
                time.sleep(0.2)
                verified_xml = self.dump_ui()
                verified_input = _first_matching_node(parse_ui_xml(verified_xml), _is_search_input)
                actual_query = verified_input.text.strip() if verified_input is not None else ""
                if actual_query == query:
                    break
                if attempt == 1 and not use_windows_clipboard:
                    # Some Android 15 builds acknowledge ``cmd clipboard``
                    # without replacing the app's paste buffer. Only after
                    # two failed UI attempts, switch to the emulator's shared
                    # Windows clipboard as a recovery path.
                    try:
                        previous_clipboard = _read_windows_clipboard_text()
                        _set_windows_clipboard_text(query)
                        time.sleep(_WINDOWS_CLIPBOARD_SYNC_SECONDS)
                        use_windows_clipboard = True
                    except OSError as error:
                        raise AndroidMetricsError(f"Could not stage the Android search text: {error}") from error
            else:
                raise AndroidMetricsError(
                    f"Instagram search input mismatch: expected {query!r}, observed {actual_query!r}."
                )
        finally:
            # Restore only after the last verification dump. Restoring early
            # races KEYCODE_PASTE and reintroduces the stale command text.
            if use_windows_clipboard and previous_clipboard is not None:
                try:
                    _set_windows_clipboard_text(previous_clipboard)
                except OSError:
                    pass
        # Instagram does not consistently start a search after a clipboard
        # paste. Confirm the query is present first, then explicitly submit it
        # before the caller selects the Tags result tab.
        self._run("shell", "input", "keyevent", "66")  # Enter
        time.sleep(_ANDROID_TAG_SEARCH_INITIAL_WAIT_SECONDS)


def parse_ui_xml(xml: str) -> list[UiNode]:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []
    return [
        UiNode(
            text=element.attrib.get("text", "").strip(),
            content_desc=element.attrib.get("content-desc", "").strip(),
            resource_id=element.attrib.get("resource-id", "").strip(),
            bounds=element.attrib.get("bounds", "").strip(),
            class_name=element.attrib.get("class", "").strip(),
            selected=element.attrib.get("selected") == "true",
        )
        for element in root.iter("node")
    ]


def _is_search_trigger(node: UiNode) -> bool:
    visible = node.visible_text.casefold()
    resource_id = node.resource_id.casefold()
    return (
        "search" in resource_id
        or "search" in visible
        or "검색" in node.visible_text
    ) and "edittext" not in node.class_name.casefold()


def _is_search_input(node: UiNode) -> bool:
    visible = node.visible_text.casefold()
    resource_id = node.resource_id.casefold()
    return (
        "edittext" in node.class_name.casefold()
        or "search_edit" in resource_id
        or "search" in resource_id
        or "search" in visible
        or "검색" in node.visible_text
    ) and bool(node.bounds)


def _is_tags_tab(node: UiNode) -> bool:
    text = node.visible_text.strip().casefold()
    resource_id = node.resource_id.casefold()
    return (
        text in {"tags", "태그"}
        or text.startswith("tags,")
        or text.startswith("태그,")
        or ("tag" in resource_id and "tab" in resource_id)
    )


def _bounds_area(bounds: str) -> int:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if match is None:
        return 0
    left, top, right, bottom = (int(value) for value in match.groups())
    return max(0, right - left) * max(0, bottom - top)


def _fallback_tags_tab_bounds(nodes: Sequence[UiNode]) -> str:
    """Estimate the fourth search tab when Instagram omits its accessible label."""
    search_input = _first_matching_node(nodes, _is_search_input)
    if search_input is None:
        return ""
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", search_input.bounds)
    if match is None:
        return ""
    left, top, right, bottom = (int(value) for value in match.groups())
    max_right = max(
        (
            int(candidate_match.group(3))
            for node in nodes
            if (candidate_match := re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.bounds)) is not None
        ),
        default=0,
    )
    screen_right = max_right if max_right > right else right + max(1, (right - left) // 4)
    tab_bottom = bottom + max(80, int((bottom - top) * 1.2))
    return f"[{screen_right * 3 // 4},{bottom}][{screen_right},{tab_bottom}]"


def _largest_tags_tab(nodes: Sequence[UiNode]) -> UiNode | None:
    candidates = [node for node in nodes if _is_tags_tab(node) and node.bounds]
    return max(candidates, key=lambda node: _bounds_area(node.bounds), default=None)


def _clickable_tags_tab_bounds(xml: str) -> str:
    """Return the clickable tab container that contains the Tags label."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return ""
    candidates: list[str] = []
    for element in root.iter("node"):
        if element.attrib.get("clickable", "").casefold() != "true":
            continue
        bounds = element.attrib.get("bounds", "").strip()
        if not bounds:
            continue
        descendants = (
            UiNode(
                text=node.attrib.get("text", "").strip(),
                content_desc=node.attrib.get("content-desc", "").strip(),
                resource_id=node.attrib.get("resource-id", "").strip(),
                bounds=node.attrib.get("bounds", "").strip(),
                class_name=node.attrib.get("class", "").strip(),
            )
            for node in element.iter("node")
        )
        if any(_is_tags_tab(node) for node in descendants):
            candidates.append(bounds)
    return min(candidates, key=_bounds_area, default="")


def extract_related_hashtag_post_counts(
    xml: str,
    query_hashtag: str,
    *,
    collected_at: str | None = None,
) -> list[dict[str, object]]:
    """Pair each Instagram search-result tag label with its nearby post total."""
    timestamp = collected_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    query = str(query_hashtag).strip().lstrip("#")
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    nodes = parse_ui_xml(xml)
    for index, node in enumerate(nodes):
        raw_tag = node.text.strip()
        hashtag = raw_tag.lstrip("#")
        if not raw_tag.startswith("#") or not hashtag or any(character.isspace() for character in hashtag):
            continue
        if hashtag.casefold() in seen:
            continue
        post_count: int | None = None
        media_count = ""
        raw_post_count = ""
        for successor in nodes[index + 1:index + 10]:
            if successor.text.strip().startswith("#"):
                break
            for pattern in _POST_COUNT_PATTERNS:
                match = pattern.search(successor.visible_text.strip())
                if match is None:
                    continue
                post_count = parse_display_count(match.group("count"))
                media_count = match.group("count").strip()
                raw_post_count = successor.visible_text.strip()
                break
            if post_count is not None:
                break
        if post_count is None:
            continue
        seen.add(hashtag.casefold())
        rows.append({
            "collected_at": timestamp,
            "query_hashtag": query,
            "hashtag": hashtag,
            "media_count": media_count,
            "post_count": post_count,
            "raw_post_count": raw_post_count,
            "source": "android_search_tags",
            "status": "collected",
            "error": "",
        })
    return rows


def parse_display_count(value: object) -> int | None:
    text = str("" if value is None else value).strip()
    compact = _COMPACT_COUNT.fullmatch(text)
    if compact:
        raw = compact.group("number").replace(" ", "")
        if raw.count(",") == 1 and "." not in raw and len(raw.rsplit(",", 1)[1]) <= 2:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
        multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "만": 10_000, "천": 1_000}[compact.group("unit").casefold()]
        try:
            parsed = Decimal(raw) * multiplier
        except InvalidOperation:
            return None
        return int(parsed) if parsed >= 0 else None
    normalized = re.sub(r"[\s,]", "", text)
    return int(normalized) if normalized.isdecimal() else None


def _node_count(node: UiNode) -> int | None:
    direct = parse_display_count(node.text)
    if direct is not None:
        return direct
    for match in re.finditer(r"(?<![\d,])\d+(?:[,\s]\d+)*(?:\.\d+)?\s*(?:[KMBkmb만천])?", node.visible_text):
        value = parse_display_count(match.group(0).strip())
        if value is not None:
            return value
    return None


def _metric_key(node: UiNode) -> str:
    resource_id = node.resource_id.casefold()
    for name, markers in _METRIC_RESOURCE_MARKERS:
        if any(marker in resource_id for marker in markers):
            return name
    if "caption" in resource_id:
        return ""
    # Captions also contain prices, 'save', 'comments', etc. Only complete
    # accessibility count labels may identify a metric without a known ID.
    for name, pattern in _METRIC_TEXT_PATTERNS:
        if any(re.fullmatch(pattern, label.strip(), re.I) for label in (node.text, node.content_desc)):
            return name
    return ""


def _like_count_is_private(xml: str) -> bool:
    for node in parse_ui_xml(xml):
        label = node.visible_text.casefold()
        if re.search(r"like count.{0,30}(?:hidden|private)|(?:hidden|private).{0,30}like count|좋아요\s*(?:수|개수).{0,20}(?:숨|비공개)", label):
            return True
        if any(marker in node.resource_id.casefold() for marker in ("like_count", "likes_count")):
            if node.text.strip().casefold() in {"likes", "like", "view likes", "좋아요"}:
                return True
    return False


def extract_visible_metrics(xml: str) -> dict[str, int]:
    metrics: dict[str, int] = {}
    for node in parse_ui_xml(xml):
        name = _metric_key(node)
        value = _node_count(node) if name else None
        if name and value is not None:
            metrics[name] = value
    if _like_count_is_private(xml):
        metrics.pop("like_count", None)
    return metrics


def extract_recollection_metrics(xml: str) -> tuple[dict[str, int], tuple[str, ...]]:
    """Prefer full content descriptions and identify display-only compact counts."""
    metrics: dict[str, int] = {}
    compact: set[str] = set()
    fields = {*ANDROID_COLLECTION_FIELDS, "comment_count"}
    for node in parse_ui_xml(xml):
        name = _metric_key(node)
        if name not in fields:
            continue
        description = next((match.group(0).strip() for match in re.finditer(
            r"(?<![\d,])\d+(?:[,\s]\d+)*(?:\.\d+)?\s*(?:[KMBkmb만천])?", node.content_desc
        )), "")
        if description and _COMPACT_COUNT.fullmatch(description) is None:
            value = parse_display_count(description)
            if value is not None:
                metrics[name] = value
                compact.discard(name)
                continue
        if name in metrics and name not in compact:
            continue
        label = description or node.text.strip()
        if _EXACT_COUNT.fullmatch(label):
            metrics[name] = int(re.sub(r'[,\s]', '', label))
            compact.discard(name)
            continue
        if _COMPACT_COUNT.fullmatch(label):
            value = parse_display_count(label)
            if value is not None:
                metrics[name] = value
                compact.add(name)
    if _like_count_is_private(xml):
        metrics.pop("like_count", None)
        compact.discard("like_count")
    return metrics, tuple(sorted(compact))


def _countless_reel_icons(xml: str) -> set[str]:
    missing: set[str] = set()
    for node in parse_ui_xml(xml):
        if _node_count(node) is not None:
            continue
        resource_id = node.resource_id.casefold()
        label = node.visible_text.casefold().strip()
        for name, markers in _COUNTLESS_ICON_MARKERS.items():
            if any(marker in resource_id for marker in markers) or (
                label in _COUNTLESS_ICON_LABELS[name]
                and node.class_name in {"android.widget.Button", "android.widget.ImageButton", "android.widget.ImageView"}
            ):
                missing.add(name)
    return missing


def _read_missing_ocr_counts(
    nodes: Sequence[UiNode], fields: set[str], read_words: Callable,
) -> tuple[dict[str, int], set[str]]:
    regions: dict[str, list[tuple[int, ...]]] = {}
    for node in nodes:
        name = _metric_key(node)
        if name in fields and (bounds := re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.bounds)):
            regions.setdefault(name, []).append(tuple(map(int, bounds.groups())))
    unique = {name: boxes[0] for name, boxes in regions.items() if len(boxes) == 1}
    if not unique:
        return {}, set()
    words = read_words()
    counts = {}
    uncertain = set()
    for name, (left, top, right, bottom) in unique.items():
        labels = [word["text"] for word in words
                  if left <= word["x"] < word["right"] <= right
                  and top <= word["y"] < word["bottom"] <= bottom]
        if len(labels) == 1 and re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", labels[0]):
            counts[name] = int(labels[0].replace(",", ""))
        elif any(re.search(r"\d", label) for label in labels):
            uncertain.add(name)
    return counts, uncertain


def _first_matching_node(nodes: Sequence[UiNode], predicate: object) -> UiNode | None:
    matcher = predicate
    return next((node for node in nodes if callable(matcher) and matcher(node)), None)


def _is_reel_detail_surface(xml: str) -> bool:
    """Profile tabs/thumbnails also mention Reels; require viewer-only IDs."""
    markers = {"clips_author_username", "clips_video_container", "clips_media_component"}
    return any(n.resource_id.rsplit("/", 1)[-1] in markers for n in parse_ui_xml(xml))


def _is_reel_paused(xml: str) -> bool:
    return any(
        node.resource_id.endswith(":id/clips_pause_button")
        and node.content_desc.strip().casefold() in {"play", "재생"}
        for node in parse_ui_xml(xml)
    )


_REEL_SERIES_AUTHOR_PATTERNS = (
    re.compile(r"^Profile picture of (?P<author>[A-Za-z0-9_.]+)", re.I),
    re.compile(r"^(?P<author>[A-Za-z0-9_.]+)님의 프로필 사진$"),
    re.compile(r"^From (?P<author>[A-Za-z0-9_.]+)$", re.I),
    re.compile(r"^Reel by (?P<author>[A-Za-z0-9_.]+)\.", re.I),
)


def _reel_authors(xml: str) -> list[str]:
    nodes = parse_ui_xml(xml)
    authors = [
        (node.text or node.content_desc).strip()
        for node in nodes
        if "author_username" in node.resource_id.casefold() and (node.text or node.content_desc).strip()
    ]
    if len(authors) != 1:
        return authors
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return authors
    candidate_elements = [
        *(
            element
            for component in root.findall('.//*[@resource-id="com.instagram.android:id/clips_author_info_component"]')
            for element in component.iter("node")
        ),
        *root.findall('.//*[@resource-id="com.instagram.android:id/clips_author_profile_pic"]'),
        *root.findall('.//*[@resource-id="com.instagram.android:id/clips_media_component"]'),
        *root.findall('.//*[@resource-id="com.instagram.android:id/clips_video_container"]'),
    ]
    fallbacks: list[str] = []
    for element in candidate_elements:
        label = (element.attrib.get("content-desc") or element.attrib.get("text") or "").strip()
        for pattern in _REEL_SERIES_AUTHOR_PATTERNS:
            if match := pattern.match(label):
                author = match.group("author")
                if author.casefold() not in {item.casefold() for item in fallbacks}:
                    fallbacks.append(author)
    if len(fallbacks) == 1 and not _reel_author_matches(fallbacks[0], authors[0]):
        return fallbacks
    return authors


def _is_reel_ready_for_metrics(xml: str, *, recollection: bool = False) -> bool:
    author = bool(_reel_authors(xml))
    metrics = extract_recollection_metrics(xml)[0] if recollection else extract_visible_metrics(xml)
    fields = set(metrics) | _countless_reel_icons(xml)
    return _is_reel_detail_surface(xml) and author and bool(fields.intersection(ANDROID_COLLECTION_FIELDS))


def _is_ui_read_error(error: object) -> bool:
    return any(marker in str(error).casefold() for marker in (
        "uiautomator", "empty or unreadable", "exec-out", "could not get idle state", "null root node",
    ))


def _is_reel_surface(xml: str) -> bool:
    nodes = parse_ui_xml(xml)
    return any(
        "clips_author_username" in node.resource_id.casefold()
        or "clips_" in node.resource_id.casefold()
        or "reel_viewer" in node.resource_id.casefold()
        or _metric_key(node)
        or "reel" in node.visible_text.casefold()
        for node in nodes
    )


def detect_android_rate_limit_signal(xml: str) -> str:
    """Return only an app-visible limit signal; it is not an HTTP status."""
    text = " ".join(node.visible_text for node in parse_ui_xml(xml))
    match = _RATE_LIMIT_SIGNAL.search(text)
    return match.group(0) if match else ""


def describe_android_surface(xml: str) -> str:
    """Return a stable, non-sensitive reason when a Reel URL did not render."""
    nodes = parse_ui_xml(xml)
    if not nodes:
        return "UIAutomator returned an empty or unreadable screen."
    resource_ids = " ".join(node.resource_id.casefold() for node in nodes)
    text = " ".join(node.visible_text.casefold() for node in nodes)
    if "com.android.chrome" in resource_ids or "com.google.android.googlequicksearchbox" in resource_ids:
        return "The Reel URL opened outside Instagram (browser/search surface)."
    if re.search(r"\b(?:log in|login)\b|로그인", text):
        return "Instagram is showing a login screen."
    if re.search(r"open\s+(?:this\s+)?(?:link\s+)?in\s+instagram|instagram에서\s*열기", text):
        return "Android is asking to open the link in Instagram."
    if re.search(r"(?:reel|video).{0,40}(?:not available|unavailable|deleted)|릴스.{0,30}(?:사용할 수 없|삭제)", text, re.I):
        return "The Reel is unavailable or was deleted."
    if detect_android_rate_limit_signal(xml):
        return "Instagram displayed a possible rate-limit message in the Android UI."
    if re.search(r"couldn.?t refresh|try again later|잠시 후 다시|새로고침", text, re.I):
        return "Instagram displayed a loading or temporary-error screen."
    return "Instagram did not render a recognizable Reel surface before the timeout."


def parse_hashtag_post_count(xml: str) -> tuple[int | None, str]:
    for node in parse_ui_xml(xml):
        text = node.visible_text.strip()
        for pattern in _POST_COUNT_PATTERNS:
            match = pattern.search(text)
            if match:
                return parse_display_count(match.group("count")), text
    return None, ""


class AndroidReelMetricsEnricher:
    """Collect Android-owned fields synchronously for one browser-discovered URL."""

    def __init__(
        self,
        *,
        adb_path: Path | None = None,
        device_id: str | None = None,
        ui_delay_seconds: float = 0.35,
        driver: AndroidUiDriver | None = None,
        diagnostics: CollectorDiagnostics | None = None,
    ) -> None:
        self._adb_path = Path(adb_path) if adb_path else default_adb_path()
        self._device_id = device_id or None
        self._delay = max(0.1, float(ui_delay_seconds))
        self._driver = driver
        self._driver_is_external = driver is not None
        self.diagnostics = diagnostics
        self._reel_paused_for_collection = False

    @property
    def driver(self) -> AndroidUiDriver:
        if self._driver is None:
            from .android_uiautomator import PersistentAndroidUiDriver
            self._driver = PersistentAndroidUiDriver(
                self._adb_path,
                self._device_id,
                diagnostics=self.diagnostics,
            )
        return self._driver

    def _ready(self) -> str:
        # Future-dated queue jobs previously caused two ADB probes every 0.5s.
        # Real UI commands still report failures immediately; cache only health.
        if (not self._driver_is_external
                and not getattr(self._driver, 'requires_ready_check', False)
                and time.monotonic() - getattr(self, "_last_ready_check", float("-inf")) < 5):
            return ""
        try:
            self.driver.ensure_ready()
        except AndroidMetricsError as error:
            self._last_ready_check = float("-inf")
            return str(error)
        self._last_ready_check = time.monotonic()
        return ""

    def reset_connection(self) -> None:
        """Discard ADB state after an emulator/server disconnect.

        Recreating the built-in driver retains the selected device and clears
        device-scoped caches. Tests and callers that inject their own driver
        retain ownership, but may expose the same hook if they need it.
        """
        self._last_ready_check = float("-inf")
        if self._driver_is_external:
            reset = getattr(self._driver, "reset_connection", None)
            if callable(reset):
                reset()
            return
        if self._driver is not None:
            self._device_id = getattr(self._driver, "device_id", self._device_id)
            close = getattr(self._driver, 'close', None)
            if callable(close):
                close()
        self._driver = None

    def close(self) -> None:
        if not self._driver_is_external:
            self.reset_connection()

    def _wait_for_surface(
        self,
        predicate: object,
        attempts: int = 4,
        timeout_seconds: float | None = None,
        max_consecutive_unreadable: int | None = None,
        target: str = "android_surface",
    ) -> str:
        if timeout_seconds is not None:
            attempts = max(attempts, int(timeout_seconds / self._delay) + 1)
        xml = ""
        consecutive_unreadable = 0
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        last_read_error = None
        for attempt in range(attempts):
            if attempt and deadline is not None and time.monotonic() >= deadline:
                break
            if attempt:
                if self.diagnostics is not None:
                    self.diagnostics.record_retry(
                        stage=self.diagnostics.current_stage or "WAIT_FOR_RENDER",
                        target=target,
                        attempt=attempt + 1,
                        total=attempts,
                        reason="ELEMENT_TIMEOUT",
                        previous_wait=self._delay,
                    )
                remaining = deadline - time.monotonic() if deadline is not None else self._delay
                if remaining <= 0:
                    break
                time.sleep(min(self._delay, remaining))
            try:
                if deadline is not None and isinstance(self.driver, AdbAndroidUiDriver):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    xml = self.driver.dump_ui(timeout_seconds=remaining)
                else:
                    xml = self.driver.dump_ui()
                last_read_error = None
            except AndroidMetricsError as error:
                if not _is_ui_read_error(error):
                    raise
                self._recover_instagram_anr()
                last_read_error = error
                consecutive_unreadable += 1
                if deadline is not None or consecutive_unreadable >= max(1, max_consecutive_unreadable or 2):
                    raise
                continue
            self._recover_instagram_anr(xml)
            rate_limit_signal = detect_android_rate_limit_signal(xml)
            if rate_limit_signal and self.diagnostics is not None:
                self.diagnostics.rate_limit_suspected(rate_limit_signal)
            if callable(predicate) and predicate(xml):
                return xml
            consecutive_unreadable = consecutive_unreadable + 1 if not parse_ui_xml(xml) else 0
            if max_consecutive_unreadable is not None:
                if consecutive_unreadable >= max(1, max_consecutive_unreadable):
                    break
        if last_read_error is not None:
            raise last_read_error
        return xml

    def _recover_instagram_anr(self, xml: str | None = None) -> None:
        if xml is not None and not any(re.fullmatch(
            r"Instagram\s*(?:isn['’]t responding|is not responding|이\s*응답하지 않습니다\.?)",
            node.visible_text.strip(), re.I,
        ) for node in parse_ui_xml(xml)):
            return
        focused_anr = getattr(self.driver, "focused_instagram_anr", None)
        restart = getattr(self.driver, "restart_unresponsive_instagram", None)
        if not callable(focused_anr) or not callable(restart):
            return
        try:
            window = focused_anr()
            if not window:
                return
            deadline = time.monotonic() + 60
            while (remaining := deadline - time.monotonic()) > 0:
                time.sleep(min(5, remaining))
                if focused_anr() != window:
                    return
        except AndroidMetricsError:
            # A failed ADB probe cannot confirm that the same app dialog persists.
            return
        restart()
        if self.diagnostics is not None:
            self.diagnostics.emit("INSTAGRAM_ANR_RELAUNCHED")
        raise InstagramAnrRecovered("Instagram was relaunched after an app-not-responding dialog.")

    def prepare_reel_retry(self) -> None:
        """Leave the stuck Reel so reopening the same deep link redraws it."""
        self.driver.press_back()
        time.sleep(self._delay)

    def reset_ui_read_timeout(self) -> None:
        self.driver._ui_dump_timeout_seconds = _ADB_UI_DUMP_TIMEOUT_SECONDS

    def recover_app(self, reel_url: str, timeout_seconds: float = 15.0) -> str:
        """Restart Instagram and require a Reel author and metric controls."""
        try:
            self.driver.restart_instagram(reel_url)
            self.driver._ui_dump_timeout_seconds = timeout_seconds
            time.sleep(max(1.0, self._delay))
            # Match normal Reel preparation; a restarted app may still animate.
            self._reel_paused_for_collection = False
            tap_center = getattr(self.driver, "tap_screen_center", None)
            if callable(tap_center):
                tap_center()
                time.sleep(self._delay)
            xml = self._wait_for_surface(_is_reel_ready_for_metrics, timeout_seconds=timeout_seconds, target="recovery_reel")
            self._reel_paused_for_collection = _is_reel_paused(xml)
            if _is_reel_ready_for_metrics(xml):
                return ""
            return "Instagram recovery did not render a Reel author and metric controls."
        except (AndroidMetricsError, OSError) as error:
            return str(error)

    def recover_ui(self, reel_url: str) -> str:
        """Probe connection and UI separately; restart the app only if needed."""
        error = self._ready()
        if error:
            return error
        for restart_app in (False, True):
            try:
                reset_ok = self.driver.reset_ui_automation()
                if reset_ok is False and not restart_app:
                    raise AndroidMetricsError("UIAutomator cleanup failed after UI read failure.")
                if restart_app:
                    self.driver.restart_instagram(reel_url)
                    self._reel_paused_for_collection = False
                    time.sleep(max(1.0, self._delay))
                    tap_center = getattr(self.driver, "tap_screen_center", None)
                    if callable(tap_center):
                        tap_center()
                xml = self._wait_for_surface(_is_reel_ready_for_metrics, timeout_seconds=_ANDROID_REEL_READY_TIMEOUT_SECONDS, target="recovery_reel")
                self._reel_paused_for_collection = _is_reel_paused(xml)
                if _is_reel_ready_for_metrics(xml):
                    return ""
                error = "UIAutomator returned an empty or unreadable screen."
            except (AndroidMetricsError, OSError) as exception:
                error = str(exception)
            if self.diagnostics is not None:
                self.diagnostics.emit("UI_RECOVERY_PROBE", restart_app=restart_app, error=error)
        return error

    def _reset_unreadable_instagram_surface(self) -> None:
        reset_surface = getattr(self.driver, "reset_instagram_surface", None)
        if callable(reset_surface):
            reset_surface()
            return
        reset_ui_automation = getattr(self.driver, "reset_ui_automation", None)
        if callable(reset_ui_automation):
            reset_ui_automation()

    def _open_hashtag_search_results(
        self,
        query: str,
        *,
        timeout_seconds: float = _ANDROID_TAG_SEARCH_READY_TIMEOUT_SECONDS,
    ) -> str:
        """Keep selecting the full Tags tab until its result rows render."""
        self.driver.open_instagram_search(f"#{query}")
        attempts = max(4, int(timeout_seconds / self._delay) + 1)
        xml = ""
        for attempt in range(attempts):
            if attempt:
                time.sleep(self._delay)
            xml = self.driver.dump_ui()
            if extract_related_hashtag_post_counts(xml, query):
                return xml
            # Instagram may rebuild the result page just after text entry and
            # override an early tap by switching back to For you. Prefer the
            # large TabWidget hitbox and retry until real Tag rows are visible.
            nodes = parse_ui_xml(xml)
            clickable_tags_bounds = _clickable_tags_tab_bounds(xml)
            tags_tab = _largest_tags_tab(nodes)
            retry_interval = max(1, int(0.75 / self._delay))
            if attempt % retry_interval == 0:
                primary_bounds = clickable_tags_bounds or (tags_tab.bounds if tags_tab is not None else "")
                fallback_bounds = _fallback_tags_tab_bounds(nodes)
                # Some Instagram versions expose the Tags label but ignore a
                # tap on its child view.  When that first tap has no effect,
                # alternate with the full fourth-tab hitbox below the search
                # field rather than tapping the same non-clickable label.
                retry_number = attempt // retry_interval
                target_bounds = (
                    fallback_bounds
                    if primary_bounds and fallback_bounds and fallback_bounds != primary_bounds and retry_number % 2
                    else primary_bounds or fallback_bounds
                )
                if target_bounds:
                    self.driver.tap_bounds(target_bounds)
        return xml

    def _verify_reel_caption(
        self, reel_xml: str, author: str, expected_username: str, expected_caption: str,
        *, expected_hashtags: str | None = None,
    ) -> str | None:
        hashtag_only = not reel_caption_key(expected_caption) and expected_hashtags is not None
        if not expected_username.strip():
            failure = "EXPECTED_USERNAME_MISSING"
        elif not _reel_author_matches(expected_username, author):
            failure = "AUTHOR_MISMATCH"
        else:
            caption_nodes = _reel_caption_nodes(reel_xml)
            expected = reel_caption_key(expected_caption)

            raw_previews = [node.get("content-desc") or node.get("text") or "" for node in caption_nodes]
            previews = {preview.strip() for preview in raw_previews}

            if all(preview.casefold() in {"", "see more", "더 보기", "…", "..."} for preview in previews):
                return ""
            if previews and all(_hashtag_preview_matches(preview, expected_hashtags) for preview in previews):
                preview = max(previews, key=len)
                if self.diagnostics is not None:
                    self.diagnostics.emit("REEL_IDENTITY_HASHTAG_PREVIEW_VERIFIED", preview=preview,
                                          web_hashtags=expected_hashtags)
                return preview
            if not hashtag_only:
                for preview in previews:
                    prefix = _caption_preview_prefix(preview)
                    if prefix and expected.startswith(prefix):
                        if self.diagnostics is not None:
                            self.diagnostics.emit("REEL_IDENTITY_CAPTION_VERIFIED", compared_characters=len(prefix))
                        return preview
            # Author matched! When caption is edited or differing, fall back to author-only match.
            caption_mismatched = bool(expected_caption or hashtag_only)
            self._last_caption_modified = caption_mismatched
            if self.diagnostics is not None:
                self.diagnostics.emit(
                    "REEL_IDENTITY_AUTHOR_VERIFIED",
                    expected_username=expected_username,
                    android_username=author,
                    reason="CAPTION_MISMATCH_AUTHOR_ACCEPTED" if caption_mismatched else "AUTHOR_ONLY",
                )
            return ""
        if self.diagnostics is not None:
            self.diagnostics.android_identity_failure(
                "REEL_IDENTITY_CAPTION_FAILED",
                reason=failure,
                detailed_only=failure not in {"HASHTAG_PREVIEW_NOT_VERIFIED", "CAPTION_PREVIEW_MISMATCH"},
                expected_username=expected_username,
                android_username=author,
                web_caption=expected_caption,
                web_hashtags=expected_hashtags,
                android_caption_previews=[
                    node.get("content-desc") or node.get("text") or ""
                    for node in _reel_caption_nodes(reel_xml)
                ],
                android_ui_xml=reel_xml,
            )
        return None

    def enrich(self, reel_url: str, *, recollection: bool = False, expected_username: str = "", expected_like_count: object = None, expected_caption: str = "", expected_hashtags: str | None = None) -> AndroidMetricResult:
        target = normalize_search_grid_reel_url(reel_url)
        if target is None:
            return AndroidMetricResult(status="unavailable", error="Invalid Reel identity")
        if self.diagnostics is not None:
            self.diagnostics.update_media(current_url=reel_url)
            self.diagnostics.stage_start("PREFLIGHT")
        error = self._ready()
        if error:
            if self.diagnostics is not None:
                self.diagnostics.stage_failed("PREFLIGHT", reason="ANDROID_UNAVAILABLE", error=error)
            return AndroidMetricResult(status="unavailable", error=error)
        if self.diagnostics is not None:
            self.diagnostics.stage_success("PREFLIGHT")
        try:
            self._reel_paused_for_collection = False
            reel_xml = ""
            def reel_ready(candidate: str) -> bool:
                authors = _reel_authors(candidate)
                return (len(authors) == 1
                        and _reel_author_matches(expected_username, authors[0])
                        and _is_reel_ready_for_metrics(candidate, recollection=recollection))
            if self.diagnostics is not None:
                self.diagnostics.stage_start("OPEN_REEL")
            for launch_attempt, ready_timeout in enumerate(
                (_ANDROID_REEL_READY_TIMEOUT_SECONDS, _ANDROID_RETRY_READY_TIMEOUT_SECONDS),
                start=1,
            ):
                if launch_attempt > 1 and self.diagnostics is not None:
                    self.diagnostics.record_retry(
                        stage="OPEN_REEL",
                        target="reel_deep_link",
                        attempt=launch_attempt,
                        total=2,
                        reason="UI_RENDER_FAILED",
                        previous_wait=self._delay,
                    )
                self.driver.open_instagram_url(reel_url)
                # Request the usual playback action; confirm its actual state
                # from the hierarchy rather than assuming a tap paused video.
                self._reel_paused_for_collection = False
                tap_center = getattr(self.driver, "tap_screen_center", None)
                if callable(tap_center):
                    time.sleep(max(1.0, self._delay))
                    tap_center()
                if self.diagnostics is not None:
                    self.diagnostics.stage_start("WAIT_FOR_RENDER", target="reel")
                try:
                    reel_xml = self._wait_for_surface(
                        reel_ready,
                        timeout_seconds=ready_timeout,
                        max_consecutive_unreadable=4,
                        target="reel",
                    )
                except AndroidMetricsError as error:
                    if isinstance(error, InstagramAnrRecovered) and launch_attempt < 2:
                        continue
                    if launch_attempt == 2 or not _is_ui_read_error(error):
                        raise
                    if self.diagnostics is not None:
                        self.diagnostics.ui_event("REEL_REOPEN_AFTER_UI_READ_FAILURE", error=str(error)[:500])
                    self.prepare_reel_retry()
                    continue
                self._reel_paused_for_collection = _is_reel_paused(reel_xml)
                if reel_ready(reel_xml):
                    if self.diagnostics is not None:
                        self.diagnostics.stage_success("WAIT_FOR_RENDER", target="reel")
                        self.diagnostics.stage_success("OPEN_REEL")
                        self.diagnostics.ui_event("MEDIA_RENDER_OK")
                        self.diagnostics.ui_event(
                            "VIDEO_RENDER_STATUS_UNAVAILABLE",
                            reason="UIAutomator confirms the Reel surface but cannot verify continuous playback.",
                        )
                    break
                if launch_attempt == 1:
                    self.prepare_reel_retry()
            else:
                if self.diagnostics is not None:
                    self.diagnostics.android_identity_failure("REEL_IDENTITY_REJECTED", expected_url=reel_url,
                                          android_ui_xml=reel_xml,
                                          expected_username=expected_username,
                                          details={"web_caption": expected_caption, "web_hashtags": expected_hashtags},
                                          observed_usernames=_reel_authors(reel_xml))
                if self.diagnostics is not None:
                    self.diagnostics.stage_timeout("WAIT_FOR_RENDER", target="reel")
                    self.diagnostics.stage_failed("OPEN_REEL", reason="UI_RENDER_FAILED")
                    self.diagnostics.ui_event("UI_RENDER_FAILED", target="reel")
                if not parse_ui_xml(reel_xml):
                    self._reset_unreadable_instagram_surface()
                return AndroidMetricResult(status="unavailable", error=describe_android_surface(reel_xml))
            if self.diagnostics is not None:
                self.diagnostics.stage_start("READ_USERNAME")
                author = next(iter(_reel_authors(reel_xml)), "")
                if author:
                    self.diagnostics.update_media(username=author)
                    self.diagnostics.stage_success("READ_USERNAME")
                else:
                    self.diagnostics.stage_failed(
                        "READ_USERNAME",
                        reason="ELEMENT_LOOKUP_FAILED",
                        target="reel_author",
                    )
            seen_fields: set[str] = set()
            for node in parse_ui_xml(reel_xml):
                name = _metric_key(node)
                if name in {*ANDROID_COLLECTION_FIELDS, "comment_count"} and _node_count(node) is not None:
                    if name in seen_fields:
                        return AndroidMetricResult(status="unavailable", error="Ambiguous Reel metric nodes")
                    seen_fields.add(name)
            author = next(iter(_reel_authors(reel_xml)), "")
            metrics, compact_fields = extract_recollection_metrics(reel_xml)
            like_count_private = _like_count_is_private(reel_xml)
            uncertain_ocr_fields = set()
            read_words = getattr(self.driver, "read_screen_ocr", None)
            if callable(read_words):
                read_words = cache(read_words)
                fields = {*ANDROID_COLLECTION_FIELDS, "comment_count"} - metrics.keys()
                if like_count_private:
                    fields.discard("like_count")
                ocr_counts, uncertain_ocr_fields = _read_missing_ocr_counts(parse_ui_xml(reel_xml), fields, read_words)
                metrics.update(ocr_counts)
                if ocr_counts and self.diagnostics is not None:
                    self.diagnostics.ui_event("METRIC_OCR_USED", fields=sorted(ocr_counts))
            caption = self._verify_reel_caption(
                reel_xml, author, expected_username, expected_caption,
                expected_hashtags=expected_hashtags,
            )
            if caption is None:
                return AndroidMetricResult(status="unavailable", error="Reel author and caption could not be verified")
            # Only a verified Reel may interpret a rendered count-less button
            # as zero. Absent controls and failed UI/identity reads stay missing.
            for name in _countless_reel_icons(reel_xml) - uncertain_ocr_fields:
                if name != "like_count" or not like_count_private:
                    metrics.setdefault(name, 0)
            for name in {"comment_count", "repost_count", "share_count", "saved_count"} - uncertain_ocr_fields:
                metrics.setdefault(name, 0)
            if not recollection:
                metrics = {name:value for name,value in metrics.items()
                           if name in ANDROID_COLLECTION_FIELDS or (name == "comment_count" and value == 0)}
            if self.diagnostics is not None:
                missing_fields = [
                    field_name
                    for field_name in ANDROID_COLLECTION_FIELDS
                    if field_name not in metrics
                ]
                if missing_fields:
                    self.diagnostics.ui_event("METADATA_MISSING", fields=missing_fields)
                else:
                    self.diagnostics.ui_event("METADATA_RENDER_OK")
            if not any(name in metrics for name in ANDROID_COLLECTION_FIELDS):
                return AndroidMetricResult(
                    status="unavailable",
                    error="Instagram rendered the Reel author but exposed no readable count after retries.",
                )
            hashtag_preview_verified = _hashtag_preview_matches(caption, expected_hashtags)
            return AndroidMetricResult(
                metrics=metrics,
                observed_username=author,
                observed_caption=caption if expected_caption and not hashtag_preview_verified else "",
                observed_hashtags=caption if caption and (not expected_caption or hashtag_preview_verified) else None,
                identity_author_only=caption == "",
                caption_modified=getattr(self, "_last_caption_modified", False),
                compact_fields=compact_fields,
                recollection=recollection,
                like_count_private=like_count_private,
            )
        except AndroidMetricsError as error:
            if self.diagnostics is not None:
                self.diagnostics.stage_failed(
                    self.diagnostics.current_stage or "ANDROID_UI",
                    reason=type(error).__name__,
                    error=str(error),
                )
            if "uiautomator" in str(error).casefold() or "exec-out" in str(error).casefold():
                self._reset_unreadable_instagram_surface()
            return AndroidMetricResult(status="unavailable", error=str(error))

    def pause_current_reel(self) -> bool:
        """Pause the Reel this worker just opened before entering queue idle."""
        try:
            xml = self.driver.dump_ui()
            if not _is_reel_detail_surface(xml):
                return False
            if _is_reel_paused(xml):
                self._reel_paused_for_collection = True
                return True
            pause = next((node for node in parse_ui_xml(xml)
                          if node.resource_id.endswith(":id/clips_pause_button")
                          and node.content_desc.strip().casefold() in {"pause", "일시정지", "일시 정지"}), None)
            if pause is None or not self.driver.tap_bounds(pause.bounds):
                return False
            time.sleep(self._delay)
            self._reel_paused_for_collection = _is_reel_paused(self.driver.dump_ui())
            return self._reel_paused_for_collection
        except AndroidMetricsError:
            return False

    def collect_profile_view_count(
        self, url: str, username: str, *, expected_caption: str = "", expected_hashtags: str | None = None, should_yield: Callable[[], bool] = lambda: False,
        timeout_seconds: float = 60.0,
    ) -> AndroidMetricResult:
        """Fallback after web failure: read exact views in Likes and plays."""
        if not ANDROID_PROFILE_VIEW_FALLBACK_ENABLED:
            return AndroidMetricResult(status="disabled", error="View assist yielded: Likes and plays fallback disabled")
        target = re.search(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)/", url)
        if target is None or not re.fullmatch(r"[A-Za-z0-9_.]+", username):
            return AndroidMetricResult(status="unavailable", error="Invalid view target identity")
        deadline = time.monotonic() + timeout_seconds
        panel_open = False
        def stopped_error() -> str:
            if should_yield():
                return "View assist yielded to other work"
            if time.monotonic() >= deadline:
                return "View fallback timed out before exact detail count"
            return ""
        try:
            if error := stopped_error() or self._ready():
                return AndroidMetricResult(status="unavailable", error=error)
            self.driver.open_instagram_url(url)
            # Match the regular metric collector: pause playback before the
            # first accessibility dump so video updates do not prevent idle.
            time.sleep(max(1.0, self._delay))
            self.driver.tap_screen_center()
            detail = self._wait_for_surface(_is_reel_detail_surface, attempts=2)
            if not _is_reel_detail_surface(detail):
                return AndroidMetricResult(status="unavailable", error="Reel detail did not render")
            authors = _reel_authors(detail)
            if len(authors) != 1 or not _reel_author_matches(username, authors[0]):
                return AndroidMetricResult(status="unavailable", error="Reel author mismatch")
            caption = self._verify_reel_caption(
                detail, authors[0], username, expected_caption, expected_hashtags=expected_hashtags,
            )
            if caption is None:
                return AndroidMetricResult(status="unavailable", error="Reel author and caption could not be verified")
            like_count = next((n for n in parse_ui_xml(detail) if n.resource_id.endswith((":id/like_count", ":id/likes_count"))
                               or n.text.strip().casefold() in {"likes", "좋아요"}), None)
            if like_count is None:
                return AndroidMetricResult(status="unavailable", error="Likes and plays entry unavailable")
            if error := stopped_error():
                return AndroidMetricResult(status="unavailable", error=error)
            self.driver.tap_bounds(like_count.bounds)
            panel_open = True
            panel = self._wait_for_surface(
                lambda x: any(n.resource_id.endswith(":id/video_view_count_text") for n in parse_ui_xml(x)), attempts=2,
            )
            if error := stopped_error():
                return AndroidMetricResult(status="unavailable", error=error)
            for node in parse_ui_xml(panel):
                if not node.resource_id.endswith(":id/video_view_count_text"):
                    continue
                for value in (node.text.strip(), re.sub(r"\s+views$", "", node.content_desc.strip(), flags=re.I)):
                    if re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", value):
                        hashtag_preview_verified = _hashtag_preview_matches(caption, expected_hashtags)
                        return AndroidMetricResult(metrics={"view_count": int(value.replace(",", ""))},
                                                   observed_caption=caption if expected_caption and not hashtag_preview_verified else "", observed_username=authors[0],
                                                   observed_hashtags=caption if caption and (not expected_caption or hashtag_preview_verified) else None,
                                                   identity_author_only=caption == "")
            return AndroidMetricResult(status="unavailable", error="Exact Likes and plays view_count unavailable")
        except (AndroidMetricsError, OSError) as error:
            return AndroidMetricResult(status="unavailable", error=str(error)[:500])
        finally:
            if panel_open:
                try:
                    self.driver.press_back()
                except (AndroidMetricsError, OSError) as error:
                    warnings.warn(f"Likes and plays cleanup failed: {error}", RuntimeWarning)

    def collect_related_hashtag_post_counts(
        self,
        hashtags: Sequence[str],
        *,
        on_rows: Callable[[Sequence[dict[str, object]]], None] | None = None,
        progress_index: int | None = None,
        progress_total: int | None = None,
    ) -> list[dict[str, object]]:
        """Read every related tag rendered by Instagram's in-app search result."""
        error = self._ready()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        results: list[dict[str, object]] = []
        queries = [str(hashtag).strip().lstrip("#") for hashtag in hashtags if str(hashtag).strip().lstrip("#")]
        total = progress_total or len(queries)
        for local_index, query in enumerate(queries, start=1):
            index = progress_index if progress_index is not None and len(queries) == 1 else local_index
            progress = f"[ANDROID hashtag {index}/{total}]"
            if error:
                results.append({
                    "collected_at": timestamp, "query_hashtag": query, "hashtag": query,
                    "post_count": "", "raw_post_count": "", "source": "android_search_tags",
                    "status": "unavailable", "error": error,
                })
                continue
            try:
                xml = self._open_hashtag_search_results(query)
                seen: set[str] = set()
                unchanged_attempts = 0
                query_rows: list[dict[str, object]] = []
                skipped_low_count = 0
                for _ in range(_ANDROID_TAG_SEARCH_SCROLL_ATTEMPTS):
                    page_rows = extract_related_hashtag_post_counts(xml, query, collected_at=timestamp)
                    visible_tags = tuple(str(row.get("hashtag", "")).casefold() for row in page_rows)
                    added = 0
                    accepted_rows: list[dict[str, object]] = []
                    for row in page_rows:
                        key = str(row["hashtag"]).casefold()
                        if key in seen:
                            continue
                        seen.add(key)
                        added += 1
                        post_count = row.get("post_count")
                        if isinstance(post_count, int) and post_count < _MIN_HASHTAG_POST_COUNT:
                            skipped_low_count += 1
                            continue
                        query_rows.append(row)
                        accepted_rows.append(row)
                    if accepted_rows and on_rows is not None:
                        on_rows(accepted_rows)
                    unchanged_attempts = unchanged_attempts + 1 if added == 0 else 0
                    if unchanged_attempts >= _ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS:
                        break
                    self.driver.scroll_down()
                    xml = self._wait_for_surface(
                        lambda candidate: tuple(
                            str(row.get("hashtag", "")).casefold()
                            for row in extract_related_hashtag_post_counts(candidate, query, collected_at=timestamp)
                        ) != visible_tags,
                        timeout_seconds=max(
                            _ANDROID_TAG_SEARCH_PAGE_CHANGE_TIMEOUT_SECONDS,
                            self._delay * 3,
                        ),
                    )
                if query_rows:
                    results.extend(query_rows)
                    print(f"{progress} #{query} -> related {len(query_rows)}")
                elif seen:
                    print(
                        f"{progress} #{query} -> related 0 "
                        f"(skipped under {_MIN_HASHTAG_POST_COUNT:,}: {skipped_low_count})"
                    )
                else:
                    message = "Instagram search did not expose related hashtag post counts."
                    print(f"{progress} #{query} -> unavailable ({message})")
                    results.append({
                        "collected_at": timestamp, "query_hashtag": query, "hashtag": query,
                        "post_count": "", "raw_post_count": "", "source": "android_search_tags",
                        "status": "unavailable", "error": message,
                    })
            except AndroidMetricsError as exception:
                print(f"{progress} #{query} -> unavailable ({exception})")
                results.append({
                    "collected_at": timestamp, "query_hashtag": query, "hashtag": query,
                    "post_count": "", "raw_post_count": "", "source": "android_search_tags",
                    "status": "unavailable", "error": str(exception),
                })
        return results

    def collect_hashtag_post_counts(self, hashtags: Sequence[str]) -> list[dict[str, object]]:
        """Read the exact query tag's post total from Instagram's Tags search tab.

        This deliberately searches one tag at a time instead of opening the
        public tag URL.  Instagram's app search is the surface that displays
        the Tag result and its post total together, and it lets the idle
        worker move on as soon as it has found the requested tag rather than
        spending time collecting every related result.
        """
        error = self._ready()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        results: list[dict[str, object]] = []
        for hashtag in hashtags:
            normalized = str(hashtag).strip().lstrip("#")
            if not normalized:
                continue
            if error:
                results.append({
                    "collected_at": timestamp, "query_hashtag": normalized, "hashtag": normalized,
                    "post_count": "", "raw_post_count": "", "source": "android_search_tags_exact",
                    "status": "unavailable", "error": error,
                })
                continue
            try:
                xml = self._open_hashtag_search_results(normalized)

                matched: dict[str, object] | None = None
                unchanged_attempts = 0
                previous_visible_tags: tuple[str, ...] = ()
                for _ in range(_ANDROID_TAG_SEARCH_SCROLL_ATTEMPTS):
                    rows = extract_related_hashtag_post_counts(xml, normalized, collected_at=timestamp)
                    matched = next(
                        (
                            row for row in rows
                            if str(row.get("hashtag", "")).casefold() == normalized.casefold()
                        ),
                        None,
                    )
                    if matched is not None:
                        break
                    visible_tags = tuple(str(row.get("hashtag", "")).casefold() for row in rows)
                    unchanged_attempts = (
                        unchanged_attempts + 1
                        if visible_tags == previous_visible_tags
                        else 0
                    )
                    previous_visible_tags = visible_tags
                    if unchanged_attempts >= _ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS:
                        break
                    self.driver.scroll_down()
                    time.sleep(self._delay)
                    xml = self.driver.dump_ui()

                if matched is None:
                    results.append({
                        "collected_at": timestamp, "query_hashtag": normalized, "hashtag": normalized,
                        "post_count": "", "raw_post_count": "", "source": "android_search_tags_exact",
                        "status": "unavailable", "error": "Instagram Tags search did not expose the exact hashtag post count.",
                    })
                    continue
                post_count = matched.get("post_count")
                if isinstance(post_count, int) and post_count < _MIN_HASHTAG_POST_COUNT:
                    print(
                        f"[ANDROID hashtag] #{normalized} -> skipped "
                        f"(under {_MIN_HASHTAG_POST_COUNT:,} posts)"
                    )
                    continue
                results.append({
                    **matched,
                    "query_hashtag": normalized,
                    "hashtag": normalized,
                    "source": "android_search_tags_exact",
                })
            except AndroidMetricsError as exception:
                results.append({
                    "collected_at": timestamp, "query_hashtag": normalized, "hashtag": normalized,
                    "post_count": "", "raw_post_count": "", "source": "android_search_tags_exact",
                    "status": "unavailable", "error": str(exception),
                })
        return results


def merge_android_metrics(record: dict[str, object], result: AndroidMetricResult) -> dict[str, object]:
    """Use Android counts where observed, with web priority for likes and comments."""
    merged = dict(record)
    fields = (*ANDROID_COLLECTION_FIELDS, "comment_count") if result.recollection or result.metrics.get("comment_count") == 0 else ANDROID_COLLECTION_FIELDS
    for field_name in fields:
        if field_name in result.metrics and field_name not in result.compact_fields:
            merged[field_name] = result.metrics[field_name]
    if result.like_count_private is True:
        merged["like_count"] = "X"
    merged = apply_metric_visibility_rules(
        merged,
        comment_count_disabled=result.comment_count_disabled is True,
    )
    merged.update({field: record[field] for field in WEB_PREFERRED_COUNT_FIELDS
                   if has_web_count(record.get(field))})
    return merged


def has_web_count(value: object) -> bool:
    return str(value if value is not None else "").strip().casefold() == "x" or (
        not isinstance(value, bool) and not needs_android_metric_fallback(value)
    )


def apply_metric_visibility_rules(
    record: dict[str, object],
    *,
    comment_count_disabled: bool = False,
) -> dict[str, object]:
    """Resolve counts that Instagram intentionally renders without a number."""
    resolved = dict(record)
    if comment_count_disabled:
        resolved["comment_count"] = "X"

    return resolved


def needs_android_metric_fallback(value: object) -> bool:
    """Return whether a count is absent, unavailable, or only compactly rendered."""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value < 0
    if isinstance(value, float):
        return value < 0 or not value.is_integer()
    text = str(value).strip()
    if not text or text.casefold() in {"x", "unavailable", "none", "null"}:
        return True
    return _EXACT_COUNT.fullmatch(text) is None


def clear_android_owned_fields(record: dict[str, object]) -> dict[str, object]:
    cleared = dict(record)
    for field_name in ANDROID_OWNED_FIELDS:
        cleared[field_name] = ""
    return cleared


def summarize_related_hashtag_counts(
    hashtags: Sequence[str],
    rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Count unique related hashtag names rendered for each Android query."""
    summaries: list[dict[str, object]] = []
    for hashtag in hashtags:
        query = str(hashtag).strip().lstrip("#")
        matching = [
            row for row in rows
            if str(row.get("query_hashtag", "")).casefold() == query.casefold()
        ]
        names = {
            str(row.get("hashtag", "")).strip().lstrip("#").casefold()
            for row in matching
            if row.get("status") == "collected" and str(row.get("hashtag", "")).strip()
        }
        errors = [str(row.get("error", "")).strip() for row in matching if row.get("error")]
        summaries.append({
            "collected_at": next((str(row.get("collected_at", "")) for row in matching if row.get("collected_at")), ""),
            "query_hashtag": query,
            "related_hashtag_count": len(names) if names else "",
            "source": "android_search_tags_count",
            "status": "collected" if names else "unavailable",
            "error": errors[0] if errors else ("" if names else "Instagram search exposed no related hashtags."),
        })
    return summaries


def _hashtag_snapshot_key(row: dict[str, object]) -> tuple[str, str]:
    return (
        str(row.get("query_hashtag", "")).strip().lstrip("#").casefold(),
        str(row.get("hashtag", "")).strip().lstrip("#").casefold(),
    )


def _hashtag_snapshot_identity(row: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(row.get("collected_at", "")).strip(),
        *_hashtag_snapshot_key(row),
    )


def _hashtag_snapshot_count(value: object) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _hashtag_snapshot_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def annotate_hashtag_collection_history(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Number Android tag snapshots and compare each row with its predecessor."""
    history: dict[tuple[str, str], list[dict[str, object]]] = {}
    annotated: list[dict[str, object]] = []
    for row in rows:
        snapshot = dict(row)
        key = _hashtag_snapshot_key(snapshot)
        if not key[0] or not key[1]:
            snapshot.update({"collection_number": "", "hours_since_previous": "", "media_count_change": ""})
            annotated.append(snapshot)
            continue
        previous = history.get(key, [])[-1] if history.get(key) else None
        snapshot.update({
            "collection_number": len(history.get(key, [])) + 1,
            "hours_since_previous": "",
            "media_count_change": "",
        })
        if previous is not None:
            previous_time = _hashtag_snapshot_time(previous.get("collected_at", ""))
            current_time = _hashtag_snapshot_time(snapshot.get("collected_at", ""))
            if previous_time is not None and current_time is not None:
                hours = (current_time - previous_time).total_seconds() / 3_600
                snapshot["hours_since_previous"] = f"{hours:.2f}".rstrip("0").rstrip(".")
            previous_count = _hashtag_snapshot_count(previous.get("post_count") or previous.get("media_count", ""))
            current_count = _hashtag_snapshot_count(snapshot.get("post_count") or snapshot.get("media_count", ""))
            if previous_count is not None and current_count is not None:
                snapshot["media_count_change"] = current_count - previous_count
        history.setdefault(key, []).append(snapshot)
        annotated.append(snapshot)
    return annotated


def write_hashtag_post_counts(
    data_dir: Path,
    rows: Sequence[dict[str, object]],
    *,
    replace_matching_snapshots: bool = False,
) -> dict[str, Path]:
    """Append the normalized hashtag media-count snapshot in all formats."""
    fields = [
        "collection_number", "hours_since_previous", "media_count_change",
        "collected_at", "query_hashtag", "hashtag", "media_count", "raw_post_count", "error",
    ]
    destination = Path(data_dir)
    destination.mkdir(parents=True, exist_ok=True)
    csv_path = destination / "hashtags.csv"
    json_path = destination / "hashtags.json"
    existing: list[dict[str, object]] = []
    if csv_path.exists() and csv_path.stat().st_size:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            existing = list(csv.DictReader(handle))
    def is_kept(row: dict[str, object]) -> bool:
        count = _hashtag_snapshot_count(row.get("post_count") or row.get("media_count", ""))
        return count is None or count >= _MIN_HASHTAG_POST_COUNT

    existing = [row for row in existing if is_kept(row)]
    rows = [row for row in rows if is_kept(row)]
    if replace_matching_snapshots:
        replacement_ids = {_hashtag_snapshot_identity(row) for row in rows}
        existing = [
            row for row in existing
            if _hashtag_snapshot_identity(row) not in replacement_ids
        ]
    snapshots = annotate_hashtag_collection_history([*existing, *rows])
    exported_rows = [
        {
            field_name: (
                snapshot.get("post_count")
                if field_name == "media_count" and snapshot.get("post_count") not in {None, ""}
                else snapshot.get(field_name, "")
            )
            for field_name in fields
        }
        for snapshot in snapshots
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(exported_rows)
    json_path.write_text(
        json.dumps(exported_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    from src.exporters.instagram_collector import sync_combined_xlsx, write_xlsx_workbook

    xlsx_path = destination / "hashtags.xlsx"
    write_xlsx_workbook(
        xlsx_path,
        [("hashtags", [fields, *[[str(row.get(field_name, "") or "") for field_name in fields] for row in exported_rows]])],
    )
    sync_combined_xlsx(destination)
    return {"csv": csv_path, "json": json_path, "xlsx": xlsx_path}


def collect_android_related_hashtag_post_counts(
    data_dir: Path | str,
    hashtags: Sequence[str],
    *,
    adb_path: Path | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = _ANDROID_TAG_SEARCH_UI_DELAY_SECONDS,
) -> tuple[list[dict[str, object]], dict[str, Path]]:
    """Run the Android search-Tag collection as a standalone command."""
    enricher = AndroidReelMetricsEnricher(
        adb_path=adb_path,
        device_id=device_id,
        ui_delay_seconds=ui_delay_seconds,
    )
    try:
        rows = enricher.collect_related_hashtag_post_counts(hashtags)
        return rows, write_hashtag_post_counts(Path(data_dir), rows)
    finally:
        enricher.close()


def collect_android_related_hashtag_counts(
    hashtags: Sequence[str],
    *,
    adb_path: Path | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = _ANDROID_TAG_SEARCH_UI_DELAY_SECONDS,
) -> list[dict[str, object]]:
    """Scroll Android Tags results and return only each query's unique count."""
    _, summaries = collect_android_related_hashtag_rows_and_counts(
        hashtags,
        adb_path=adb_path,
        device_id=device_id,
        ui_delay_seconds=ui_delay_seconds,
    )
    return summaries


def collect_android_related_hashtag_rows_and_counts(
    hashtags: Sequence[str],
    *,
    adb_path: Path | None = None,
    device_id: str | None = None,
    ui_delay_seconds: float = _ANDROID_TAG_SEARCH_UI_DELAY_SECONDS,
    on_rows: Callable[[Sequence[dict[str, object]]], None] | None = None,
    progress_index: int | None = None,
    progress_total: int | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return every Android Tags row plus one unique-related-tag summary per query."""
    enricher = AndroidReelMetricsEnricher(
        adb_path=adb_path,
        device_id=device_id,
        ui_delay_seconds=ui_delay_seconds,
    )
    try:
        rows = enricher.collect_related_hashtag_post_counts(
            hashtags,
            on_rows=on_rows,
            progress_index=progress_index,
            progress_total=progress_total,
        )
        return rows, summarize_related_hashtag_counts(hashtags, rows)
    finally:
        enricher.close()
