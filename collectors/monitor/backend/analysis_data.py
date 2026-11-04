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


def read_analysis(path):
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
    metrics,users,_,metric_warnings=collected_metrics({record.get('reel_id') for record in records},path.parent)
    for record in reversed(apply_korean_overrides(records, data_root)):
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
        audience = [{"age": code(item.get("age_group")), "gender": code(item.get("gender")),
                     "evidence": text(item.get("evidence"), 700)} for item in objects(analysis.get("recommended_audience"), 20)]
        raw_scenes = analysis.get("scene_details")
        scene_records = [item for item in raw_scenes if isinstance(item, dict)] if isinstance(raw_scenes, list) else []
        scenes = [{"start": seconds(item.get("start_second")), "end": seconds(item.get("end_second")),
                   "description": text(item.get("visual_description"), 700)} for item in scene_records[:100]]
        points = [{"point": text(item.get("point"), 600), "type": code(item.get("appeal_type")),
                   "evidence": text(item.get("evidence"), 900)} for item in objects(marketing.get("selling_points"), 30)]
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
                     "hook_strength": code(hook.get("strength")), "hook": text(hook.get("description"), 1200),
                     "body": text(analysis.get("body_structure"), 1800), "cut_speed": code(editing.get("cut_speed")),
                     "audience": audience, "ages": sorted({item["age"] for item in audience}) or ["unknown"],
                     "genders": sorted({item["gender"] for item in audience}) or ["unknown"],
                     "appeals": sorted({item["type"] for item in points}) or ["unknown"],
                     "points": points, "scenes": scenes, "scene_count": len(scene_records), "duration": duration if duration > 0 else None})
    warnings = list(metric_warnings)
    if not metrics:
        warnings.append("수집 지표가 없어 일부 지표 그래프가 비어 있을 수 있습니다.")
    if skipped:
        warnings.append(f"분석 또는 릴스 식별자가 없는 {skipped}건 제외")
    if duplicates:
        warnings.append(f"중복 {duplicates}건 제외 · 파일 뒤쪽 레코드 우선")
    return {"version": 1, "source": "collectors/data/collector/reel_analysis.json",
            "loaded_at": datetime.now(timezone.utc).isoformat(), "rows": rows, "warnings": warnings}
