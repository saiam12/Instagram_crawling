"""
Instagram Reels Analyzer (CLI)

Instagram Reel 영상을 분석하고 아래 정보를 JSON으로 저장합니다.
1) 훅(Hook)/바디(Body) 구조 판별 (예: 인물 훅 -> 상품 플랫레이 전환 구조 감지)
2) 씬별 상세 분석 (Pydantic 스키마로 구조화, layout_type 포함)
3) AI 영상 생성 모델에 바로 넣을 수 있는 영문 프롬프트(video_prompt_en) 자동 생성

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
import argparse
import threading
from queue import Queue
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
import yt_dlp
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google.genai import errors as genai_errors

from google import genai
from google.genai import types

from pool import GeminiKeyPool, KeyPoolExhaustedError, Combo, create_pool
from input_sources import is_instagram_reel_url, read_reel_urls_from_xlsx


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

load_dotenv()  # .env 파일에서 환경변수 로드

# Gemini에 inline(base64)으로 바로 보낼 수 있는 최대 용량(MB).
# 이보다 크면 자동으로 Files API(업로드 방식)로 전환합니다.
INLINE_SIZE_LIMIT_MB = 18

# 풀에서 쓸 수 있는 조합이 당장 없을 때(RPM 제한), 최대 몇 초까지 기다릴지
POOL_WAIT_MAX_SEC = 90

# 대화형 입력과 분석을 분리하되 프로젝트별 API 한도를 과도하게 밀어붙이지 않는다.
INTERACTIVE_WORKERS = 2

# 장면(scene) 분석용 프레임 샘플링 속도(fps).
SCENE_ANALYSIS_FPS = 5

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
# Batch callers can give each worker an isolated file.  The normal CLI keeps
# the historical output path and behavior.
OUTPUT_FILE = os.getenv(
    "GEMINI_OUTPUT_FILE",
    os.path.join(OUTPUT_DIR, "reel_analyses.json"),
)
OUTPUT_LOCK = threading.Lock()
ALLOWED_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Instagram Reel 영상을 Gemini로 분석합니다.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", help="분석할 Instagram Reel URL 한 건")
    source.add_argument("--xlsx", type=Path, help="url 또는 reel_url 열을 순회할 XLSX 파일")
    source.add_argument(
        "--sync-key-pool",
        action="store_true",
        help=".env의 키 별칭을 Google Sheets 프로젝트 설정과 동기화",
    )
    parser.add_argument(
        "--model",
        choices=ALLOWED_MODELS,
        help="지정한 Flash 모델만 사용합니다. 생략하면 공용 풀이 자동 선택합니다.",
    )
    return parser.parse_args(argv)


def ensure_api_keys(pool: GeminiKeyPool):
    """API 키가 하나도 없으면 안내하고 프로그램을 종료한다."""
    if not pool.keys:
        print("=" * 60)
        print("[오류] GEMINI_API_KEYS가 설정되어 있지 않습니다.")
        print(".env.example 파일을 복사해서 .env 파일을 만든 뒤,")
        print("GEMINI_API_KEYS를 큰따옴표로 감싸 키를 한 줄씩 입력하세요.")
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
# 2. Gemini 분석 - 구조화 출력 스키마 (Pydantic)
# ---------------------------------------------------------------------------

class Hook(BaseModel):
    description: str = Field(description="첫 3초에 사용된 Hook에 대한 설명")
    strength: str = Field(description="strong | medium | weak")


class Camera(BaseModel):
    main_composition: str = Field(description="영상 전체의 주요 카메라 구도")
    angles: List[str] = Field(default_factory=list, description="등장하는 카메라 앵글 목록")
    movements: List[str] = Field(default_factory=list, description="등장하는 카메라 움직임 목록")


class Editing(BaseModel):
    style: str = Field(description="영상 편집 스타일")
    cut_speed: str = Field(description="fast | medium | slow")
    transitions: List[str] = Field(default_factory=list, description="사용된 전환 기법 목록")


class Subtitle(BaseModel):
    exists: bool
    position: Optional[str] = Field(default=None, description="자막/텍스트 오버레이 위치")
    style: Optional[str] = Field(default=None, description="자막/텍스트 오버레이 스타일 (말풍선 여부 등)")


class Subjects(BaseModel):
    people_count: int
    main_subject: str = Field(description="주요 피사체 (인물, 캐릭터, 상품 등)")


class Product(BaseModel):
    exists: bool
    description: Optional[str] = None


class SceneDetail(BaseModel):
    scene_number: int
    start_second: float
    end_second: float
    section: str = Field(description="이 장면이 속하는 구간. 'hook' 또는 'body' 중 하나")
    visual_description: str = Field(description="장면에 대한 상세 시각적 묘사 (인물/캐릭터, 동작, 배경, 소품 포함)")
    layout_type: str = Field(
        description="이 장면의 구도/포맷 유형. 예: mirror_selfie, flat_lay_outfit_grid, closeup, "
        "talking_head, product_shot 등 실제 관찰되는 형태를 자유롭게 기술"
    )
    camera: str = Field(description="카메라 구도/앵글")
    camera_movement: str = Field(description="카메라 움직임 (없으면 '없음')")
    on_screen_text: Optional[str] = Field(default=None, description="화면에 표시된 자막/말풍선 텍스트 원문")
    audio_or_dialogue: Optional[str] = Field(default=None, description="대사, 나레이션, 배경음악 특징")
    transition_in: str = Field(description="이전 장면에서 넘어올 때 사용된 전환 기법 (없으면 '없음')")
    emotional_tone: str = Field(description="이 장면이 전달하는 감정/분위기")
    purpose: str = Field(description="이 장면이 영상 전체에서 하는 역할")


class VideoGenerationPrompts(BaseModel):
    video_prompt_en: str = Field(
        description="AI 영상 생성 모델(예: Veo, Sora, Runway 등) 입력용 영문 프롬프트. "
        "'video of' 같은 표현은 쓰지 말고, 주제/캐릭터/구도/씬 전환을 명확하게 기술할 것."
    )
    graphic_post_processing_needed: bool = Field(
        description="텍스트 오버레이, 말풍선, 누끼(배경 제거) 합성 등 AI 영상 생성 후 별도 후처리가 필요한지 여부"
    )
    post_processing_notes: Optional[str] = Field(
        default=None, description="필요한 후처리 작업에 대한 구체적인 설명 (한글, 자막 폰트/위치 등)"
    )


class MarketingAnalysis(BaseModel):
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    notable_elements: List[str] = Field(default_factory=list)


class VideoAnalysis(BaseModel):
    summary: str = Field(description="영상 전체 내용 요약")
    hook: Hook
    body_structure: str = Field(
        description="훅 이후 본편이 어떤 구조로 전개되는지 설명. "
        "예: '인물/캐릭터가 등장하는 훅 이후, 인물 없이 상품을 바닥에 배치한 "
        "플랫레이(flat lay) 컷들이 순서대로 전환되며 코디를 소개하는 구조'"
    )
    camera: Camera
    editing: Editing
    subtitle: Subtitle
    subjects: Subjects
    product: Product
    scene_details: List[SceneDetail] = Field(description="씬별 상세 분석 목록. 훅과 바디를 모두 포함해야 함")
    content_type: str
    marketing_analysis: MarketingAnalysis
    generation_prompts: VideoGenerationPrompts


ANALYSIS_PROMPT = """
이 Instagram Reel 영상을 분석하세요.

주어진 JSON 스키마에 맞춰 결과를 반환해야 합니다. 아래 지침을 반드시 지키세요.
분석 목적은 원본의 인물/캐릭터 외형, 상품 외형, 움직임을 재현할 수 있게 관찰 정보를 남기는 것입니다.
기존 JSON 필드 안에 상세 내용을 기록하고, 스키마에 없는 필드는 추가하지 마세요.
보이지 않는 부분이나 흐려서 확인할 수 없는 특징은 추측하지 말고 '확인 불가'로 표시하세요.
브랜드, 제품명, 인물의 신원은 추정하지 마세요. 시간과 위치는 관찰 가능한 범위에서만 기술하세요.
모든 설명은 한국어로 작성하되, generation_prompts.video_prompt_en만 영어로 작성하세요.

[분석 절차]
1. 출력을 작성하기 전에 영상을 처음부터 끝까지 확인하고, 실제 영상 길이와 모든 시각적·청각적 전환 시점을 먼저 파악하세요.
2. 커트, 구도, 대상, 의상/상품, 텍스트가 바뀌는 순간을 기준으로 신 경계를 먼저 확정하세요.
3. 확정한 타임라인을 기준으로 scene_details를 완성한 뒤, 요약 필드와 generation_prompts를 작성하세요.
4. visual_description은 화면에서 확인한 사실만 적고, emotional_tone, purpose, marketing_analysis에서만 근거 있는 해석을 제공하세요.

[인물/캐릭터 외형 기록]
1. subjects.main_subject에 주요 인물/캐릭터 각각을 구별할 수 있는 이름표(인물 A, 캐릭터 A 등)를 붙이고,
   얼굴 윤곽, 눈/눈썹/코/입의 보이는 형태, 헤어스타일과 색, 체형과 신체 비율을 구체적으로 적으세요.
   인형 탈이나 마스코트라면 실제 사람의 얼굴로 해석하지 말고 머리 형태, 귀, 눈/입의 배치,
   털/천/플라스틱 등 보이는 표면 질감과 색 구획을 기록하세요.
2. 의상은 상의/하의/신발/액세서리별 색, 핏, 길이, 소재의 시각적 특징, 무늬와 장식 위치를 기록하세요.
   여러 인물이 있으면 각 인물의 특징을 섞지 말고 장면마다 동일한 이름표를 사용하세요.
3. scene_details의 visual_description에도 해당 장면에서 보이는 외형, 의상, 표정, 시선 방향을 기록하세요.
   장면 사이 의상이나 외형이 실제로 달라지면 그 변화도 명시하세요.

[상품 외형과 배치 기록]
1. product.description에 상품 A, 상품 B처럼 개별 상품을 구분해 기록하세요. 코디 세트도 개별 품목으로 나누고,
   각 상품의 종류, 실루엣, 가로/세로 비율, 색 구획, 재질과 표면 질감, 패턴, 부속품을 설명하세요.
   로고/문구는 읽히는 경우만 원문과 위치를 적고, 읽히지 않으면 판독 불가로 표시하세요.
2. visual_description에는 해당 상품의 화면 내 위치, 다른 물체 대비 크기, 방향, 앞/뒷면 노출,
   겹침과 가림 상태를 기록하세요. 실측 크기를 추정하지 마세요.
3. 플랫레이 장면은 상의/하의/신발/소품의 상대적 배치와 간격, 배경색, 그림자 방향까지 설명하세요.
   정지된 상품 배치를 사람이 착용하거나 손으로 움직이는 장면으로 해석하지 마세요.

[움직임 기록]
1. 각 visual_description에 '시작 상태 / 시간별 동작 / 종료 상태' 순서로 자세와 움직임을 기록하세요.
   영상 전체 기준 초 단위 시점을 사용하고, 동작 변화가 보이는 시점만 구분하세요.
2. 어느 신체 부위가 무엇을 잡고 어디에서 어디로 움직이는지, 이동 방향, 회전, 속도 변화,
   멈춤과 놓는 순간을 기술하세요. 좌우는 화면 기준임을 명시하고, 실제 오른손/왼손은 확실할 때만 적으세요.
   거울 셀카의 좌우 반전을 고려하고, 가려진 동작이나 샘플 사이 동작을 만들어내지 마세요.
3. 피사체 움직임은 visual_description에, 카메라 이동은 camera_movement에 분리해 기록하세요.
   정지 장면은 '움직임 없음'으로 명시하고, 컷으로 상품이 바뀐 것을 물체가 변형되거나 이동한 것으로 쓰지 마세요.

[구조 판별 지침]
1. 영상이 인물/캐릭터가 등장하는 훅(hook) 이후, 인물 없이 상품이나 코디를 배치한
   플랫레이(flat lay) 이미지/영상 전환 구조로 바뀌는지 반드시 확인하고 body_structure에 명시하세요.
   훅 구간과 바디 구간의 시각적 구성(등장 인물 유무, 구도, 배경)이 다르다면 그 차이를 구체적으로 설명하세요.
2. 영상의 0초 이상 3초 미만은 hook, 3초 이상은 body로 분류하세요. 3초를 가로지르는 장면은 3초 경계에서 나누고,
   영상이 3초보다 짧으면 모든 장면을 hook으로 분류하세요.

[씬 분석 지침]
1. 컷이 바뀌거나(화면 전환), 카메라 구도가 바뀌거나, 인물 유무가 바뀌거나, 새로운 텍스트/자막이
   등장하는 시점마다 반드시 새로운 scene_detail 항목을 만드세요. 뭉뚱그려서 크게 나누지 마세요.
2. scene_number는 1부터 시작해 시간순으로 1씩 증가해야 하며, 모든 장면은 start_second < end_second여야 합니다.
3. 첫 장면은 start_second=0으로 시작하고, 다음 장면의 start_second는 바로 앞 장면의 end_second와 같아야 합니다.
   빈 구간이나 겹치는 구간을 만들지 마세요. 마지막 장면의 end_second는 실제 영상 종료 시각과 일치해야 합니다.
4. 하나의 시각적 상태가 3초를 넘게 유지되면 동작의 자연스러운 하위 단계를 기준으로 3초 이하의 연속 장면으로 나누세요.
   이 분할은 새로운 커트를 의미하지 않으며, transition_in에 임의의 전환 효과를 만들지 마세요.
5. layout_type에는 mirror_selfie, flat_lay_outfit_grid, closeup, talking_head, product_shot 등
   실제 관찰되는 형태를 최대한 정확한 표현으로 적으세요.
6. visual_description은 "사람이 말한다" 같은 뭉뚱그린 표현 대신, 무엇을 어떻게 하고 있는지 구체적으로 서술하세요.
7. on_screen_text에는 화면에서 확실히 판독되는 텍스트만 원문 그대로 옮기세요. 일부만 판독되면 확인된 부분과 '[판독 불가]'를 구분하고,
   텍스트가 없거나 전혀 판독할 수 없으면 null을 사용하세요.

[필드 간 일관성]
1. subjects.people_count는 영상 전체에서 구별되는 실제 인물의 수입니다. 거울이나 반사에 나타난 같은 인물을 두 명으로 세지 마세요.
2. subtitle.exists는 판독 여부와 관계없이 텍스트 오버레이가 한 번이라도 보이면 true입니다. false인 경우 position과 style은 null이어야 합니다.
3. camera, editing, subjects, product, body_structure는 scene_details에 기록한 사실을 요약해야 하며, scene_details에 없는 대상이나 전환을 추가하지 마세요.
4. product.exists가 false면 description은 null이어야 하며, true면 실제로 보이는 상품만 description에 포함하세요.
5. marketing_analysis의 강점·약점·특이 요소는 관찰된 훅, 편집, 상품 노출, CTA에 근거해야 하며 성과나 시청자 반응을 지어내지 마세요.

[영상 생성 프롬프트(generation_prompts) 지침]
1. video_prompt_en에는 이 영상을 AI 영상 생성 모델(Veo, Sora, Runway 등)로 재현하기 위한 영문 프롬프트를
   작성하세요. 주제, 캐릭터(의상/외형 포함), 구도, 씬 전환 순서를 명확하게 기술하세요.
2. 'video of', 'a video showing' 같은 불필요한 표현은 쓰지 말고, 주제와 장면을 바로 묘사하세요.
3. 텍스트 오버레이나 한글 말풍선처럼 AI 영상 생성 모델이 재현하기 어려운 요소는 video_prompt_en에
   포함하지 말고, 대신 graphic_post_processing_needed를 true로 표시하고 post_processing_notes에
   별도로 어떤 후처리(자막 합성, 누끼 합성 등)가 필요한지 설명하세요.
4. video_prompt_en은 짧은 전체 요약으로 축약하지 마세요. 하나의 문자열 안에 줄바꿈으로
   'Appearance and setting', 'Shot timeline', 'Continuity constraints' 구간을 구성하세요.
   Appearance and setting에는 관찰된 인물/캐릭터와 상품의 구별되는 외형, 배경, 조명을 영문으로 옮기세요.
5. Shot timeline에는 모든 scene_details를 순서대로 반영하고 각 장면의 시작/종료 초, 등장 대상,
   구도와 상품 배치, 시작 자세, 동작 순서와 속도, 종료 자세, 카메라 움직임, 전환 기법을 적으세요.
   단순 분석용 분할로 나눈 연속 장면에는 새로운 컷을 지시하지 말고 동작이 연속됨을 명시하세요.
6. Continuity constraints에는 동일 대상의 얼굴/캐릭터 형태, 의상, 상품 실루엣/색/패턴을
   원본에서 관찰된 변화 외에는 유지하고, 추가 인물/상품/몸짓/카메라 움직임을 만들지 않도록 적으세요.
   원본이 정지 이미지나 플랫레이 컷이면 그 정적 특성을 유지하도록 지시하세요.
7. 원본에서 확인하지 못한 특징을 생성 프롬프트에서 확정하지 마세요. 분위기를 꾸미기 위해
   조명, 외형, 동작을 추가하지 말고, 분석 내용과 생성 지시가 일치하는지 확인하세요.
   후처리가 필요한 자막/로고/그래픽의 위치와 등장 시간은 post_processing_notes에 기록하세요.

[출력 전 자체 검증]
출력 직전에 다음을 내부적으로 검사하고, 어긋나는 항목은 수정한 뒤 JSON만 반환하세요.
- scene_number가 1부터 연속적인가?
- 모든 장면이 start_second < end_second이고, 0초부터 실제 종료 시각까지 빈틈이나 겹침 없이 연결되는가?
- 3초 경계에서 hook과 body가 올바르게 나뉘었는가?
- 요약 필드, scene_details, video_prompt_en 사이에 인물·상품·의상·동작·시간의 모순이 없는가?
- 확인하지 못한 신원, 브랜드, 텍스트, 신체 특징, 동작을 추정하지 않았는가?
"""


def _build_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=VideoAnalysis,
    )


def _call_with_pool(pool: GeminiKeyPool, call_fn, max_attempts: Optional[int] = None):
    """
    call_fn(client, model) -> response 형태의 함수를 받아서,
    풀에서 (키, 모델) 조합을 하나씩 꺼내가며 성공할 때까지 시도한다.

    일일 한도는 소진 처리하고, 일시적 제한은 대기 후 재사용한다.
    인증/모델 오류는 제외하고 요청 자체의 오류는 즉시 반환한다.
    """
    max_attempts = max_attempts or len(pool.combos) * 2
    last_exc = None

    for attempt in range(1, max_attempts + 1):
        combo = pool.acquire_blocking(max_wait_sec=POOL_WAIT_MAX_SEC)
        print(f"  - 사용 조합: {combo.combo_id}  (시도 {attempt}/{max_attempts})")

        client = None
        response = None
        call_error = None
        try:
            client = genai.Client(api_key=combo.api_key)
            response = call_fn(client, combo.model)
            return response
        except genai_errors.APIError as e:
            call_error = e
            last_exc = e
            is_quota = e.code == 429 or (e.status and "RESOURCE_EXHAUSTED" in str(e.status))
            is_transient = e.code in (500, 502, 503, 504) or (
                e.status and "UNAVAILABLE" in str(e.status)
            )
            if is_quota:
                reason = pool.mark_quota_error(combo, e)
                print(f"    -> {combo.combo_id} {reason}. 다음 조합을 시도합니다.")
            elif is_transient:
                print(f"    -> {combo.combo_id} 일시적 서버 오류({e.code}). 다른 조합으로 넘어갑니다.")
                pool.mark_cooldown(combo, 2)
            elif e.code in (401, 403):
                pool.disable(combo, whole_key=True)
                print(f"    -> {combo.key_label} 인증/권한 오류({e.code}). 다음 키를 시도합니다.")
            elif e.code == 404:
                pool.disable(combo)
                print(f"    -> {combo.combo_id} 모델을 사용할 수 없습니다. 다음 조합을 시도합니다.")
            else:
                raise
        except BaseException as e:
            call_error = e
            raise
        finally:
            try:
                pool.finish(combo, response=response, error=call_error)
            finally:
                if client is not None:
                    client.close()

    raise last_exc or RuntimeError("모든 (키, 모델) 조합 시도가 실패했습니다.")


def _analyze_inline(pool: GeminiKeyPool, video_bytes: bytes, mime_type: str) -> dict:
    """작은 영상: base64 inline으로 바로 전송."""

    def _call(client: genai.Client, model: str):
        video_part = types.Part(
            inline_data=types.Blob(data=video_bytes, mime_type=mime_type),
            video_metadata=types.VideoMetadata(fps=SCENE_ANALYSIS_FPS),
        )
        return client.models.generate_content(
            model=model,
            contents=types.Content(parts=[video_part, types.Part(text=ANALYSIS_PROMPT)]),
            config=_build_config(),
        )

    response = _call_with_pool(pool, _call)
    return json.loads(response.text)


def _analyze_via_files_api(pool: GeminiKeyPool, video_bytes: bytes, mime_type: str) -> dict:
    """큰 영상: Files API로 업로드 후 처리 완료를 기다렸다가 분석."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

        def _call(client: genai.Client, model: str):
            # 파일은 업로드한 프로젝트 소유이므로 키가 바뀌면 다시 업로드한다.
            uploaded_file = None
            try:
                print("  - 영상이 커서 Files API로 업로드합니다...")
                uploaded_file = client.files.upload(file=tmp_path)
                while uploaded_file.state.name == "PROCESSING":
                    print("  - 파일 처리 중... (5초 후 재확인)")
                    time.sleep(5)
                    uploaded_file = client.files.get(name=uploaded_file.name)
                if uploaded_file.state.name != "ACTIVE":
                    raise RuntimeError("Gemini Files API 파일 처리에 실패했습니다.")
                return client.models.generate_content(
                    model=model,
                    contents=types.Content(parts=[
                        types.Part(
                            file_data=types.FileData(
                                file_uri=uploaded_file.uri,
                                mime_type=uploaded_file.mime_type,
                            ),
                            video_metadata=types.VideoMetadata(fps=SCENE_ANALYSIS_FPS),
                        ),
                        types.Part(text=ANALYSIS_PROMPT),
                    ]),
                    config=_build_config(),
                )
            finally:
                if uploaded_file is not None:
                    try:
                        client.files.delete(name=uploaded_file.name)
                    except Exception:
                        print("  - 임시 업로드 파일 삭제에 실패했습니다.")

        response = _call_with_pool(pool, _call)

        return json.loads(response.text)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def analyze_video(pool: GeminiKeyPool, video_bytes: bytes, mime_type: str = "video/mp4") -> dict:
    size_mb = len(video_bytes) / (1024 * 1024)
    print(f"  - 영상 용량 : {size_mb:.2f} MB")

    if size_mb > INLINE_SIZE_LIMIT_MB:
        return _analyze_via_files_api(pool, video_bytes, mime_type)
    return _analyze_inline(pool, video_bytes, mime_type)


# ---------------------------------------------------------------------------
# 3. 결과 저장
# ---------------------------------------------------------------------------

def save_result(shortcode: str, result: dict) -> str:
    with OUTPUT_LOCK:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        records = []
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                raise ValueError(f"누적 결과 파일은 JSON 배열이어야 합니다: {OUTPUT_FILE}")

        records.append({
            "reel_id": shortcode or "reel",
            "analyzed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "analysis": result,
        })
        temp_path = OUTPUT_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, OUTPUT_FILE)
    return OUTPUT_FILE


def print_summary(result: dict):
    print("\n" + "=" * 60)
    print("[분석 요약]")
    print("-" * 60)
    print(f"요약         : {result.get('summary')}")
    hook = result.get("hook", {})
    print(f"Hook         : {hook.get('description')} (강도: {hook.get('strength')})")
    print(f"바디 구조    : {result.get('body_structure')}")
    print(f"콘텐츠 유형  : {result.get('content_type')}")

    gen = result.get("generation_prompts", {})
    video_prompt = gen.get("video_prompt_en", "")
    preview = (video_prompt[:150] + "...") if len(video_prompt) > 150 else video_prompt
    print("-" * 60)
    print(f"영상 생성 프롬프트(EN) : {preview}")
    print(f"후처리 필요 여부       : {gen.get('graphic_post_processing_needed')}")
    if gen.get("post_processing_notes"):
        print(f"후처리 참고            : {gen.get('post_processing_notes')}")

    scenes = result.get("scene_details", [])
    print("-" * 60)
    print(f"씬 개수      : {len(scenes)}개 (hook: {sum(1 for s in scenes if s.get('section') == 'hook')} / "
          f"body: {sum(1 for s in scenes if s.get('section') == 'body')})")
    print("=" * 60)


# ---------------------------------------------------------------------------
# 4. 메인 루프
# ---------------------------------------------------------------------------

def process_one(pool: GeminiKeyPool, reel_url: str):
    print(f"\n[처리 시작] {reel_url}")

    print("1) 영상 다운로드 중...")
    reel = get_instagram_video(reel_url)

    print("2) Gemini 분석 중...")
    result = analyze_video(pool, reel["video_bytes"], reel["mime_type"])

    print("3) 결과 저장 중...")
    filepath = save_result(reel["id"], result)
    print(f"  - 저장 완료: {filepath}")

    print_summary(result)


def process_safely(pool: GeminiKeyPool, reel_url: str) -> bool:
    if not is_instagram_reel_url(reel_url):
        print(f"[경고] Instagram Reel URL이 아닙니다: {reel_url}\n")
        return False
    try:
        process_one(pool, reel_url)
        return True
    except yt_dlp.utils.DownloadError as e:
        print(f"[오류] 영상을 가져오지 못했습니다: {e}")
        print("      비공개 계정이거나 삭제된 게시물일 수 있습니다.\n")
    except requests.exceptions.RequestException as e:
        print(f"[오류] 영상 다운로드 중 네트워크 오류: {e}\n")
    except KeyPoolExhaustedError as e:
        print(f"[오류] {e}\n")
    except Exception as e:
        print(f"[오류] 분석 중 예상치 못한 문제가 발생했습니다: {e}\n")
    return False


def run_interactive(status_pool: GeminiKeyPool, worker_pools: list[GeminiKeyPool]):
    """URL 입력은 계속 받고, 각 워커가 자신의 키 풀 예약으로 분석한다."""
    work_queue = Queue()

    def consume(worker_pool):
        while True:
            reel_url = work_queue.get()
            try:
                if reel_url is None:
                    return
                process_safely(worker_pool, reel_url)
            finally:
                work_queue.task_done()

    workers = [
        threading.Thread(target=consume, args=(worker_pool,), name=f"gemini-worker-{i + 1}")
        for i, worker_pool in enumerate(worker_pools)
    ]
    for worker in workers:
        worker.start()

    print("Reel URL을 연속으로 입력할 수 있습니다. 입력한 순서대로 대기열에 추가됩니다.")
    print("'status' 입력 시 현재 사용량 확인. 종료하려면 'quit' 또는 'exit' 입력.\n")
    try:
        while True:
            try:
                reel_url = input("Reel URL > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n입력된 분석을 마친 뒤 종료합니다.")
                break

            if not reel_url:
                continue
            if reel_url.lower() in ("quit", "exit", "q"):
                print("입력된 분석을 마친 뒤 종료합니다.")
                break
            if reel_url.lower() == "status":
                try:
                    print(status_pool.status())
                except RuntimeError as e:
                    print(f"[시트 오류] {e}\n")
                continue
            if not is_instagram_reel_url(reel_url):
                print(f"[경고] Instagram Reel URL이 아닙니다: {reel_url}\n")
                continue
            work_queue.put(reel_url)
            print(f"  - 대기열 추가 완료 (대기 {work_queue.qsize()}건)")
    finally:
        for _ in workers:
            work_queue.put(None)
        work_queue.join()
        for worker in workers:
            worker.join()


def main(argv=None):
    options = parse_args(argv)
    if options.model:
        os.environ["GEMINI_MODELS"] = options.model
    try:
        pool = create_pool()
    except RuntimeError as e:
        print(f"[설정 오류] {e}")
        return
    if options.sync_key_pool:
        if not hasattr(pool, "sync_keys"):
            print("[설정 오류] 키 동기화는 GEMINI_POOL_MODE=sheets에서만 사용할 수 있습니다.")
            return
        try:
            result = pool.sync_keys()
        except RuntimeError as e:
            print(f"[시트 오류] {e}")
            return
        print(
            "키 풀 동기화 완료: "
            f"공유 추가 {result['vault_added']}개 / 공유 갱신 {result['vault_updated']}개 / "
            f"담당자 변경 {result['owner_renamed']}행 / "
            f"로컬 추가 {result['local_added']}개 / 로컬 갱신 {result['local_updated']}개 / "
            f"추가 {result['added']}행 / 정보 정리 {result['updated']}행 / "
            f"기록 정리 {result['log_updated']}행 / 사용 전환 {result['enabled']}행 / "
            f"중지 {result['stopped']}행"
        )
        return

    ensure_api_keys(pool)

    print("Instagram Reels Analyzer")
    print(f"등록된 키 {len(pool.keys)}개 x 모델 {len(pool.models)}개 = 총 {len(pool.combos)}개 조합 사용 가능")
    if options.model:
        print(f"선택 모델: {options.model}")

    if options.url:
        process_safely(pool, options.url)
        return

    if options.xlsx:
        try:
            urls = read_reel_urls_from_xlsx(options.xlsx)
        except (OSError, ValueError) as e:
            print(f"[XLSX 오류] {e}")
            return
        if not urls:
            print("[XLSX 오류] url 또는 reel_url 열에서 Instagram Reel URL을 찾지 못했습니다.")
            return
        succeeded = 0
        for index, reel_url in enumerate(urls, 1):
            print(f"\n[XLSX {index}/{len(urls)}]")
            succeeded += process_safely(pool, reel_url)
        print(f"\n[XLSX 완료] 성공 {succeeded}건 / 실패 {len(urls) - succeeded}건 / 전체 {len(urls)}건")
        return

    sheets_mode = os.getenv("GEMINI_POOL_MODE", "sheets").strip().lower() == "sheets"
    worker_count = INTERACTIVE_WORKERS if sheets_mode else 1
    try:
        worker_pools = [create_pool() for _ in range(worker_count)]
    except RuntimeError as e:
        print(f"[설정 오류] {e}")
        return
    run_interactive(pool, worker_pools)


if __name__ == "__main__":
    main()
