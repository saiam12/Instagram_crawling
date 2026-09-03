"""Android-app enrichment for the browser collector's Reel records.

The browser owns identity and static Reel fields.  This module opens that
already-collected URL in the logged-in Instagram Android app and reads only
the app-visible metrics that are unreliable or absent in browser responses.
No account-changing action is issued: all ADB commands are navigation,
UIAutomator inspection, or taps on visible read-only controls.
"""

from __future__ import annotations

import csv
import ctypes
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Protocol, Sequence
from urllib.parse import quote
from xml.etree import ElementTree


INSTAGRAM_PACKAGE = "com.instagram.android"
ANDROID_OWNED_FIELDS = (
    "like_count",
    "view_count",
    "comment_count",
    "share_count",
    "repost_count",
    "saved_count",
    "audio_name",
)
METRIC_FIELDS = ANDROID_OWNED_FIELDS[:-1]
_LIKE_DETAIL_LABELS = ("like number is", "view likes", "좋아요 수", "좋아요 보기")
_COMMENT_RESOURCE_MARKERS = ("comment_button", "comment_count", "comments_count")
_COMMENT_DETAIL_LABELS = ("comment number is", "view comments", "댓글 수", "댓글 보기")
_NO_COMMENTS = re.compile(r"\bno\s+comments\s+yet\b|아직\s*댓글이\s*(없습니다|없어요)", re.I)
_COMMENTS_DISABLED = re.compile(r"\bcomments?\s+(?:are|is)\s+(?:turned\s+off|disabled)\b|댓글\s*(?:기능이\s*)?(?:꺼져\s*있|사용할\s*수\s*없)", re.I)
_LIKE_PRIVATE = re.compile(r"only\s+.+?\s+can\s+see\s+the\s+total\s+number\s+of\s+likes|좋아요\s*수는\s*.+?만\s*볼\s*수\s*있", re.I)
_COMPACT_COUNT = re.compile(r"(?P<number>\d+(?:[.,]\d+)?)\s*(?P<unit>[KMBkmb만천])")
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
    ("share_count", ("share_count", "shares_count", "share_number", "send_count")),
    ("repost_count", ("repost_count", "reposts_count", "reshare_count", "reshare_number")),
    ("saved_count", ("save_count", "saved_count", "saves_count", "bookmark_count")),
)
_METRIC_TEXT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("like_count", ("like number is", " likes", "좋아요")),
    ("view_count", ("view count", " views", "조회수")),
    ("comment_count", ("comment number is", " comments", "댓글")),
    ("share_count", ("share number is", "share count", " shares", "공유 수")),
    ("repost_count", ("repost number is", " repost", "리포스트")),
    ("saved_count", ("save number is", " saves", "저장")),
)
_ANDROID_REEL_READY_TIMEOUT_SECONDS = 5.0
_ANDROID_RETRY_READY_TIMEOUT_SECONDS = 2.0
_ANDROID_TAG_SEARCH_SCROLL_ATTEMPTS = 120
_ANDROID_TAG_SEARCH_UNCHANGED_ATTEMPTS = 3
_ANDROID_TAG_SEARCH_READY_TIMEOUT_SECONDS = 15.0
_ANDROID_TAG_SEARCH_INITIAL_WAIT_SECONDS = 0.2
_ANDROID_TAG_SEARCH_PAGE_CHANGE_TIMEOUT_SECONDS = 0.75
_ANDROID_TAG_SEARCH_UI_DELAY_SECONDS = 0.2
_MIN_HASHTAG_POST_COUNT = 1_000
_ADB_COMMAND_TIMEOUT_SECONDS = 15.0
_WINDOWS_CLIPBOARD_SYNC_SECONDS = 0.8
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


class AndroidMetricsError(RuntimeError):
    """The app is unavailable or did not render a usable public surface."""


@dataclass(frozen=True)
class UiNode:
    text: str
    content_desc: str
    resource_id: str
    bounds: str
    class_name: str = ""

    @property
    def visible_text(self) -> str:
        if self.text == self.content_desc:
            return self.text
        return " ".join(part for part in (self.text, self.content_desc) if part).strip()


@dataclass(frozen=True)
class AndroidMetricResult:
    metrics: dict[str, int] = field(default_factory=dict)
    audio_name: str = ""
    like_count_private: bool | None = None
    status: str = "collected"
    error: str = ""

    def display_value(self, field_name: str) -> str:
        if field_name == "audio_name":
            return self.audio_name or "unavailable"
        value = self.metrics.get(field_name)
        return f"{value:,}" if value is not None else "unavailable"


class AndroidUiDriver(Protocol):
    def ensure_ready(self) -> None: ...

    def open_instagram_url(self, url: str) -> None: ...

    def open_instagram_search(self, query: str) -> None: ...

    def dump_ui(self) -> str: ...

    def tap_bounds(self, bounds: str) -> bool: ...

    def press_back(self) -> None: ...

    def scroll_down(self) -> None: ...


def default_adb_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    return Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe"


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

    def __init__(self, adb_path: Path, device_id: str | None = None) -> None:
        self.adb_path = Path(adb_path)
        self.device_id = device_id or self._select_online_device()
        self._cached_screen_size: tuple[int, int] | None = None

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = [str(self.adb_path), "-s", self.device_id, *arguments]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_ADB_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            rendered = " ".join(arguments[:3])
            raise AndroidMetricsError(
                f"ADB command timed out after {_ADB_COMMAND_TIMEOUT_SECONDS:g}s: {rendered}"
            ) from error
        if result.returncode:
            raise AndroidMetricsError(_command_error(result))
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
        package = self._run("shell", "pm", "path", INSTAGRAM_PACKAGE)
        if not package.stdout.strip():
            raise AndroidMetricsError("Instagram is not installed on the selected Android device.")

    def open_instagram_url(self, url: str) -> None:
        # Without the package constraint an emulator can send the https URL
        # to Chrome/the system resolver.  A successful ``am start`` then
        # looks like an Instagram failure because UIAutomator never sees the
        # Reel.  Keep the normal VIEW intent but force Instagram to handle it.
        self._run(
            "shell",
            "am",
            "start",
            "-W",
            "-p",
            INSTAGRAM_PACKAGE,
            "-a",
            "android.intent.action.VIEW",
            "-d",
            url,
        )

    def dump_ui(self) -> str:
        command = "uiautomator dump --compressed /sdcard/hybrid_window.xml >/dev/null && cat /sdcard/hybrid_window.xml"
        return self._run("exec-out", "sh", "-c", command).stdout

    def tap_bounds(self, bounds: str) -> bool:
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if match is None:
            return False
        left, top, right, bottom = (int(value) for value in match.groups())
        self._run("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
        return True

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
    text = str(value or "").strip()
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
    visible = node.visible_text.casefold()
    for name, markers in _METRIC_TEXT_MARKERS:
        if any(marker in visible for marker in markers):
            return name
    return ""


def extract_visible_metrics(xml: str) -> dict[str, int]:
    metrics: dict[str, int] = {}
    for node in parse_ui_xml(xml):
        name = _metric_key(node)
        value = _node_count(node) if name else None
        if name and value is not None:
            metrics[name] = value
    return metrics


def extract_audio_name(xml: str) -> str:
    nodes = parse_ui_xml(xml)
    for node in nodes:
        resource_id = node.resource_id.casefold()
        if any(marker in resource_id for marker in ("audio_name", "audio_title", "music_title", "sound_title")):
            if node.visible_text and node.visible_text.casefold() not in {"audio", "오디오"}:
                return node.visible_text
    for index, node in enumerate(nodes):
        if "author_username" not in node.resource_id.casefold():
            continue
        for successor in nodes[index + 1:index + 9]:
            if "caption" in successor.resource_id.casefold():
                break
            candidate = successor.visible_text.strip()
            if not candidate or candidate.casefold().startswith(("follow", "팔로우")):
                continue
            if _metric_key(successor) or _is_likes_trigger(successor):
                continue
            if candidate.casefold() not in {"audio", "오디오"}:
                return candidate
    return ""


def is_likes_and_plays_panel(xml: str) -> bool:
    nodes = parse_ui_xml(xml)
    if any(
        marker in node.resource_id.casefold()
        for node in nodes
        for marker in ("like_count_text", "video_view_count_text")
    ):
        return True
    text = " ".join(node.visible_text.casefold() for node in nodes)
    return "likes and plays" in text or "좋아요 및 재생" in text or "좋아요와 재생" in text


def _likes_detail_ready(xml: str) -> bool:
    metrics = extract_visible_metrics(xml)
    return is_likes_and_plays_panel(xml) and (
        "view_count" in metrics or "like_count" in metrics or bool(_LIKE_PRIVATE.search(xml))
    )


def _first_matching_node(nodes: Sequence[UiNode], predicate: object) -> UiNode | None:
    matcher = predicate
    return next((node for node in nodes if callable(matcher) and matcher(node)), None)


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
    if re.search(r"couldn.?t refresh|try again later|잠시 후 다시|새로고침", text, re.I):
        return "Instagram displayed a loading or temporary-error screen."
    return "Instagram did not render a recognizable Reel surface before the timeout."


def _is_likes_trigger(node: UiNode) -> bool:
    visible = node.visible_text.casefold()
    return any(label in visible for label in _LIKE_DETAIL_LABELS)


def _is_comment_trigger(node: UiNode) -> bool:
    resource_id = node.resource_id.casefold()
    if any(marker in resource_id for marker in _COMMENT_RESOURCE_MARKERS):
        return True
    visible = node.visible_text.casefold()
    return any(label in visible for label in _COMMENT_DETAIL_LABELS)


def _comment_sheet_state(xml: str) -> str:
    text = " ".join(node.visible_text for node in parse_ui_xml(xml))
    if _NO_COMMENTS.search(text):
        return "empty"
    if _COMMENTS_DISABLED.search(text):
        return "disabled"
    return ""


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
    ) -> None:
        self._adb_path = Path(adb_path) if adb_path else default_adb_path()
        self._device_id = device_id or None
        self._delay = max(0.1, float(ui_delay_seconds))
        self._driver = driver
        self._preflight_error = ""

    @property
    def driver(self) -> AndroidUiDriver:
        if self._driver is None:
            self._driver = AdbAndroidUiDriver(self._adb_path, self._device_id)
        return self._driver

    def _ready(self) -> str:
        if self._preflight_error:
            return self._preflight_error
        try:
            self.driver.ensure_ready()
        except AndroidMetricsError as error:
            self._preflight_error = str(error)
        return self._preflight_error

    def _wait_for_surface(
        self,
        predicate: object,
        attempts: int = 4,
        timeout_seconds: float | None = None,
    ) -> str:
        if timeout_seconds is not None:
            attempts = max(attempts, int(timeout_seconds / self._delay) + 1)
        xml = ""
        for attempt in range(attempts):
            if attempt:
                time.sleep(self._delay)
            xml = self.driver.dump_ui()
            if callable(predicate) and predicate(xml):
                return xml
        return xml

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

    def _open_likes_and_plays(self, reel_xml: str) -> tuple[dict[str, int], bool | None]:
        node = _first_matching_node(parse_ui_xml(reel_xml), _is_likes_trigger)
        if node is None or not self.driver.tap_bounds(node.bounds):
            return {}, None
        panel_xml = self._wait_for_surface(_likes_detail_ready)
        if not is_likes_and_plays_panel(panel_xml):
            return {}, None
        try:
            return extract_visible_metrics(panel_xml), bool(_LIKE_PRIVATE.search(panel_xml))
        finally:
            self.driver.press_back()

    def _read_empty_comment_state(self, reel_xml: str) -> str:
        node = _first_matching_node(parse_ui_xml(reel_xml), _is_comment_trigger)
        if node is None or not self.driver.tap_bounds(node.bounds):
            return ""
        panel_xml = self._wait_for_surface(lambda xml: bool(_comment_sheet_state(xml)) or not _is_reel_surface(xml))
        try:
            return _comment_sheet_state(panel_xml)
        finally:
            self.driver.press_back()

    def enrich(self, reel_url: str) -> AndroidMetricResult:
        error = self._ready()
        if error:
            return AndroidMetricResult(status="unavailable", error=error)
        try:
            reel_xml = ""
            for launch_attempt, ready_timeout in enumerate(
                (_ANDROID_REEL_READY_TIMEOUT_SECONDS, _ANDROID_RETRY_READY_TIMEOUT_SECONDS),
                start=1,
            ):
                self.driver.open_instagram_url(reel_url)
                reel_xml = self._wait_for_surface(
                    _is_reel_surface,
                    timeout_seconds=ready_timeout,
                )
                if _is_reel_surface(reel_xml):
                    break
                if launch_attempt == 1:
                    time.sleep(self._delay)
            else:
                return AndroidMetricResult(status="unavailable", error=describe_android_surface(reel_xml))
            metrics = extract_visible_metrics(reel_xml)
            audio_name = extract_audio_name(reel_xml)
            if not metrics and not audio_name:
                # The author node can appear before Instagram finishes drawing
                # the metric rail. Retry that partial surface instead of
                # recording a false Android success with every field blank.
                reel_xml = self._wait_for_surface(
                    lambda candidate: bool(extract_visible_metrics(candidate)) or bool(extract_audio_name(candidate)),
                    attempts=4,
                )
                if not _is_reel_surface(reel_xml):
                    return AndroidMetricResult(status="unavailable", error="Instagram left the Reel surface before its metrics rendered.")
                metrics = extract_visible_metrics(reel_xml)
                audio_name = extract_audio_name(reel_xml)
            detail_metrics, like_private = self._open_likes_and_plays(reel_xml)
            metrics.update(detail_metrics)
            if "comment_count" not in metrics:
                comment_state = self._read_empty_comment_state(reel_xml)
                if comment_state == "empty":
                    metrics["comment_count"] = 0
            if not metrics and not audio_name and like_private is None:
                return AndroidMetricResult(
                    status="unavailable",
                    error="Instagram rendered the Reel author but exposed no readable metric or audio nodes after retries.",
                )
            return AndroidMetricResult(
                metrics={name: value for name, value in metrics.items() if name in METRIC_FIELDS},
                audio_name=audio_name,
                like_count_private=like_private,
            )
        except AndroidMetricsError as error:
            return AndroidMetricResult(status="unavailable", error=str(error))

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
    """Overlay only Android-owned values; browser identity/static fields stay intact."""
    merged = dict(record)
    for field_name in METRIC_FIELDS:
        if field_name in result.metrics:
            merged[field_name] = result.metrics[field_name]
    if result.audio_name:
        merged["audio_name"] = result.audio_name
    if result.like_count_private is True and "like_count" not in result.metrics:
        merged["like_count"] = "X"
    return merged


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
    from exporters.instagram_collector import write_xlsx_workbook

    xlsx_path = destination / "hashtags.xlsx"
    write_xlsx_workbook(
        xlsx_path,
        [("hashtags", [fields, *[[str(row.get(field_name, "") or "") for field_name in fields] for row in exported_rows]])],
    )
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
    rows = enricher.collect_related_hashtag_post_counts(hashtags)
    return rows, write_hashtag_post_counts(Path(data_dir), rows)


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
    rows = enricher.collect_related_hashtag_post_counts(
        hashtags,
        on_rows=on_rows,
        progress_index=progress_index,
        progress_total=progress_total,
    )
    return rows, summarize_related_hashtag_counts(hashtags, rows)
