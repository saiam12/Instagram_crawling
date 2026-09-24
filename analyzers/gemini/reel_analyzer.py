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
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo

import requests
import yt_dlp
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator
from google.genai import errors as genai_errors

from google import genai
from google.genai import types

from pool import GeminiKeyPool, KeyPoolExhaustedError, create_pool
from input_sources import normalize_reel_url, read_reel_urls_from_xlsx
from prompts import ANALYSIS_PROMPT


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

load_dotenv()  # .env 파일에서 환경변수 로드

# Gemini에 inline(base64)으로 바로 보낼 수 있는 최대 용량(MB).
# 이보다 크면 자동으로 Files API(업로드 방식)로 전환합니다.
INLINE_SIZE_LIMIT_MB = 18

# 풀에서 쓸 수 있는 조합이 당장 없을 때(RPM 제한), 최대 몇 초까지 기다릴지
POOL_WAIT_MAX_SEC = 90

# 최초 호출을 포함한 API 최대 시도 횟수.
MAX_API_ATTEMPTS = 5

# 대화형 입력과 분석을 분리하되 프로젝트별 API 한도를 과도하게 밀어붙이지 않는다.
INTERACTIVE_WORKERS = 2

# 장면(scene) 분석용 프레임 샘플링 속도(fps).
SCENE_ANALYSIS_FPS = 5

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
DEFAULT_OUTPUT_FILE = os.path.join(OUTPUT_DIR, "reel_analyses.json")
# Batch callers can give each worker an isolated file.  The normal CLI keeps
# the historical output path and behavior.
OUTPUT_FILE = os.getenv("GEMINI_OUTPUT_FILE", DEFAULT_OUTPUT_FILE)
WRITE_READABLE_OUTPUT = "GEMINI_OUTPUT_FILE" not in os.environ
OUTPUT_LOCK = threading.Lock()
ALLOWED_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Instagram Reel 영상을 Gemini로 분석합니다.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("-url", "--url", help="분석할 Instagram Reel URL 또는 shortcode 한 건")
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
    parser.add_argument(
        "--group-size",
        type=int,
        choices=(1, 2),
        default=1,
        help="한 번의 Gemini 호출로 분석할 영상 수 (기본값: 1)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="재현 가능한 출력을 위한 고정 seed (생략하면 API 기본값 사용)",
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

    thumbnail_bytes = None
    thumbnail_mime_type = None
    thumbnail_url = info.get("thumbnail")
    if thumbnail_url:
        try:
            thumbnail_response = requests.get(thumbnail_url, headers=headers, timeout=30)
            thumbnail_response.raise_for_status()
            thumbnail_bytes = thumbnail_response.content
            thumbnail_mime_type = thumbnail_response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
            print("  - thumbnail : Instagram 메타데이터 커버 이미지 사용")
        except requests.exceptions.RequestException as e:
            print(f"  - thumbnail : 커버 다운로드 실패, 영상 첫 프레임으로 대체 ({e})")

    return {
        "id": info.get("id"),
        "video_bytes": response.content,
        "mime_type": "video/mp4",
        "thumbnail_bytes": thumbnail_bytes,
        "thumbnail_mime_type": thumbnail_mime_type,
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
    angles: List[str] = Field(
        default_factory=list,
        description="scene_details.camera의 angle 영문 코드만 중복 없이 나열",
    )
    movements: List[str] = Field(
        default_factory=list,
        description="scene_details.camera_movement의 type 영문 코드만 중복 없이 나열",
    )


class Editing(BaseModel):
    style: str = Field(description="영상 편집 스타일")
    cut_speed: str = Field(description="fast | medium | slow")
    transitions: List[str] = Field(default_factory=list, description="사용된 전환 기법 목록")


class Subtitle(BaseModel):
    exists: bool
    position: Optional[str] = Field(default=None, description="자막/텍스트 오버레이 위치")
    style: Optional[str] = Field(default=None, description="자막/텍스트 오버레이 스타일 (말풍선 여부 등)")


class SubjectPerson(BaseModel):
    subject_id: str = Field(description="영상 전체에서 동일하게 사용하는 인물 A, 인물 B 등의 이름표")
    gender_presentation: Literal["male", "female", "unknown"] = Field(
        description="영상에서 확인되는 남성형/여성형 표현. 모호하면 unknown이며 실제 성별·성 정체성을 뜻하지 않음"
    )


class Subjects(BaseModel):
    people_count: int
    main_subject: str = Field(description="주요 피사체 (인물, 캐릭터, 상품 등)")
    people: List[SubjectPerson] = Field(description="등장하는 실제 인물을 이름표별로 한 번씩 기록")


class Product(BaseModel):
    exists: bool
    description: Optional[str] = None


class SceneWornOutfit(BaseModel):
    subject_id: str = Field(description="이 의상을 실제로 입은 인물의 이름표")
    gender_presentation: Literal["male", "female", "unknown"] = Field(
        description="해당 인물의 subjects.people.gender_presentation과 동일한 코드"
    )
    clothing_items: List[str] = Field(min_length=1, description="이 장면에서 해당 인물이 실제로 착용한 옷, 신발, 액세서리")


class SceneDetail(BaseModel):
    scene_number: int
    start_second: float
    end_second: float
    section: str = Field(description="이 장면이 속하는 구간. 'hook' 또는 'body' 중 하나")
    visual_description: str = Field(description="장면에 대한 상세 시각적 묘사 (인물/캐릭터, 동작, 배경, 소품 포함)")
    worn_outfits: List[SceneWornOutfit] = Field(
        description="장면에서 실제 착용 중인 인물별 의상. 플랫레이, 마네킹, 단독 상품만 보이면 빈 목록"
    )
    layout_type: str = Field(
        description="mirror_selfie | flat_lay_outfit_grid | closeup | talking_head | product_shot | "
        "full_body_fashion_shot | split_screen | text_only | scenery | other 중 하나"
    )
    camera: str = Field(
        description="shot_size=<표준 코드>; angle=<표준 코드>; composition=<표준 코드> 형식"
    )
    camera_movement: str = Field(
        description="type=<표준 코드>; direction=<표준 코드>; speed=<표준 코드> 형식. "
        "움직임이 없으면 type=static; direction=none; speed=none"
    )
    on_screen_text: Optional[str] = Field(default=None, description="화면에 표시된 자막/말풍선 텍스트 원문")
    audio_or_dialogue: Optional[str] = Field(default=None, description="대사, 나레이션, 배경음악 특징")
    transition_in: str = Field(
        description="none | hard_cut | jump_cut | match_cut | dissolve | fade_in | fade_out | wipe | "
        "whip_pan | graphic_match | other 중 하나"
    )
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


class TranscriptSegment(BaseModel):
    start_second: float
    end_second: float
    speaker: str = Field(description="인물 A, 인물 B, narrator 또는 unknown")
    text: str = Field(description="들리는 대사나 내레이션의 원문. 노래 가사는 제외")


class SoundEvent(BaseModel):
    start_second: float
    end_second: float
    type: str = Field(
        description="laughter | clap | snap | footsteps | impact | whoosh | click | object_handling | "
        "animal | vehicle | ambient | other 중 하나"
    )
    description: str = Field(description="실제로 들리는 비언어 소리에 대한 한국어 설명")


class BackgroundMusic(BaseModel):
    exists: bool
    mood: Optional[str] = Field(
        default=None,
        description="neutral | upbeat | calm | dark | dramatic | romantic | playful | energetic | sad | other | unknown",
    )
    tempo: str = Field(description="none | slow | medium | fast | variable | unknown")
    vocals: Optional[bool] = Field(default=None, description="보컬 존재 여부. 확인할 수 없으면 null")


class AudioAnalysis(BaseModel):
    speech_present: bool
    language: Optional[str] = Field(default=None, description="ISO 639-1 언어 코드, mixed, unknown 또는 null")
    transcript: List[TranscriptSegment] = Field(default_factory=list)
    sound_events: List[SoundEvent] = Field(default_factory=list)
    background_music: BackgroundMusic


class SellingPoint(BaseModel):
    point: str = Field(description="영상이 강조하는 구체적인 판매 포인트")
    evidence: str = Field(description="판매 포인트를 뒷받침하는 음성, 화면, 자막 근거의 종합 설명")
    spoken_evidence: Optional[str] = Field(description="실제로 들리는 핵심 대사 원문. 음성 근거가 없으면 null")
    visual_evidence: Optional[str] = Field(description="같은 구간에서 직접 보이는 행동, 상품, 전후 변화. 없으면 null")
    on_screen_text_evidence: Optional[str] = Field(description="같은 구간에서 판독되는 화면 문구 원문. 없으면 null")
    start_second: float
    end_second: float
    appeal_type: str = Field(
        description="product_feature | product_variety | styling_inspiration | transformation | convenience | "
        "price_value | scarcity | social_proof | aspiration | novelty | brand_identity | other 중 하나"
    )
    evidence_confidence: float = Field(
        ge=0,
        le=1,
        description="판매 성과 예측값이 아니라 관찰 근거가 해석을 지지하는 정도. 0에서 1 사이",
    )


class MarketingAnalysis(BaseModel):
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    notable_elements: List[str] = Field(default_factory=list)
    selling_points: List[SellingPoint] = Field(default_factory=list)

    @field_validator("selling_points")
    @classmethod
    def discard_unsupported_selling_points(cls, points: List[SellingPoint]) -> List[SellingPoint]:
        return [point for point in points if point.evidence_confidence >= 0.5]


class RecommendedAudience(BaseModel):
    age_group: Literal["10s", "20s", "30s", "20s_30s", "40s_plus", "all", "unknown"]
    gender: Literal["male", "female", "all", "unknown"]
    evidence: str = Field(min_length=1, description="화면 문구, 대사, 상품 또는 스타일을 근거로 이 대상을 추천한 이유")


class ThumbnailAnalysis(BaseModel):
    source: str = Field(description="provided_cover_image | video_first_frame")
    visual_description: str = Field(description="대표 화면에서 관찰되는 인물, 상품, 배경, 색상과 배치")
    on_screen_text: Optional[str] = Field(default=None, description="대표 화면에서 판독되는 텍스트 원문")
    focal_point: str = Field(description="가장 먼저 시선이 가는 핵심 대상과 그 이유")
    composition: str = Field(description="구도, 피사체 크기와 위치, 여백, 대비에 대한 설명")
    selling_point: Optional[str] = Field(default=None, description="대표 화면이 전달하는 판매 포인트 또는 null")
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    effectiveness: str = Field(description="strong | medium | weak")
    evidence_confidence: float = Field(ge=0, le=1, description="대표 화면에서 직접 확인되는 근거의 신뢰도")


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
    audio_analysis: AudioAnalysis
    content_type: str
    thumbnail_analysis: ThumbnailAnalysis
    marketing_analysis: MarketingAnalysis
    recommended_audience: List[RecommendedAudience] = Field(
        description="영상의 의도된 추천 대상. 대상이 여러 개면 각각 기록하며 판단 근거가 없으면 빈 목록"
    )
    generation_prompts: VideoGenerationPrompts

    @model_validator(mode="after")
    def validate_worn_outfit_links(self):
        genders = {person.subject_id: person.gender_presentation for person in self.subjects.people}
        if len(genders) != len(self.subjects.people):
            raise ValueError("subjects.people의 subject_id는 중복될 수 없습니다")
        if self.subjects.people_count != len(self.subjects.people):
            raise ValueError("subjects.people_count와 subjects.people의 인원수가 일치하지 않습니다")
        for scene in self.scene_details:
            for outfit in scene.worn_outfits:
                if outfit.subject_id not in genders:
                    raise ValueError(f"scene {scene.scene_number}: 알 수 없는 착용자 {outfit.subject_id}")
                if outfit.gender_presentation != genders[outfit.subject_id]:
                    raise ValueError(f"scene {scene.scene_number}: 착용자의 성별 표현 코드가 일치하지 않습니다")
        return self


class GroupedVideoAnalysis(BaseModel):
    input_index: int = Field(description="입력 영상 번호. VIDEO_1은 1, VIDEO_2는 2")
    analysis: VideoAnalysis


class GroupedVideoAnalyses(BaseModel):
    analyses: List[GroupedVideoAnalysis]


def _build_config(response_schema=VideoAnalysis) -> types.GenerateContentConfig:
    raw_seed = os.getenv("GEMINI_SEED", "").strip()
    try:
        seed = int(raw_seed) if raw_seed else None
    except ValueError:
        raise RuntimeError("GEMINI_SEED는 정수여야 합니다.") from None
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=response_schema,
        seed=seed,
    )


def _call_with_pool(pool: GeminiKeyPool, call_fn, max_attempts: Optional[int] = None):
    """
    call_fn(client, model) -> response 형태의 함수를 받아서,
    풀에서 (키, 모델) 조합을 하나씩 꺼내가며 성공할 때까지 시도한다.

    일일 한도는 소진 처리하고, 일시적 제한은 대기 후 재사용한다.
    인증/모델 오류는 제외하고 요청 자체의 오류는 즉시 반환한다.
    """
    max_attempts = max_attempts or MAX_API_ATTEMPTS
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


def _thumbnail_parts(thumbnail_bytes: Optional[bytes], thumbnail_mime_type: Optional[str]) -> list[types.Part]:
    if not thumbnail_bytes:
        return []
    return [
        types.Part(text="INSTAGRAM_COVER_IMAGE_START"),
        types.Part(inline_data=types.Blob(data=thumbnail_bytes, mime_type=thumbnail_mime_type or "image/jpeg")),
        types.Part(text="INSTAGRAM_COVER_IMAGE_END"),
    ]


def _analyze_inline(
    pool: GeminiKeyPool,
    video_bytes: bytes,
    mime_type: str,
    thumbnail_bytes: Optional[bytes] = None,
    thumbnail_mime_type: Optional[str] = None,
) -> dict:
    """작은 영상: base64 inline으로 바로 전송."""

    def _call(client: genai.Client, model: str):
        video_part = types.Part(
            inline_data=types.Blob(data=video_bytes, mime_type=mime_type),
            video_metadata=types.VideoMetadata(fps=SCENE_ANALYSIS_FPS),
        )
        parts = [video_part, *_thumbnail_parts(thumbnail_bytes, thumbnail_mime_type), types.Part(text=ANALYSIS_PROMPT)]
        return client.models.generate_content(
            model=model,
            contents=types.Content(parts=parts),
            config=_build_config(),
        )

    response = _call_with_pool(pool, _call)
    return VideoAnalysis.model_validate_json(response.text).model_dump()


def _analyze_via_files_api(
    pool: GeminiKeyPool,
    video_bytes: bytes,
    mime_type: str,
    thumbnail_bytes: Optional[bytes] = None,
    thumbnail_mime_type: Optional[str] = None,
) -> dict:
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
                        *_thumbnail_parts(thumbnail_bytes, thumbnail_mime_type),
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

        return VideoAnalysis.model_validate_json(response.text).model_dump()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def analyze_video(
    pool: GeminiKeyPool,
    video_bytes: bytes,
    mime_type: str = "video/mp4",
    thumbnail_bytes: Optional[bytes] = None,
    thumbnail_mime_type: Optional[str] = None,
) -> dict:
    size_mb = len(video_bytes) / (1024 * 1024)
    print(f"  - 영상 용량 : {size_mb:.2f} MB")

    if size_mb > INLINE_SIZE_LIMIT_MB:
        return _analyze_via_files_api(pool, video_bytes, mime_type, thumbnail_bytes, thumbnail_mime_type)
    return _analyze_inline(pool, video_bytes, mime_type, thumbnail_bytes, thumbnail_mime_type)


def _group_prompt(count: int) -> str:
    return f"""
아래에는 VIDEO_1부터 VIDEO_{count}까지 서로 다른 Instagram Reel 영상이 있습니다.
각 영상을 완전히 독립적으로 분석하고 다른 영상의 인물, 상품, 장면, 음성, 타임스탬프를 섞지 마세요.
VIDEO_N_INSTAGRAM_COVER 이미지는 번호가 같은 VIDEO_N에만 속하며 다른 영상의 썸네일로 사용하지 마세요.
analyses 배열에 입력 영상과 같은 순서로 정확히 {count}개를 반환하세요.
각 항목의 input_index는 VIDEO_N의 N과 정확히 같아야 합니다.

{ANALYSIS_PROMPT}
"""


def _group_parts(reels: list[dict], file_parts: Optional[list[types.Part]] = None) -> list[types.Part]:
    parts = []
    for index, reel in enumerate(reels, 1):
        parts.append(types.Part(text=f"VIDEO_{index}_START"))
        parts.append(
            file_parts[index - 1] if file_parts else types.Part(
                inline_data=types.Blob(data=reel["video_bytes"], mime_type=reel["mime_type"]),
                video_metadata=types.VideoMetadata(fps=SCENE_ANALYSIS_FPS),
            )
        )
        parts.append(types.Part(text=f"VIDEO_{index}_END"))
        if reel.get("thumbnail_bytes"):
            parts.append(types.Part(text=f"VIDEO_{index}_INSTAGRAM_COVER_START"))
            parts.append(types.Part(inline_data=types.Blob(
                data=reel["thumbnail_bytes"],
                mime_type=reel.get("thumbnail_mime_type") or "image/jpeg",
            )))
            parts.append(types.Part(text=f"VIDEO_{index}_INSTAGRAM_COVER_END"))
    parts.append(types.Part(text=_group_prompt(len(reels))))
    return parts


def _parse_grouped_response(response_text: str, count: int) -> list[dict]:
    grouped = GroupedVideoAnalyses.model_validate_json(response_text)
    by_index = {item.input_index: item.analysis for item in grouped.analyses}
    expected = set(range(1, count + 1))
    if len(grouped.analyses) != count or set(by_index) != expected:
        raise ValueError(f"묶음 분석 결과 인덱스가 입력과 다릅니다: expected={sorted(expected)}, actual={sorted(by_index)}")
    return [by_index[index].model_dump() for index in range(1, count + 1)]


def _analyze_group_inline(pool: GeminiKeyPool, reels: list[dict]) -> list[dict]:
    def _call(client: genai.Client, model: str):
        return client.models.generate_content(
            model=model,
            contents=types.Content(parts=_group_parts(reels)),
            config=_build_config(GroupedVideoAnalyses),
        )

    response = _call_with_pool(pool, _call)
    return _parse_grouped_response(response.text, len(reels))


def _analyze_group_via_files_api(pool: GeminiKeyPool, reels: list[dict]) -> list[dict]:
    temp_paths = []
    try:
        for reel in reels:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp.write(reel["video_bytes"])
                temp_paths.append(tmp.name)

        def _call(client: genai.Client, model: str):
            uploaded_files = []
            try:
                for index, path in enumerate(temp_paths, 1):
                    print(f"  - VIDEO_{index}을 Files API로 업로드합니다...")
                    uploaded = client.files.upload(file=path)
                    while uploaded.state.name == "PROCESSING":
                        time.sleep(5)
                        uploaded = client.files.get(name=uploaded.name)
                    if uploaded.state.name != "ACTIVE":
                        raise RuntimeError(f"VIDEO_{index}의 Files API 처리에 실패했습니다.")
                    uploaded_files.append(uploaded)

                file_parts = [
                    types.Part(
                        file_data=types.FileData(file_uri=item.uri, mime_type=item.mime_type),
                        video_metadata=types.VideoMetadata(fps=SCENE_ANALYSIS_FPS),
                    )
                    for item in uploaded_files
                ]
                return client.models.generate_content(
                    model=model,
                    contents=types.Content(parts=_group_parts(reels, file_parts)),
                    config=_build_config(GroupedVideoAnalyses),
                )
            finally:
                for uploaded in uploaded_files:
                    try:
                        client.files.delete(name=uploaded.name)
                    except Exception:
                        print("  - 임시 업로드 파일 삭제에 실패했습니다.")

        response = _call_with_pool(pool, _call)
        return _parse_grouped_response(response.text, len(reels))
    finally:
        for path in temp_paths:
            if os.path.exists(path):
                os.remove(path)


def analyze_reels(pool: GeminiKeyPool, reels: list[dict]) -> list[dict]:
    if not 1 <= len(reels) <= 2:
        raise ValueError("한 번에 분석할 영상은 1개 또는 2개여야 합니다.")
    if len(reels) == 1:
        reel = reels[0]
        return [analyze_video(
            pool,
            reel["video_bytes"],
            reel["mime_type"],
            reel.get("thumbnail_bytes"),
            reel.get("thumbnail_mime_type"),
        )]

    total_mb = sum(len(reel["video_bytes"]) for reel in reels) / (1024 * 1024)
    print(f"  - 묶음 영상 {len(reels)}개 / 합산 용량 {total_mb:.2f} MB / Gemini 호출 1회")
    if total_mb > INLINE_SIZE_LIMIT_MB:
        return _analyze_group_via_files_api(pool, reels)
    return _analyze_group_inline(pool, reels)


# ---------------------------------------------------------------------------
# 3. 결과 저장
# ---------------------------------------------------------------------------

def _append_readable_output(shortcode: str, analyzed_at: str, result: dict):
    from openpyxl import Workbook, load_workbook

    path = Path(OUTPUT_FILE).with_suffix(".xlsx")
    audience_headers = ("subject_genders", "scene_worn_outfits", "recommended_audience")
    if path.exists():
        workbook = load_workbook(path)
        sheet = workbook.active
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Reel analyses"
        sheet.append([
            "reel_id", "analyzed_at", "summary", "hook", "hook_strength", "body_structure",
            "content_type", "scene_count", "transcript", "background_music", "selling_points",
            *audience_headers,
        ])
        sheet.freeze_panes = "A2"
        widths = (18, 24, 50, 45, 14, 55, 22, 12, 60, 35, 60)
        for column, width in enumerate(widths, 1):
            sheet.column_dimensions[chr(64 + column)].width = width
    for column, header in enumerate(audience_headers, 12):
        sheet.cell(row=1, column=column, value=header)
    for column, width in enumerate((32, 70, 55), 12):
        sheet.column_dimensions[chr(64 + column)].width = width
    sheet.auto_filter.ref = "A1:N1"

    audio = result.get("audio_analysis") or {}
    transcript = "\n".join(
        f"[{item.get('start_second')}-{item.get('end_second')}] {item.get('speaker')}: {item.get('text')}"
        for item in audio.get("transcript") or []
    )
    selling_points = "\n".join(
        f"[{item.get('start_second')}-{item.get('end_second')}] {item.get('point')} "
        f"(confidence={item.get('evidence_confidence')}): {item.get('evidence')}\n"
        f"  음성: {item.get('spoken_evidence') or '없음'}\n"
        f"  화면: {item.get('visual_evidence') or '없음'}\n"
        f"  문구: {item.get('on_screen_text_evidence') or '없음'}"
        for item in (result.get("marketing_analysis") or {}).get("selling_points") or []
    )
    hook = result.get("hook") or {}
    presentation_labels = {"male": "남성형", "female": "여성형", "unknown": "확인 불가"}
    age_labels = {
        "10s": "10대", "20s": "20대", "30s": "30대", "20s_30s": "20~30대",
        "40s_plus": "40대 이상", "all": "전 연령", "unknown": "연령 확인 불가",
    }
    audience_gender_labels = {"male": "남성", "female": "여성", "all": "모두", "unknown": "성별 확인 불가"}
    subjects = (result.get("subjects") or {}).get("people") or []
    scenes = result.get("scene_details") or []
    subject_genders = "\n".join(
        f"{person.get('subject_id')}: {presentation_labels.get(person.get('gender_presentation'), '확인 불가')}"
        for person in subjects
    )
    scene_worn_outfits = "\n".join(
        f"씬 {scene.get('scene_number')} - {outfit.get('subject_id')} "
        f"({presentation_labels.get(outfit.get('gender_presentation'), '확인 불가')}): "
        f"{', '.join(outfit.get('clothing_items') or [])}"
        for scene in scenes for outfit in scene.get("worn_outfits") or []
    )
    recommended_audience = "\n".join(
        f"{age_labels.get(item.get('age_group'), '연령 확인 불가')} "
        f"{audience_gender_labels.get(item.get('gender'), '성별 확인 불가')}: {item.get('evidence')}"
        for item in result.get("recommended_audience") or []
    )
    sheet.append([
        shortcode or "reel",
        analyzed_at,
        result.get("summary"),
        hook.get("description"),
        hook.get("strength"),
        result.get("body_structure"),
        result.get("content_type"),
        len(scenes),
        transcript,
        json.dumps(audio.get("background_music") or {}, ensure_ascii=False),
        selling_points,
        subject_genders,
        scene_worn_outfits,
        recommended_audience,
    ])
    temporary = path.with_name(path.stem + ".tmp.xlsx")
    try:
        workbook.save(temporary)
        os.replace(temporary, path)
    finally:
        workbook.close()
        temporary.unlink(missing_ok=True)


def save_result(shortcode: str, result: dict) -> str:
    with OUTPUT_LOCK:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        records = []
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                raise ValueError(f"누적 결과 파일은 JSON 배열이어야 합니다: {OUTPUT_FILE}")

        analyzed_at = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d, %H:%M:%S")
        records.append({
            "reel_id": shortcode or "reel",
            "analyzed_at": analyzed_at,
            "analysis": result,
        })
        temp_path = OUTPUT_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, OUTPUT_FILE)
        if WRITE_READABLE_OUTPUT:
            try:
                _append_readable_output(shortcode, analyzed_at, result)
            except Exception as e:
                print(f"  - XLSX 요약 저장 실패: {e}")
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
    result = analyze_video(
        pool,
        reel["video_bytes"],
        reel["mime_type"],
        reel.get("thumbnail_bytes"),
        reel.get("thumbnail_mime_type"),
    )

    print("3) 결과 저장 중...")
    filepath = save_result(reel["id"], result)
    print(f"  - 저장 완료: {filepath}")

    print_summary(result)


def process_safely(pool: GeminiKeyPool, reel_url: str) -> bool:
    original = reel_url
    reel_url = normalize_reel_url(reel_url)
    if not reel_url:
        print(f"[경고] Instagram Reel URL 또는 shortcode가 아닙니다: {original}\n")
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


def process_group_safely(pool: GeminiKeyPool, reel_urls: list[str]) -> int:
    if len(reel_urls) == 1:
        return int(process_safely(pool, reel_urls[0]))

    reels = []
    for index, reel_url in enumerate(reel_urls, 1):
        original = reel_url
        reel_url = normalize_reel_url(reel_url)
        if not reel_url:
            print(f"[경고] Instagram Reel URL 또는 shortcode가 아닙니다: {original}\n")
            continue
        try:
            print(f"\n[묶음 다운로드 {index}/{len(reel_urls)}] {reel_url}")
            reels.append(get_instagram_video(reel_url))
        except yt_dlp.utils.DownloadError as e:
            print(f"[오류] 영상을 가져오지 못했습니다: {e}\n")
        except requests.exceptions.RequestException as e:
            print(f"[오류] 영상 다운로드 중 네트워크 오류: {e}\n")
        except Exception as e:
            print(f"[오류] 영상 준비 중 예상치 못한 문제가 발생했습니다: {e}\n")

    if not reels:
        return 0

    try:
        print(f"\n[묶음 분석] 영상 {len(reels)}개를 Gemini 호출 1회로 처리합니다.")
        results = analyze_reels(pool, reels)
    except KeyPoolExhaustedError as e:
        print(f"[오류] {e}\n")
        return 0
    except Exception as e:
        print(f"[오류] 묶음 분석 중 예상치 못한 문제가 발생했습니다: {e}\n")
        return 0

    succeeded = 0
    for reel, result in zip(reels, results):
        try:
            filepath = save_result(reel["id"], result)
            print(f"  - {reel['id']} 저장 완료: {filepath}")
            print_summary(result)
            succeeded += 1
        except Exception as e:
            print(f"[오류] {reel['id']} 결과 저장 실패: {e}\n")
    return succeeded


def run_interactive(
    status_pool: GeminiKeyPool,
    worker_pools: list[GeminiKeyPool],
    group_size: int = 1,
):
    """URL 입력은 계속 받고, 각 워커가 자신의 키 풀 예약으로 분석한다."""
    work_queue = Queue()
    pending_urls = []

    def consume(worker_pool):
        while True:
            reel_urls = work_queue.get()
            try:
                if reel_urls is None:
                    return
                process_group_safely(worker_pool, list(reel_urls))
            finally:
                work_queue.task_done()

    def enqueue_pending():
        if pending_urls:
            work_queue.put(tuple(pending_urls))
            pending_urls.clear()

    workers = [
        threading.Thread(target=consume, args=(worker_pool,), name=f"gemini-worker-{i + 1}")
        for i, worker_pool in enumerate(worker_pools)
    ]
    for worker in workers:
        worker.start()

    print(f"Reel URL을 연속으로 입력할 수 있습니다. {group_size}개씩 한 번의 Gemini 호출로 처리합니다.")
    print("'flush' 입력 시 모인 URL 즉시 처리, 'status' 입력 시 사용량 확인, 종료는 'quit' 또는 'exit'.\n")
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
            if reel_url.lower() == "flush":
                enqueue_pending()
                continue
            if reel_url.lower() == "status":
                try:
                    print(status_pool.status())
                except RuntimeError as e:
                    print(f"[시트 오류] {e}\n")
                continue
            normalized_url = normalize_reel_url(reel_url)
            if not normalized_url:
                print(f"[경고] Instagram Reel URL 또는 shortcode가 아닙니다: {reel_url}\n")
                continue
            pending_urls.append(normalized_url)
            if len(pending_urls) == group_size:
                enqueue_pending()
                print(f"  - {group_size}개 묶음 대기열 추가 완료 (대기 묶음 {work_queue.qsize()}개)")
            else:
                print(f"  - 묶음 대기 중 ({len(pending_urls)}/{group_size})")
    finally:
        enqueue_pending()
        for _ in workers:
            work_queue.put(None)
        work_queue.join()
        for worker in workers:
            worker.join()


def main(argv=None):
    options = parse_args(argv)
    if options.model:
        os.environ["GEMINI_MODELS"] = options.model
    if options.seed is not None:
        os.environ["GEMINI_SEED"] = str(options.seed)
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
    if os.getenv("GEMINI_SEED", "").strip():
        print(f"고정 seed: {os.environ['GEMINI_SEED']}")
    print(f"Gemini 호출당 영상 수: {options.group_size}")

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
        for start in range(0, len(urls), options.group_size):
            group = urls[start:start + options.group_size]
            print(f"\n[XLSX {start + 1}-{start + len(group)}/{len(urls)}]")
            succeeded += process_group_safely(pool, group)
        print(f"\n[XLSX 완료] 성공 {succeeded}건 / 실패 {len(urls) - succeeded}건 / 전체 {len(urls)}건")
        return

    sheets_mode = os.getenv("GEMINI_POOL_MODE", "sheets").strip().lower() == "sheets"
    worker_count = INTERACTIVE_WORKERS if sheets_mode else 1
    try:
        worker_pools = [create_pool() for _ in range(worker_count)]
    except RuntimeError as e:
        print(f"[설정 오류] {e}")
        return
    run_interactive(pool, worker_pools, options.group_size)


if __name__ == "__main__":
    main()
