"""
Instagram Reels Analyzer (CLI)

터미널에 Instagram Reel URL을 입력하면
1) yt-dlp로 영상을 메모리에 다운로드하고
2) Gemini API로 영상을 분석해서
3) 결과를 output/ 폴더에 JSON 파일로 저장합니다.

사용법:
    1. .env.example 을 .env 로 복사하고 GEMINI_API_KEY를 입력
    2. pip install -r requirements.txt
    3. python reel_analyzer.py
"""

import os
import sys
import json
import time
import tempfile
from datetime import datetime

import requests
import yt_dlp
from dotenv import load_dotenv

from google import genai
from google.genai import types


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

load_dotenv()  # .env 파일에서 환경변수 로드

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# Gemini에 inline(base64)으로 바로 보낼 수 있는 최대 용량(MB).
# 이보다 크면 자동으로 Files API(업로드 방식)로 전환합니다.
INLINE_SIZE_LIMIT_MB = 18

# 503(과부하)/429(rate limit) 등 일시적 오류가 났을 때 재시도할 횟수와 대기 시간(초)
MAX_RETRIES = 4
RETRY_BASE_DELAY_SEC = 5

# 장면(scene) 분석용 프레임 샘플링 속도(fps). 값을 올릴수록 장면을 더 촘촘하게 나눠서 보지만
# 토큰 사용량도 늘어납니다. 릴스는 대부분 짧으므로 4~5 정도가 적당합니다.
SCENE_ANALYSIS_FPS = 5

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def ensure_api_key():
    """API 키가 없으면 안내하고 프로그램을 종료한다."""
    if not GEMINI_API_KEY:
        print("=" * 60)
        print("[오류] GEMINI_API_KEY가 설정되어 있지 않습니다.")
        print(".env.example 파일을 복사해서 .env 파일을 만든 뒤,")
        print("GEMINI_API_KEY=본인의_API_키 형태로 값을 채워주세요.")
        print("API 키 발급: https://aistudio.google.com/apikey")
        print("=" * 60)
        sys.exit(1)


# ---------------------------------------------------------------------------
# 1. Instagram 영상 다운로드
# ---------------------------------------------------------------------------

def get_instagram_video(reel_url: str) -> dict:
    """
    Instagram Reel을 디스크에 저장하지 않고
    실제 영상 URL을 추출한 뒤 메모리에 올린다.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[ext=mp4]/best",
        # 비공개 계정/로그인 필요 시 아래 주석을 해제하고 브라우저를 지정하세요.
        # "cookiesfrombrowser": ("chrome", None, None, None),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(reel_url, download=False)

    video_url = info["url"]
    headers = info.get("http_headers", {})

    print(f"  - shortcode : {info.get('id')}")
    print(f"  - duration  : {info.get('duration')}초")

    response = requests.get(video_url, headers=headers, timeout=60)
    response.raise_for_status()

    return {
        "id": info.get("id"),
        "video_bytes": response.content,
        "mime_type": "video/mp4",
        "metadata": info,
    }


# ---------------------------------------------------------------------------
# 2. Gemini 분석
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """
이 Instagram Reel 영상을 분석하세요.

다음 정보를 반드시 JSON 형식으로 반환하세요.

{
    "summary": "영상 전체 내용 요약",

    "hook": {
        "description": "첫 3초에 사용된 Hook",
        "strength": "strong | medium | weak"
    },

    "camera": {
        "main_composition": "주요 카메라 구도",
        "angles": [],
        "movements": []
    },

    "editing": {
        "style": "영상 편집 스타일",
        "cut_speed": "fast | medium | slow",
        "transitions": []
    },

    "subtitle": {
        "exists": true,
        "position": "자막 위치",
        "style": "자막 스타일"
    },

    "subjects": {
        "people_count": 0,
        "main_subject": "주요 피사체"
    },

    "product": {
        "exists": false,
        "description": null
    },

    "scenes": [
        {
            "timestamp": "00:00-00:02",
            "duration_seconds": 2,
            "visual_description": "화면에 보이는 내용을 구체적으로 (인물의 동작, 표정, 배경, 소품 포함)",
            "camera": "이 장면의 카메라 구도/앵글 (예: 클로즈업, 풀샷, 로우앵글 등)",
            "camera_movement": "카메라 움직임 (예: 고정, 팬, 줌인, 핸드헬드 등. 없으면 '없음')",
            "on_screen_text": "화면에 표시된 자막/텍스트 그래픽 내용 (없으면 null)",
            "audio_or_dialogue": "이 구간의 대사, 나레이션, 배경음악 특징 (없으면 null)",
            "transition_in": "이전 장면에서 이 장면으로 넘어올 때 사용된 전환 기법 (예: 컷, 페이드, 줌 트랜지션, 없으면 '없음')",
            "emotional_tone": "이 장면이 전달하는 감정/분위기",
            "purpose": "이 장면이 영상 전체에서 하는 역할 (예: 시선 끌기, 문제 제시, 해결책 제시, 증거 제시, CTA 등)"
        }
    ],

    "content_type": "콘텐츠 유형",

    "marketing_analysis": {
        "strengths": [],
        "weaknesses": [],
        "notable_elements": []
    }
}

장면(scenes) 분석 시 반드시 지켜야 할 규칙:
1. 컷이 바뀌거나(화면 전환), 카메라 구도가 바뀌거나, 새로운 텍스트/자막이 등장하는 시점마다 반드시 새로운 scene 항목을 만드세요. 뭉뚱그려서 크게 나누지 마세요.
2. 각 scene의 길이는 원칙적으로 3초를 넘지 않도록 잘게 쪼개세요. 정적인 장면이라도 최소 2~3초 단위로는 구간을 나누세요.
3. 영상 전체 길이를 빠짐없이 처음부터 끝까지 scene으로 커버하세요. 중간에 구간이 비면 안 됩니다.
4. timestamp는 "MM:SS-MM:SS" 형식으로, 실제 영상 재생 시간 기준으로 최대한 정확하게 적으세요.
5. visual_description은 "사람이 말한다" 같은 뭉뚱그린 표현 대신, 무엇을 어떻게 하고 있는지 구체적으로 서술하세요.
"""


RETRYABLE_MARKERS = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded")


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc)
    return any(marker in msg for marker in RETRYABLE_MARKERS)


def _call_with_retry(fn, *args, **kwargs):
    """일시적 오류(503 과부하, 429 rate limit 등)가 나면 지수 백오프로 재시도한다."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if not _is_retryable(e) or attempt == MAX_RETRIES:
                raise
            delay = RETRY_BASE_DELAY_SEC * attempt
            print(f"  - [재시도 {attempt}/{MAX_RETRIES}] 모델이 혼잡한 것 같습니다. {delay}초 후 다시 시도합니다...")
            time.sleep(delay)
    raise last_exc


def _analyze_inline(client: genai.Client, video_bytes: bytes, mime_type: str) -> dict:
    """작은 영상: base64 inline으로 바로 전송."""
    video_part = types.Part(
        inline_data=types.Blob(data=video_bytes, mime_type=mime_type),
        video_metadata=types.VideoMetadata(fps=SCENE_ANALYSIS_FPS),
    )

    response = _call_with_retry(
        client.models.generate_content,
        model=GEMINI_MODEL,
        contents=types.Content(parts=[video_part, types.Part(text=ANALYSIS_PROMPT)]),
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(response.text)


def _analyze_via_files_api(client: genai.Client, video_bytes: bytes, mime_type: str) -> dict:
    """큰 영상: Files API로 업로드 후 처리 완료를 기다렸다가 분석."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

        print("  - 영상이 커서 Files API로 업로드합니다...")
        uploaded_file = client.files.upload(file=tmp_path)

        # 업로드된 파일이 서버에서 처리(ACTIVE)될 때까지 대기
        while uploaded_file.state.name == "PROCESSING":
            print("  - 파일 처리 중... (5초 후 재확인)")
            time.sleep(5)
            uploaded_file = client.files.get(name=uploaded_file.name)

        if uploaded_file.state.name == "FAILED":
            raise RuntimeError("Gemini Files API 파일 처리에 실패했습니다.")

        response = _call_with_retry(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=types.Content(
                parts=[
                    types.Part(
                        file_data=types.FileData(
                            file_uri=uploaded_file.uri,
                            mime_type=uploaded_file.mime_type,
                        ),
                        video_metadata=types.VideoMetadata(fps=SCENE_ANALYSIS_FPS),
                    ),
                    types.Part(text=ANALYSIS_PROMPT),
                ]
            ),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(response.text)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def analyze_video(client: genai.Client, video_bytes: bytes, mime_type: str = "video/mp4") -> dict:
    size_mb = len(video_bytes) / (1024 * 1024)
    print(f"  - 영상 용량 : {size_mb:.2f} MB")

    if size_mb > INLINE_SIZE_LIMIT_MB:
        return _analyze_via_files_api(client, video_bytes, mime_type)
    return _analyze_inline(client, video_bytes, mime_type)


# ---------------------------------------------------------------------------
# 3. 결과 저장
# ---------------------------------------------------------------------------

def save_result(shortcode: str, result: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{shortcode or 'reel'}_{timestamp}.json"
    filepath = os.path.join(OUTPUT_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return filepath


def print_summary(result: dict):
    print("\n" + "=" * 60)
    print("[분석 요약]")
    print("-" * 60)
    print(f"요약        : {result.get('summary')}")
    hook = result.get("hook", {})
    print(f"Hook        : {hook.get('description')} (강도: {hook.get('strength')})")
    print(f"콘텐츠 유형 : {result.get('content_type')}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# 4. 메인 루프
# ---------------------------------------------------------------------------

def process_one(client: genai.Client, reel_url: str):
    print(f"\n[처리 시작] {reel_url}")

    print("1) 영상 다운로드 중...")
    reel = get_instagram_video(reel_url)

    print("2) Gemini 분석 중...")
    result = analyze_video(client, reel["video_bytes"], reel["mime_type"])

    print("3) 결과 저장 중...")
    filepath = save_result(reel["id"], result)
    print(f"  - 저장 완료: {filepath}")

    print_summary(result)


def main():
    ensure_api_key()
    client = genai.Client(api_key=GEMINI_API_KEY)

    print("Instagram Reels Analyzer")
    print("Reel URL을 입력하세요. 종료하려면 'quit' 또는 'exit' 입력.\n")

    while True:
        try:
            reel_url = input("Reel URL > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break

        if not reel_url:
            continue
        if reel_url.lower() in ("quit", "exit", "q"):
            print("종료합니다.")
            break
        if "instagram.com" not in reel_url:
            print("[경고] Instagram URL이 아닌 것 같습니다. 다시 확인해주세요.\n")
            continue

        try:
            process_one(client, reel_url)
        except yt_dlp.utils.DownloadError as e:
            print(f"[오류] 영상을 가져오지 못했습니다: {e}")
            print("      비공개 계정이거나 삭제된 게시물일 수 있습니다.\n")
        except requests.exceptions.RequestException as e:
            print(f"[오류] 영상 다운로드 중 네트워크 오류: {e}\n")
        except Exception as e:
            print(f"[오류] 분석 중 예상치 못한 문제가 발생했습니다: {e}\n")


if __name__ == "__main__":
    main()
