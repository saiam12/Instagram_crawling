"""Read-only, bounded projection of the configured Analyzer dataset for charts."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from visualization_data import metric_number
from classification import korean_value
from src.reels.korean_classification import normalize_account_country
from src.reels.korean_classification import apply_korean_overrides
from src.analysis_categories import normalize_content_type
from recommendations import collected_metrics, metric_projection, METRICS


def text(value, limit=1000):
    return value[:limit] if isinstance(value, str) else ""


def code(value):
    return text(value, 80).strip() or "unknown"


def objects(value, limit=100):
    return [item for item in value[:limit] if isinstance(item, dict)] if isinstance(value, list) else []


def seconds(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def fields(value, labels):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, label in labels.items():
        raw = value.get(key)
        if isinstance(raw, list):
            raw = ", ".join(filter(None, (text(item, 500) for item in raw[:30])))
        if isinstance(raw, bool):
            raw = "있음" if raw else "없음"
        if display := text(raw, 2000).strip():
            result[label] = display
    return result


def visual_features(analysis):
    subjects = analysis.get("subjects")
    subjects = subjects if isinstance(subjects, dict) else {}
    people_count = subjects.get("people_count")
    audio = analysis.get("audio_analysis")
    audio = audio if isinstance(audio, dict) else {}
    groups = {
        "인물·캐릭터": {
            **fields(subjects, {"main_subject": "주요 피사체"}),
            **({"등장 인원": f"{people_count}명"} if type(people_count) is int and people_count >= 0 else {}),
        },
        "상품": fields(analysis.get("product"), {"exists": "화면 등장", "description": "상품 특징"}),
        "촬영": fields(analysis.get("camera"), {"main_composition": "주요 구도", "angles": "앵글", "movements": "카메라 움직임"}),
        "편집": fields(analysis.get("editing"), {"style": "편집 방식", "transitions": "전환 기법"}),
        "자막": fields(analysis.get("subtitle"), {"exists": "자막", "position": "위치", "style": "스타일"}),
        "오디오": {
            **fields(audio, {"speech_present": "음성", "language": "언어"}),
            **fields(audio.get("background_music"), {"exists": "배경음악", "mood": "분위기", "tempo": "템포", "vocals": "보컬"}),
        },
        "썸네일": fields(analysis.get("thumbnail_analysis"), {
            "source": "출처", "visual_description": "화면", "on_screen_text": "문구", "focal_point": "시선이 가는 대상",
            "composition": "구도", "selling_point": "소구점", "strengths": "강점", "weaknesses": "약점", "effectiveness": "전달력",
        }),
        "마케팅 분석": fields(analysis.get("marketing_analysis"), {
            "strengths": "강점", "weaknesses": "약점", "notable_elements": "특이 요소",
        }),
    }
    audio_events = []
    for key, labels in [
        ("transcript", {"speaker": "화자", "text": "대사"}),
        ("sound_events", {"type": "소리 유형", "description": "소리"}),
    ]:
        for item in objects(audio.get(key)):
            if info := fields(item, labels):
                audio_events.append({"start": seconds(item.get("start_second")), "end": seconds(item.get("end_second")), "info": info})
    audio_events.sort(key=lambda item: (item["start"] is None, item["start"] or 0))
    return {name: group for name, group in groups.items() if group}, audio_events


def scene_projection(item):
    return {
        "start": seconds(item.get("start_second")), "end": seconds(item.get("end_second")),
        "description": text(item.get("visual_description"), 1200),
        "features": fields(item, {
            "section": "구간", "layout_type": "화면 구성", "camera": "카메라 구도", "camera_movement": "움직임",
            "on_screen_text": "화면 문구", "audio_or_dialogue": "음성·소리",
            "transition_in": "들어오는 전환", "emotional_tone": "분위기", "purpose": "장면 역할",
        }),
        "camera_view": fields(item.get("camera_view"), {
            "viewpoint": "시점", "aim_target": "조준 대상", "confidence": "판단 신뢰도", "evidence": "카메라 시점 근거",
        }),
        "camera_frame": fields(item.get("camera_frame"), {"aspect": "촬영 화면 비율", "letterbox": "상하 검은 여백"}),
        "orientations": [fields(subject, {
            "subject_id": "인물", "body_direction": "몸통", "head_direction": "머리",
            "gaze_direction": "시선", "gaze_target": "시선 대상", "confidence": "판단 신뢰도", "evidence": "방향 근거",
        }) for subject in objects(item.get("subject_orientations"), 20)],
        "outfits": [fields(outfit, {"subject_id": "착용자", "clothing_items": "의상"})
                    for outfit in objects(item.get("worn_outfits"), 20)],
    }


def read_analysis(path, *, frozen_metrics=False):
    limit = 32 * 1024 * 1024
    try:
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("분석 파일은 32 MiB 이하여야 합니다.")
        data = json.loads(content.decode("utf-8-sig"))
    except OSError:
        raise ValueError("reel_analysis.json을 읽지 못했습니다. 지정 경로를 확인하세요.") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("분석 파일의 JSON 형식을 확인하세요.") from None
    if not isinstance(data, list) or len(data) > 10000:
        raise ValueError("분석 데이터는 10,000건 이하의 JSON 배열이어야 합니다.")
    rows, seen = [], set()
    records = [record for record in data if isinstance(record, dict)]
    skipped, duplicates = len(data) - len(records), 0
    data_root = path.parent / ".datasets" / "fashion"
    if not data_root.is_dir():
        data_root = path.parent
    if frozen_metrics:
        metrics, users, metric_warnings = {}, {}, []
    else:
        metrics, users, _, metric_warnings = collected_metrics({record.get('reel_id') for record in records}, path.parent)
        records = apply_korean_overrides(records, data_root)
    for record in reversed(records):
        if not isinstance(record, dict) or not isinstance(record.get("analysis"), dict) or not record["analysis"]:
            skipped += 1
            continue
        key = record.get("reel_id")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,80}", key):
            skipped += 1
            continue
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        collected=metrics.get(key,record)
        metric_info=metric_projection(record,collected,users)
        analysis = record["analysis"]
        hook = analysis.get("hook") if isinstance(analysis.get("hook"), dict) else {}
        editing = analysis.get("editing") if isinstance(analysis.get("editing"), dict) else {}
        marketing = analysis.get("marketing_analysis") if isinstance(analysis.get("marketing_analysis"), dict) else {}
        product = analysis.get("product") if isinstance(analysis.get("product"), dict) else {}
        exposure = product.get("first_exposure") if product.get("exists") is True else None
        exposure = exposure if isinstance(exposure, dict) else {}
        exposure_start = seconds(exposure.get("start_second"))
        exposure_evidence = text(exposure.get("visual_evidence"), 900).strip()
        features, audio_events = visual_features(analysis)
        audience = [{"age": code(item.get("age_group")), "gender": code(item.get("gender")),
                     "evidence": text(item.get("evidence"), 700)} for item in objects(analysis.get("recommended_audience"), 20)]
        raw_scenes = analysis.get("scene_details")
        scene_records = [item for item in raw_scenes if isinstance(item, dict)] if isinstance(raw_scenes, list) else []
        scenes = [scene_projection(item) for item in scene_records[:100]]
        points = [{"point": text(item.get("point"), 600), "type": code(item.get("appeal_type")),
                   "evidence": text(item.get("evidence"), 900), "start": seconds(item.get("start_second")),
                   "end": seconds(item.get("end_second")), "sources": fields(item, {
                       "spoken_evidence": "대사 근거", "visual_evidence": "화면 근거", "on_screen_text_evidence": "자막 근거",
                   })} for item in objects(marketing.get("selling_points"), 30)]
        raw_points = marketing.get("selling_points")
        appeal_starts = (seconds(point.get("start_second")) for point in raw_points if isinstance(point, dict)) if isinstance(raw_points, list) else ()
        first_appeal = min((start for start in appeal_starts if start is not None), default=None)
        duration = max((seconds(item.get("end_second")) or 0 for item in scene_records), default=0)
        rows.append({**{field:metric_info['metrics'].get(field) for field in METRICS},
                     "username":metric_info['username'],"url":f"https://www.instagram.com/reel/{key}/",
                     "collected_at":text(collected.get('collected_at'),80),
                     "video_duration_seconds":metric_number(collected.get('video_duration_seconds')) or (duration if duration>0 else None),
                     "duration_source":"collector" if metric_number(collected.get('video_duration_seconds')) else "analysis_scene_end",
                     "reel_id": key, "korean": korean_value(record), "korean_source": text(record.get("korean_source"), 80),
                     "account_country": normalize_account_country(text(record.get("account_country"), 100)), "summary": text(analysis.get("summary"), 1500),
                     "analyzed_at": text(record.get("analyzed_at"), 80), "content_type": normalize_content_type(code(analysis.get("content_type"))),
                     "content_type_raw": code(analysis.get("content_type")),
                     "hook_strength": code(hook.get("strength")), "hook_type": code(hook.get("hook_type")),
                     "hook": text(hook.get("description"), 1200),
                     "body": text(analysis.get("body_structure"), 1800), "cut_speed": code(editing.get("cut_speed")),
                     "product_visible": product.get("exists") if isinstance(product.get("exists"), bool) else None,
                     "first_product_exposure_seconds": exposure_start if exposure_evidence else None,
                     "first_product_exposure_evidence": exposure_evidence,
                     "first_appeal_seconds": first_appeal, "features": features, "audio_events": audio_events,
                     "audience": audience, "ages": sorted({item["age"] for item in audience}) or ["unknown"],
                     "genders": sorted({item["gender"] for item in audience}) or ["unknown"],
                     "appeals": sorted({item["type"] for item in points}) or ["unknown"],
                     "points": points, "scenes": scenes, "scene_count": len(scene_records), "duration": duration if duration > 0 else None})
    warnings = list(metric_warnings)
    if not metrics and not frozen_metrics:
        warnings.append("수집 지표가 없어 일부 지표 그래프가 비어 있을 수 있습니다.")
    if skipped:
        warnings.append(f"분석 또는 릴스 식별자가 없는 {skipped}건 제외")
    if duplicates:
        warnings.append(f"중복 {duplicates}건 제외 · 파일 뒤쪽 레코드 우선")
    return {"version": 1, "source": "collectors/data/collector/reel_analysis.json",
            "loaded_at": datetime.now(timezone.utc).isoformat(), "rows": rows, "warnings": warnings}
