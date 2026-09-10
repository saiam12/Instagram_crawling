from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from video.metadata import VideoMetadata
from video.frame_sampler import ExtractedFrame


DEFAULT_ANALYSIS: dict[str, Any] = {
    "content": {"summary": "", "category": "", "format": "", "purpose": ""},
    "hook": {"type": "", "description": "", "strength": 0, "first_3_seconds": "", "first_product_appearance_time": None, "first_person_appearance_time": None},
    "camera": {"dominant_shot": "", "shot_types": [], "angles": [], "movement": "", "framing_style": "", "distance": ""},
    "subject": {"people_count": 0, "position": "", "gaze": "", "face_visibility": "", "body_visibility": ""},
    "product": {"visible": False, "type": "", "first_appearance_sec": None, "position": "", "prominence": "", "closeup_used": False},
    "subtitle": {"present": False, "position": "", "density": "", "style": "", "text_hook_present": False},
    "editing": {"pace": "", "scene_changes": 0, "transition_styles": [], "cut_frequency": "", "average_scene_duration_estimate": ""},
    "visual_style": {"lighting": "", "background": "", "color": "", "aesthetic": ""},
    "marketing": {"format": "", "cta": "", "techniques": [], "product_demonstration": False, "before_after": False, "testimonial": False, "tutorial": False, "storytelling": False},
    "viral_features": [],
}

def _merge_defaults(defaults: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, default in defaults.items():
        value = values.get(key, default)
        merged[key] = _merge_defaults(default, value) if isinstance(default, dict) and isinstance(value, dict) else value
    return {**merged, **{key: value for key, value in values.items() if key not in defaults}}


def compose_result(
    *,
    shortcode: str | None,
    media_pk: str | None,
    reel_url: str | None,
    metadata: VideoMetadata,
    frames: list[ExtractedFrame],
    analyzed_frames: list[ExtractedFrame],
    model: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    result = _merge_defaults(DEFAULT_ANALYSIS, analysis)
    result.update({
        "shortcode": shortcode,
        "media_pk": media_pk,
        "reel_url": reel_url,
        "video_duration": round(metadata.duration_sec, 3),
        "video_width": metadata.width,
        "video_height": metadata.height,
        "fps": metadata.fps,
        "extracted_frame_count": len(frames),
        "analyzed_frame_count": len(analyzed_frames),
        "frame_timestamps": [frame.timestamp for frame in frames],
        "qwen_frame_timestamps": [frame.timestamp for frame in analyzed_frames],
        "analysis_model": model,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "frame_selection": [
            {
                "timestamp": frame.timestamp,
                "reason": frame.reasons,
                "analysis": "similar_to_previous" if frame.similar_to_previous else "qwen_analysis",
                "similar_to_timestamp": frame.similar_to_timestamp,
            }
            for frame in frames
        ],
    })
    result["video"] = {
        "duration_sec": round(metadata.duration_sec, 3),
        "frames_sampled": len(frames),
        "frames_analyzed": len(analyzed_frames),
    }
    return result
