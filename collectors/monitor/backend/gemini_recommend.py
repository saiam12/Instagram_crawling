"""JSON stdin/stdout provider adapter, run in the existing Gemini Analyzer venv."""
from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

ANALYZER = Path(__file__).resolve().parents[3] / "analyzers"
def failure_result(error):
    code = {503: "gemini_unavailable", 429: "gemini_rate_limit", 401: "gemini_auth",
            403: "gemini_auth", 404: "gemini_model", 400: "gemini_request"}.get(getattr(error, "code", None))
    return {"ready": False, "error": "시트 풀 설정 또는 Gemini 응답을 확인하지 못했습니다.",
            "error_code": code or "generation_failed"}


def run(status=False):
    from dotenv import load_dotenv
    load_dotenv(ANALYZER / ".env", override=False)
    sys.path.insert(0, str(ANALYZER))
    from guide_videos import VideoPool, generate_videos
    from pool.local import GUIDE_AGENT

    enabled = os.getenv("GEMINI_GUIDE_ENABLED", "true").strip().lower() != "false"
    pool = VideoPool(models=[GUIDE_AGENT])
    ready = enabled and bool(pool.keys) and os.getenv("GEMINI_POOL_MODE", "sheets").strip().lower() == "sheets"
    if status or not ready:
        return {"ready": ready, "provider": "antigravity-sheets", "models": [GUIDE_AGENT],
                "message": "Antigravity에 가이드 영상 6개와 영상별 장면 구성·대본을 직접 병렬 요청합니다." if ready else
                "Analyzer .env의 공용 시트 풀·API 키·GEMINI_GUIDE_ENABLED 설정을 확인하세요."}
    payload = json.load(sys.stdin)
    try:
        guide = generate_videos({}, payload["brief"], payload["references"],
                                output_dir=ANALYZER / "output" / "guides")
    except Exception:
        guide = {"state": "failed", "kind": "videos", "videos": [], "completed": 0,
                 "message": "가이드 영상을 생성하지 못했습니다. 참고 사례 3개 이상·Antigravity 한도·영상 변환 환경을 확인하세요. 자동 재전송하지 않았습니다."}
    return {"provider": "antigravity-sheets", "model": GUIDE_AGENT, "guide": guide}


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if "--retrieve" in sys.argv:
                sys.path.insert(0, str(ANALYZER))
                from embedding_search import semantic_search
                payload = json.load(sys.stdin)
                result = semantic_search(payload["documents"], payload["brief"])
            elif "--guide-cache" in sys.argv:
                sys.path.insert(0, str(ANALYZER))
                from guide_diagram import cached_guide
                import re
                filename = json.load(sys.stdin).get("filename", "")
                if not re.fullmatch(r"[a-f0-9]{32}\.png", filename):
                    raise ValueError("invalid guide filename")
                result = {"guide": cached_guide(ANALYZER / "output" / "guides" / filename.replace(".png", ".response.json"))}
            else:
                result = run("--status" in sys.argv)
    except ImportError:
        result = {"ready": False, "error": "Analyzer 의존성이 없습니다. analyzers/requirements.txt를 설치하세요."}
    except Exception as error:
        result = failure_result(error)
    print(json.dumps(result, ensure_ascii=False))
