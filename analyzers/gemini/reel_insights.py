"""Build incremental Gemini 3.8 insights and an HTML report from Reel analyses."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal

from google.genai import types
from pydantic import BaseModel, Field

from pool import create_pool
from reel_analyzer import _call_with_pool


MODEL = "gemini-3.6-flash"
DEFAULT_BATCH_SIZE = 10
HOOK_TYPES = ("텍스트 훅", "동작 훅", "비포·애프터", "상품 선노출", "상황·공감", "호기심·반전", "기타")
SCENE_TYPES = ("거울 셀카", "전신 착장", "제품 클로즈업", "플랫레이", "토킹헤드", "POV", "텍스트 중심", "기타")
MOVEMENT_TYPES = ("정적 포즈", "걷기", "손동작", "착장 전환", "카메라 이동", "제품 조작", "기타")


class VideoInsight(BaseModel):
    reel_id: str
    title: str = Field(description="영상의 핵심 특징을 한 문장으로 요약")
    features: List[str] = Field(description="관찰 근거가 있는 2~4개의 짧은 특징")
    hook_type: Literal["텍스트 훅", "동작 훅", "비포·애프터", "상품 선노출", "상황·공감", "호기심·반전", "기타"]
    scene_patterns: List[Literal["거울 셀카", "전신 착장", "제품 클로즈업", "플랫레이", "토킹헤드", "POV", "텍스트 중심", "기타"]]
    movement_patterns: List[Literal["정적 포즈", "걷기", "손동작", "착장 전환", "카메라 이동", "제품 조작", "기타"]]


class InsightBatch(BaseModel):
    videos: List[VideoInsight]
    common_scene_patterns: List[str]
    common_movement_patterns: List[str]
    common_hook_patterns: List[str]
    summary: str


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return default


def _write_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def compact_record(record: dict) -> dict | None:
    analysis = record.get("analysis") if isinstance(record.get("analysis"), dict) else record
    reel_id = str(record.get("reel_id") or analysis.get("shortcode") or "").strip()
    if not reel_id:
        return None
    scenes = []
    for scene in analysis.get("scene_details", []):
        if not isinstance(scene, dict):
            continue
        scenes.append({
            key: scene.get(key)
            for key in ("section", "layout_type", "camera_movement", "transition_in", "purpose", "visual_description")
        })
    return {
        "reel_id": reel_id,
        "summary": analysis.get("summary"),
        "hook": analysis.get("hook"),
        "body_structure": analysis.get("body_structure"),
        "camera": analysis.get("camera"),
        "editing": analysis.get("editing"),
        "scenes": scenes,
    }


def record_fingerprint(record: dict) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_prompt(records: list[dict]) -> str:
    return f"""
아래는 Reel별 1차 영상 분석 결과입니다. 각 영상의 특징을 정규화하고 scene, movement, hook의 공통점을 찾으세요.
원본 분석에 없는 성과, 인물 신원, 브랜드, 시청자 반응은 추정하지 마세요.
모든 reel_id를 한 번씩 그대로 반환하고, features는 영상별로 2~4개만 작성하세요.
hook_type은 {list(HOOK_TYPES)} 중 하나만 선택하세요.
scene_patterns는 {list(SCENE_TYPES)} 중 해당하는 값만, movement_patterns는 {list(MOVEMENT_TYPES)} 중 해당하는 값만 중복 없이 선택하세요.
common_* 필드는 여러 영상에서 반복되는 패턴만 빈도가 높은 순으로 적으세요.

입력 JSON:
{json.dumps(records, ensure_ascii=False, separators=(",", ":"))}
""".strip()


def analyze_batch(records: list[dict], model: str = MODEL) -> InsightBatch:
    os.environ["GEMINI_POOL_MODE"] = "local" if model == "gemini-3.8-flash" else "sheets"
    os.environ["GEMINI_MODELS"] = model
    pool = create_pool()

    def call(client, selected_model):
        return client.models.generate_content(
            model=selected_model,
            contents=build_prompt(records),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=InsightBatch,
            ),
        )

    response = _call_with_pool(pool, call, max_attempts=2)
    return InsightBatch.model_validate_json(response.text)


def distribution(videos: list[dict], field: str) -> list[dict]:
    counts: Counter[str] = Counter()
    for video in videos:
        value = video.get(field)
        values = value if isinstance(value, list) else [value]
        counts.update({str(item) for item in values if item})
    total = len(videos)
    return [
        {"label": label, "count": count, "percentage": round(count * 100 / total, 1) if total else 0.0}
        for label, count in counts.most_common()
    ]


def build_output(videos: list[dict], batch: InsightBatch, model: str) -> dict:
    distributions = {
        "hook": distribution(videos, "hook_type"),
        "scene": distribution(videos, "scene_patterns"),
        "movement": distribution(videos, "movement_patterns"),
    }
    return {
        "model": model,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "video_count": len(videos),
        "commonalities": {
            key: [item["label"] for item in values[:3]]
            for key, values in distributions.items()
        },
        "latest_batch_analysis": {
            "summary": batch.summary,
            "scene": batch.common_scene_patterns,
            "movement": batch.common_movement_patterns,
            "hook": batch.common_hook_patterns,
        },
        "distributions": distributions,
        "videos": videos,
    }


def _bars(items: list[dict]) -> str:
    return "".join(
        '<div class="bar-row"><span>' + html.escape(item["label"]) + '</span>'
        f'<div class="track"><i style="width:{item["percentage"]}%"></i></div>'
        f'<b>{item["count"]} ({item["percentage"]:.1f}%)</b></div>'
        for item in items
    ) or '<p class="empty">집계할 데이터가 없습니다.</p>'


def write_html_report(payload: dict, path: Path) -> None:
    sections = "".join(
        f'<section><h2>{title}</h2>{_bars(payload["distributions"][key])}</section>'
        for key, title in (("hook", "Hook 비율"), ("scene", "Scene 공통 패턴"), ("movement", "Movement 공통 패턴"))
    )
    cards = "".join(
        '<article><h3>' + html.escape(video["reel_id"]) + '</h3><p>' + html.escape(video["title"]) + '</p>'
        '<p><strong>Hook:</strong> ' + html.escape(video["hook_type"]) + '</p>'
        '<p><strong>Scene:</strong> ' + html.escape(", ".join(video["scene_patterns"])) + '</p>'
        '<p><strong>Movement:</strong> ' + html.escape(", ".join(video["movement_patterns"])) + '</p>'
        '<div class="tags">' + "".join(f'<span>{html.escape(tag)}</span>' for tag in video["features"]) + '</div></article>'
        for video in payload["videos"]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reel 특징 분석</title><style>
:root{{--bg:#f5f7fb;--card:#fff;--text:#172033;--muted:#667085;--track:#e7ecf3;--accent:#5965e8}}*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:32px 20px}}
h1{{margin-bottom:4px}}.lead{{color:var(--muted);margin-top:0}}section,article{{background:var(--card);border-radius:14px;padding:20px;box-shadow:0 2px 12px #1720330d}}
section{{margin:18px 0}}.bar-row{{display:grid;grid-template-columns:140px 1fr 110px;gap:12px;align-items:center;margin:12px 0}}.track{{height:14px;background:var(--track);border-radius:99px;overflow:hidden}}.track i{{display:block;height:100%;background:var(--accent);border-radius:99px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}article h3{{margin-top:0}}.tags{{display:flex;flex-wrap:wrap;gap:6px}}.tags span{{background:#eef0ff;color:#3942a0;border-radius:99px;padding:4px 9px}}@media(max-width:640px){{.bar-row{{grid-template-columns:1fr}}}}
</style><main><h1>Reel 특징 분석</h1><p class="lead">{payload["video_count"]}개 영상 · {html.escape(payload["model"])} · {html.escape(payload["updated_at"])}</p>
<p>{html.escape(payload["latest_batch_analysis"]["summary"])}</p>{sections}<h2>영상별 특징</h2><div class="cards">{cards}</div></main></html>''', encoding="utf-8")


def run(input_path: Path, output_path: Path, report_path: Path, state_path: Path, batch_size: int, force: bool, model: str) -> str:
    source = _load_json(input_path, [])
    if not isinstance(source, list):
        raise ValueError(f"분석 입력은 JSON 배열이어야 합니다: {input_path}")
    compact = [item for record in source if isinstance(record, dict) and (item := compact_record(record))]
    existing = _load_json(output_path, {})
    existing_videos = existing.get("videos", []) if isinstance(existing, dict) else []
    by_id = {video.get("reel_id"): video for video in existing_videos if isinstance(video, dict) and video.get("reel_id")}
    pending = [record for record in compact if by_id.get(record["reel_id"], {}).get("source_fingerprint") != record_fingerprint(record)]
    if force and not pending:
        pending = compact
    limit = max(1, batch_size)
    if not force and len(pending) < limit:
        _write_json_atomic(state_path, {"status": "not_due", "pending": len(pending), "batch_size": batch_size, "model": model})
        return "not_due"
    pending = pending[:limit]

    try:
        batch = analyze_batch(pending, model)
        returned = {video.reel_id: video for video in batch.videos}
        missing = [record["reel_id"] for record in pending if record["reel_id"] not in returned]
        if missing:
            raise ValueError(f"Gemini가 {len(missing)}개 reel_id를 반환하지 않았습니다: {', '.join(missing[:5])}")
        for record in pending:
            video = returned[record["reel_id"]].model_dump()
            video["source_fingerprint"] = record_fingerprint(record)
            by_id[record["reel_id"]] = video
        videos = [by_id[record["reel_id"]] for record in compact if record["reel_id"] in by_id]
        payload = build_output(videos, batch, model)
        _write_json_atomic(output_path, payload)
        write_html_report(payload, report_path)
        _write_json_atomic(state_path, {"status": "succeeded", "processed": len(pending), "video_count": len(videos), "model": model, "updated_at": payload["updated_at"]})
        return "succeeded"
    except Exception as error:
        _write_json_atomic(state_path, {"status": "failed", "pending": len(pending), "model": model, "error": str(error), "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Create incremental Reel pattern insights with Gemini 3.8 Flash.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("FASHION_INSIGHTS_BATCH_SIZE", DEFAULT_BATCH_SIZE)))
    parser.add_argument("--model", default=os.getenv("FASHION_INSIGHTS_MODEL", MODEL))
    parser.add_argument("--force", action="store_true")
    options = parser.parse_args()
    status = run(options.input, options.output, options.report, options.state, options.batch_size, options.force, options.model)
    print(f"Reel insights: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
