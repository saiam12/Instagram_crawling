from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from pathlib import Path

from .frame_deduplicator import frames_are_similar


@dataclass
class FrameCandidate:
    timestamp: float
    reasons: set[str] = field(default_factory=set)


@dataclass
class ExtractedFrame:
    timestamp: float
    reasons: list[str]
    path: Path
    similar_to_previous: bool = False
    similar_to_timestamp: float | None = None


def build_candidates(
    duration_sec: float,
    scene_changes: list[float] | None = None,
    min_fps: float = 3.0,
) -> list[FrameCandidate]:
    if duration_sec <= 0:
        return []
    if min_fps <= 0:
        raise ValueError("Minimum FPS must be positive")
    candidates: dict[float, set[str]] = {}

    def add(timestamp: float, reason: str) -> None:
        timestamp = round(max(0.0, min(timestamp, duration_sec)), 3)
        candidates.setdefault(timestamp, set()).add(reason)

    for index in range(int(min(duration_sec, 3.0) / 0.5) + 1):
        add(index * 0.5, "opening")
        add(index * 0.5, "dense_sampling")
    add(0.0, "video_start")
    add(duration_sec, "video_end")
    for index in range(ceil(duration_sec * min_fps)):
        add(index / min_fps, "minimum_sampling")
    for timestamp in scene_changes or []:
        if 0 <= timestamp <= duration_sec:
            add(timestamp, "scene_change")
    return [FrameCandidate(timestamp, reasons) for timestamp, reasons in candidates.items()]


def limit_candidates(candidates: list[FrameCandidate], max_frames: int | None = None) -> list[FrameCandidate]:
    """Apply priority, then return chronological order for the vision model."""
    if max_frames is None:
        return sorted(candidates, key=lambda item: item.timestamp)
    priorities = ("opening", "scene_change", "video_start", "video_end", "minimum_sampling")
    ordered = sorted(
        candidates,
        key=lambda item: (min((priorities.index(reason) for reason in item.reasons if reason in priorities), default=len(priorities)), item.timestamp),
    )
    return sorted(ordered[:max_frames], key=lambda item: item.timestamp)


def extract_selected_frames(
    video_path: Path,
    candidates: list[FrameCandidate],
    output_dir: Path,
    max_frames: int | None = None,
) -> list[ExtractedFrame]:
    """Save every sampled frame, marking ordinary frames similar to the prior analysis frame."""
    import cv2

    output_dir.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError("Unable to open video for frame extraction")
    frames: list[ExtractedFrame] = []
    previous_analysis_frame = None
    previous_analysis_timestamp: float | None = None
    important_reasons = {"opening", "scene_change", "video_start", "video_end"}
    try:
        for index, candidate in enumerate(limit_candidates(candidates, max_frames)):
            capture.set(cv2.CAP_PROP_POS_MSEC, candidate.timestamp * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
            similar = previous_analysis_frame is not None and frames_are_similar(frame, previous_analysis_frame)
            is_important = bool(candidate.reasons & important_reasons)
            is_similar = similar and not is_important
            labels = "_".join(sorted(candidate.reasons | ({"similar"} if is_similar else set())))
            path = output_dir / f"{index:03d}_{candidate.timestamp:.2f}s_{labels}.jpg"
            if not cv2.imwrite(str(path), frame):
                raise RuntimeError("Failed to create frame image")
            frames.append(ExtractedFrame(
                candidate.timestamp,
                sorted(candidate.reasons),
                path,
                similar_to_previous=is_similar,
                similar_to_timestamp=previous_analysis_timestamp if is_similar else None,
            ))
            if not is_similar:
                previous_analysis_frame = frame
                previous_analysis_timestamp = candidate.timestamp
    finally:
        capture.release()
    return frames


def frames_for_analysis(frames: list[ExtractedFrame]) -> list[ExtractedFrame]:
    return [frame for frame in frames if not frame.similar_to_previous]
