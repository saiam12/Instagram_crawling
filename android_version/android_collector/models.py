from __future__ import annotations

from dataclasses import dataclass, field, replace


class CollectorError(RuntimeError):
    """Base class for collection failures that can be shown to the user."""


class AccessBlockedError(CollectorError):
    """Instagram requires a manual login, challenge, or rate-limit pause."""


class LayoutUnrecognisedError(CollectorError):
    """The visible Instagram layout does not expose a usable Reel identity."""


@dataclass(frozen=True)
class Metric:
    label: str
    value: int | None
    raw_text: str


@dataclass(frozen=True)
class EvidencePaths:
    xml_path: str = ""
    png_path: str = ""


@dataclass(frozen=True)
class ObservedProfile:
    """Public profile fields rendered by the Instagram Android app."""

    user_id: str = ""
    username: str = ""
    biography: str = ""
    profile_category: str = ""
    account_country: str = ""
    post_count: int | None = None
    following_count: int | None = None
    follower_count: int | None = None


@dataclass(frozen=True)
class ObservedReel:
    source_mode: str
    source_query: str
    reel_url: str
    reel_fingerprint: str
    collected_at: str
    username: str = ""
    caption: str = ""
    audio_name: str = ""
    location_name: str = ""
    # Instagram exposes the posting date only after opening the caption sheet.
    # It is stored as an ISO calendar date (YYYY-MM-DD), not a guessed time.
    uploaded_at: str = ""
    is_ad: bool = False
    profile: ObservedProfile = field(default_factory=ObservedProfile)
    metrics: dict[str, Metric] = field(default_factory=dict)
    visible_metrics: dict[str, str] = field(default_factory=dict)
    evidence_paths: EvidencePaths = field(default_factory=EvidencePaths)
    detail_evidence_paths: EvidencePaths = field(default_factory=EvidencePaths)
    like_count_is_private: bool | None = None
    status: str = "collected"

    def with_evidence(self, evidence_paths: EvidencePaths) -> "ObservedReel":
        return replace(self, evidence_paths=evidence_paths)

    def with_detail_evidence(self, evidence_paths: EvidencePaths) -> "ObservedReel":
        return replace(self, detail_evidence_paths=evidence_paths)

    def with_profile(self, profile: ObservedProfile) -> "ObservedReel":
        return replace(self, profile=profile)
