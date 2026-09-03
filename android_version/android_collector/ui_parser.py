from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from .models import Metric, ObservedProfile, ObservedReel


@dataclass(frozen=True)
class UiNode:
    text: str
    content_desc: str
    resource_id: str
    bounds: str

    @property
    def visible_text(self) -> str:
        if self.text == self.content_desc:
            return self.text
        return " ".join(part for part in (self.text, self.content_desc) if part).strip()


_ACCESS_BLOCKS = (
    ("login_required", ("log in", "login", "로그인")),
    ("challenge_required", ("challenge", "보안 확인", "본인 인증")),
    ("captcha_required", ("captcha", "보안 문자")),
    ("rate_limited", ("try again later", "잠시 후 다시", "too many requests")),
)
_METRIC_IDS = (
    ("likes_and_plays_count", ("likes_and_plays", "metric_panel_count")),
    ("like_count", ("like_count", "likes_count")),
    ("view_count", ("video_view_count", "view_count", "play_count")),
    ("comment_count", ("comment_count", "comments_count")),
    ("share_count", ("share_count", "shares_count")),
    ("repost_count", ("repost_count", "reposts_count")),
    ("save_count", ("save_count", "saves_count")),
)
_METRIC_LABELS = (
    ("likes_and_plays_count", ("likes and plays", "좋아요 및 재생", "좋아요와 재생")),
    ("like_count", ("like number is", " likes", "좋아요")),
    ("view_count", ("view count", " views", "조회수")),
    ("comment_count", (" comments", "댓글")),
    ("share_count", ("reshare number is", "share count", "공유 수")),
    ("share_count", (" shares", "공유")),
    ("repost_count", (" repost", "리포스트")),
    ("save_count", (" saves", "저장")),
)
_LIKE_COUNT_PRIVATE_PATTERNS = (
    re.compile(r"only\s+.+?\s+can\s+see\s+the\s+total\s+number\s+of\s+likes\s+on\s+this\s+reel", re.I),
    re.compile(r"이\s*릴스의\s*총\s*좋아요\s*수는\s*.+?만\s*볼\s*수\s*있", re.I),
    re.compile(r"좋아요\s*수는\s*.+?만\s*볼\s*수\s*있", re.I),
)
_NO_COMMENTS_PATTERNS = (
    re.compile(r"\bno\s+comments\s+yet\b", re.I),
    re.compile(r"아직\s*댓글이\s*(없습니다|없어요)"),
)
_COMMENTS_DISABLED_PATTERNS = (
    re.compile(r"\bcomments?\s+(are|is)\s+(turned\s+off|disabled)\b", re.I),
    re.compile(r"댓글\s*(기능이\s*)?(꺼져\s*있|사용할\s*수\s*없)"),
)
_COMMENTS_LIMITED_PATTERNS = (
    re.compile(r"\bcomments?\s+on\s+this\s+(post|reel)\s+have\s+been\s+limited\b", re.I),
    re.compile(r"댓글\s*(기능이\s*)?제한"),
)
_AD_LABELS = {"ad", "광고"}
_ACCOUNT_BASED_IN_LABELS = ("account based in", "계정 기반 위치", "계정 위치")
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_ENGLISH_UPLOAD_DATE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(_MONTHS) + r")\s+([0-3]?\d)(?:,?\s+(\d{4}))?(?!\d)",
    re.IGNORECASE,
)
_KOREAN_UPLOAD_DATE = re.compile(r"(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일")


_COMPACT_COUNT = re.compile(r"(?P<number>\d+(?:[.,]\d+)?)\s*(?P<unit>[KMBkmb만천])")
_COMPACT_MULTIPLIERS = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "만": 10_000,
    "천": 1_000,
}


def _compact_number(number_text: str, unit: str) -> int | None:
    """Normalise a compact on-screen count without inventing precision.

    Instagram sometimes renders only ``10K`` or ``1.2M`` on a profile/Reel.
    In that situation the app has not revealed an exact underlying value, but
    retaining the display-derived integer is more useful than writing an
    unavailable cell.  The unmodified on-screen text remains in ``raw_text``.
    """
    normalized = number_text.replace(" ", "")
    if "," in normalized and "." not in normalized:
        # Instagram uses commas both for thousands and, in some locales, as a
        # compact-number decimal separator.  A single short final group in a
        # compact number is the latter (``1,5K``); otherwise it is grouping.
        comma_parts = normalized.split(",")
        normalized = (
            ".".join(comma_parts)
            if len(comma_parts) == 2 and len(comma_parts[1]) <= 2
            else "".join(comma_parts)
        )
    else:
        normalized = normalized.replace(",", "")
    try:
        parsed = Decimal(normalized)
    except InvalidOperation:
        return None
    value = parsed * _COMPACT_MULTIPLIERS[unit.casefold()]
    return int(value) if value >= 0 else None


def parse_exact_count(text: str) -> int | None:
    """Return an integer from a full or compact count rendered by the app."""
    candidate = re.sub(r"[\s,]", "", str(text).strip())
    if re.fullmatch(r"\d+", candidate):
        return int(candidate)
    compact = _COMPACT_COUNT.fullmatch(str(text).strip())
    return _compact_number(compact.group("number"), compact.group("unit")) if compact else None


def parse_ui_xml(xml: str) -> list[UiNode]:
    """Extract the visible text-bearing UIAutomator nodes in screen order."""
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
        )
        for element in root.iter("node")
    ]


def detect_access_block(xml: str) -> str | None:
    text = " ".join(node.visible_text.casefold() for node in parse_ui_xml(xml))
    for status, tokens in _ACCESS_BLOCKS:
        if any(token in text for token in tokens):
            return status
    return None


def build_fingerprint(username: str, caption: str, audio_name: str) -> str:
    payload = "\x1f".join(value.strip().casefold() for value in (username, caption, audio_name))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metric_key_from_node(node: UiNode) -> str | None:
    resource_id = node.resource_id.casefold()
    for key, markers in _METRIC_IDS:
        if any(marker in resource_id for marker in markers):
            return key
    visible = node.visible_text.casefold()
    for key, labels in _METRIC_LABELS:
        if any(label in visible for label in labels):
            return key
    return None


def _node_value(node: UiNode) -> tuple[int | None, str]:
    if parse_exact_count(node.text) is not None:
        return parse_exact_count(node.text), node.text
    for count_match in re.finditer(
        r"(?<![\d,])\d+(?:[,\s]\d+)*(?:\.\d+)?\s*(?:[KMBkmb만천])?",
        node.visible_text,
    ):
        raw = count_match.group(0).strip()
        value = parse_exact_count(raw)
        if value is not None:
            return value, node.visible_text
    return None, node.visible_text


def extract_metrics(nodes: list[UiNode]) -> tuple[dict[str, Metric], dict[str, str]]:
    metrics: dict[str, Metric] = {}
    visible_metrics: dict[str, str] = {}
    for index, node in enumerate(nodes):
        key = _metric_key_from_node(node)
        if key is None:
            continue
        value, raw = _node_value(node)
        if key == "likes_and_plays_count" and value is None:
            for successor in nodes[index + 1:index + 4]:
                candidate, candidate_raw = _node_value(successor)
                if candidate is not None:
                    value, raw = candidate, candidate_raw
                    break
        label = "Likes and plays" if key == "likes_and_plays_count" else key.removesuffix("_count")
        metrics[key] = Metric(label=label, value=value, raw_text=raw)
        visible_metrics[label] = raw
    return metrics, visible_metrics


def _like_count_is_private(nodes: list[UiNode], metrics: dict[str, Metric]) -> bool | None:
    visible = " ".join(node.visible_text for node in nodes)
    if any(pattern.search(visible) for pattern in _LIKE_COUNT_PRIVATE_PATTERNS):
        return True
    # A compact count on the Reel itself is not a privacy setting.  Instagram
    # only exposes that setting in the Likes and plays sheet, so avoid claiming
    # that likes are public until that sheet has actually been read.
    if any("like_count_text" in node.resource_id.casefold() for node in nodes):
        return False
    return None


def is_likes_and_plays_panel(xml: str) -> bool:
    """Return whether the current surface is Instagram's read-only metric sheet."""
    for node in parse_ui_xml(xml):
        resource_id = node.resource_id.casefold()
        if "like_count_text" in resource_id or "video_view_count_text" in resource_id:
            return True
        if node.visible_text.casefold() in {
            "likes and plays",
            "좋아요 및 재생",
            "좋아요와 재생",
        }:
            return True
    return False


def comment_count_status(xml: str) -> str:
    """Classify a comment sheet without treating an empty thread as missing data."""
    visible = " ".join(node.visible_text for node in parse_ui_xml(xml))
    if any(pattern.search(visible) for pattern in _NO_COMMENTS_PATTERNS):
        return "empty"
    if any(pattern.search(visible) for pattern in _COMMENTS_DISABLED_PATTERNS):
        return "disabled"
    if any(pattern.search(visible) for pattern in _COMMENTS_LIMITED_PATTERNS):
        return "limited"
    return ""


def is_comments_panel(xml: str) -> bool:
    """Recognise comment-sheet states that need a safe back-navigation."""
    status = comment_count_status(xml)
    if status:
        return True
    visible = " ".join(node.visible_text.casefold() for node in parse_ui_xml(xml))
    return "start the conversation" in visible or "댓글을 시작" in visible


def is_profile_screen(xml: str) -> bool:
    """Return whether Instagram currently shows a creator profile."""
    return any("profile_header" in node.resource_id.casefold() for node in parse_ui_xml(xml))


def _has_visible_ad_label(nodes: list[UiNode]) -> bool:
    """Recognise Instagram's standalone lower-right Ad/광고 disclosure label."""
    for node in nodes:
        labels = {node.text.strip().casefold(), node.content_desc.strip().casefold()}
        if labels & _AD_LABELS:
            return True
    return False


def has_visible_label(xml: str, labels: tuple[str, ...]) -> bool:
    wanted = {label.casefold() for label in labels}
    return any(node.visible_text.strip().casefold() in wanted for node in parse_ui_xml(xml))


def parse_account_country(xml: str) -> str:
    """Read the public country shown on Instagram's About this account page."""
    nodes = parse_ui_xml(xml)
    labels = tuple(label.casefold() for label in _ACCOUNT_BASED_IN_LABELS)
    for index, node in enumerate(nodes):
        visible = node.visible_text.strip()
        folded = visible.casefold()
        if not any(label in folded for label in labels):
            continue
        for label in labels:
            if label in folded:
                remainder = visible[folded.find(label) + len(label):].strip(" :\n")
                if remainder:
                    return remainder
        for successor in nodes[index + 1:index + 5]:
            candidate = successor.text.strip()
            if candidate and candidate.casefold() not in labels:
                return candidate
    return ""


def parse_uploaded_at(xml: str, *, collected_at: str) -> str:
    """Return the date Instagram shows in a Reel caption sheet.

    Instagram omits the year for a post from the current year.  The caller
    supplies its collection timestamp so that an on-screen value such as
    ``April 28`` is faithfully normalised to ``2026-04-28`` when collected in
    2026, while ``April 28, 2024`` keeps its explicit year.  No time-of-day is
    inferred because the app does not expose it in this surface.
    """
    try:
        collection_time = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        collection_time = datetime.now(timezone.utc)
    collection_year = collection_time.astimezone(timezone.utc).year
    for node in parse_ui_xml(xml):
        # Instagram renders the posting date as its own text node at the foot
        # of the caption sheet.  Requiring the complete node prevents wording
        # inside a creator's caption (for example “launches April 28”) from
        # being mistaken for the posting date.
        visible = node.visible_text.strip()
        english = _ENGLISH_UPLOAD_DATE.fullmatch(visible)
        if english:
            month = _MONTHS[english.group(1).casefold()]
            day = int(english.group(2))
            year = int(english.group(3) or collection_year)
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                continue
        korean = _KOREAN_UPLOAD_DATE.fullmatch(visible)
        if korean:
            year = int(korean.group(1) or collection_year)
            month = int(korean.group(2))
            day = int(korean.group(3))
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                continue
    return ""


def _first_node_text(nodes: list[UiNode], marker: str) -> str:
    marker = marker.casefold()
    for node in nodes:
        if marker in node.resource_id.casefold() and node.visible_text:
            return node.visible_text
    return ""


def _audio_name(nodes: list[UiNode]) -> str:
    """Read the audio label from explicit fields or the author-info sibling.

    Instagram's current Reel layout frequently puts the track name in a
    resource-less node immediately after ``clips_author_username``.  The
    album-art button itself is labelled only ``Audio``, so treating that
    button as the title loses the artist and track name.
    """
    for marker in ("audio_name", "audio_title", "music_title", "sound_title"):
        value = _first_node_text(nodes, marker)
        if value and value.casefold() not in {"audio", "오디오"}:
            return value
    for index, node in enumerate(nodes):
        if "author_username" not in node.resource_id.casefold():
            continue
        for successor in nodes[index + 1:index + 9]:
            resource_id = successor.resource_id.casefold()
            if "caption" in resource_id:
                break
            candidate = successor.visible_text
            if not candidate:
                continue
            if candidate.casefold().startswith(("follow", "팔로우")):
                continue
            if "profile picture" in candidate.casefold():
                continue
            return candidate
    return ""


def _location_name(nodes: list[UiNode]) -> str:
    """Read a location only when the Android Reel surface exposes one."""
    for marker in ("location_name", "location_text", "location_label", "clips_location"):
        value = _first_node_text(nodes, marker)
        if value:
            return value
    for node in nodes:
        match = re.fullmatch(r"(?:location|위치)\s*[:：]\s*(.+)", node.visible_text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _caption(nodes: list[UiNode]) -> str:
    direct = _first_node_text(nodes, "caption")
    if direct:
        return direct
    for index, node in enumerate(nodes):
        if "caption" not in node.resource_id.casefold():
            continue
        for successor in nodes[index + 1:index + 8]:
            if successor.visible_text:
                return successor.visible_text
    return ""


def _username(nodes: list[UiNode]) -> str:
    for node in nodes:
        if "username" in node.resource_id.casefold() and node.text:
            return node.text.lstrip("@")
    for node in nodes:
        match = re.match(r"@?([A-Za-z0-9._]{1,30})(?:,?\s+(?:profile|프로필))", node.visible_text, re.I)
        if match:
            return match.group(1)
    return ""


def _profile_metric(nodes: list[UiNode], *resource_markers: str) -> int | None:
    for node in nodes:
        resource_id = node.resource_id.casefold()
        if not any(marker in resource_id for marker in resource_markers):
            continue
        value, _ = _node_value(node)
        if value is not None:
            return value
    return None


def _profile_text(nodes: list[UiNode], *resource_markers: str) -> str:
    for node in nodes:
        resource_id = node.resource_id.casefold()
        if any(marker in resource_id for marker in resource_markers) and node.visible_text:
            return node.visible_text
    return ""


def _profile_biography(nodes: list[UiNode]) -> str:
    """Read the Compose-based bio container used by current Android profiles."""
    direct = _profile_text(nodes, "profile_header_bio", "profile_bio")
    if direct:
        return direct
    for index, node in enumerate(nodes):
        if "profile_user_info_compose_view" not in node.resource_id.casefold():
            continue
        for successor in nodes[index + 1:index + 32]:
            resource_id = successor.resource_id.casefold()
            if "profile_links" in resource_id or "profile_action_buttons" in resource_id:
                break
            candidate = successor.text.strip()
            if not candidate or candidate.casefold() in {"see translation", "번역 보기"}:
                continue
            return re.sub(r"\s+", " ", candidate).strip()
    return ""


def parse_visible_profile(xml: str, *, expected_username: str = "") -> ObservedProfile:
    """Read public profile fields without inferring abbreviated counts.

    Resource ids vary slightly between Instagram builds, so the parser accepts
    the stable profile-header fragments and returns an empty value whenever the
    app does not render a particular field.
    """
    nodes = parse_ui_xml(xml)
    username = expected_username.strip().lstrip("@") or _profile_text(
        nodes,
        "action_bar_title",
        "profile_header_username",
    ).lstrip("@")
    return ObservedProfile(
        username=username,
        biography=_profile_biography(nodes),
        profile_category=_profile_text(
            nodes,
            "profile_header_category",
            "profile_category",
            "profile_header_business_category",
            "business_category",
        ),
        post_count=_profile_metric(nodes, "post_count", "posts_count"),
        following_count=_profile_metric(nodes, "following_value", "following_count", "following_stacked"),
        follower_count=_profile_metric(nodes, "followers_value", "follower_count", "followers_stacked"),
    )


def parse_visible_reel(
    xml: str,
    source_mode: str,
    source_query: str,
    reel_url: str,
    collected_at: str,
) -> ObservedReel:
    """Build a raw, evidence-friendly snapshot from the current app surface."""
    nodes = parse_ui_xml(xml)
    username = _username(nodes)
    caption = _caption(nodes)
    audio_name = _audio_name(nodes)
    metrics, visible_metrics = extract_metrics(nodes)
    comments_state = comment_count_status(xml)
    if comments_state == "empty" and metrics.get("comment_count", Metric("", None, "")).value is None:
        metrics["comment_count"] = Metric(label="comment", value=0, raw_text="No comments yet")
        visible_metrics["comment_count"] = "No comments yet"
    elif comments_state:
        # This is Android-only diagnostic evidence.  The python-compatible
        # public field remains blank because Instagram did not expose a count.
        metrics.pop("comment_count", None)
        visible_metrics["comment_count"] = f"comments_{comments_state}"
    return ObservedReel(
        source_mode=source_mode,
        source_query=source_query,
        reel_url=reel_url,
        reel_fingerprint=build_fingerprint(username, caption, audio_name),
        collected_at=collected_at,
        username=username,
        caption=caption,
        audio_name=audio_name,
        location_name=_location_name(nodes),
        is_ad=_has_visible_ad_label(nodes),
        metrics=metrics,
        visible_metrics=visible_metrics,
        like_count_is_private=_like_count_is_private(nodes, metrics),
    )
