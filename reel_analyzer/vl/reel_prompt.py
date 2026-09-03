from __future__ import annotations

import json
from typing import Any


def build_window_prompt(frame_timestamps: list[float], prior_state: dict[str, Any]) -> str:
    timeline = ", ".join(f"{timestamp:.2f}s" for timestamp in frame_timestamps)
    state = json.dumps(prior_state, ensure_ascii=False, separators=(",", ":"))
    return f"""These images are consecutive frames from one Instagram Reel at: {timeline}.
Previous compact observations: {state}

Update the observations using only visible evidence. The previous state may describe earlier, overlapping frames; preserve verified facts and do not duplicate events. Return JSON only with these keys: timeline (short timestamped events), content_facts, hook_facts, camera_subject_product_text, editing_marketing_viral, uncertainties. Keep the whole JSON under 250 words."""


def build_final_prompt(state: dict[str, Any]) -> str:
    evidence = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    return f"""Turn this compact, timestamped evidence from one Instagram Reel into the final analysis. Do not invent facts not supported by the evidence. Return JSON only, no Markdown.
Evidence: {evidence}

Return this shape, using empty values when unknown:
{{
  "content": {{"summary": "", "category": "", "format": "", "purpose": ""}},
  "hook": {{"type": "", "description": "", "strength": 0, "first_3_seconds": "", "first_product_appearance_time": null, "first_person_appearance_time": null}},
  "camera": {{"dominant_shot": "", "shot_types": [], "angles": [], "movement": "", "framing_style": "", "distance": ""}},
  "subject": {{"people_count": 0, "position": "", "gaze": "", "face_visibility": "", "body_visibility": ""}},
  "product": {{"visible": false, "type": "", "first_appearance_sec": null, "position": "", "prominence": "", "closeup_used": false}},
  "subtitle": {{"present": false, "position": "", "density": "", "style": "", "text_hook_present": false}},
  "editing": {{"pace": "", "scene_changes": 0, "transition_styles": [], "cut_frequency": "", "average_scene_duration_estimate": ""}},
  "visual_style": {{"lighting": "", "background": "", "color": "", "aesthetic": ""}},
  "marketing": {{"format": "", "cta": "", "techniques": [], "product_demonstration": false, "before_after": false, "testimonial": false, "tutorial": false, "storytelling": false}},
  "viral_features": []
}}"""
