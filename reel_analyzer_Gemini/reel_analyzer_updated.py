"""
Instagram Reels Analyzer (CLI)

터미널에 Instagram Reel URL을 입력하면
1) yt-dlp로 실제 영상 URL을 얻고 영상을 메모리에 로드한 뒤
2) Gemini API로 구조화된 영상 분석을 수행하고
3) 코드로 계산 가능한 파생 지표를 추가해서
4) output/ 폴더에 JSON 파일로 저장합니다.

사용법:
    1. .env.example 을 .env 로 복사하고 GEMINI_API_KEY를 입력
    2. pip install -r requirements.txt
    3. python reel_analyzer.py

주의:
    - 음원명 / 가수 / original audio 여부 등 Instagram audio metadata는
      이 분석기에서 추측하지 않습니다. 별도 수집 데이터와 결합하세요.
    - Gemini는 의미/맥락 분석을 담당하고, 코드로 계산 가능한 값은
      가능한 한 후처리 단계에서 계산합니다.
"""

import os
import sys
import json
import time
import tempfile
from collections import Counter
from datetime import datetime
from typing import Literal, Optional

import requests
import yt_dlp
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from google import genai
from google.genai import types


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

INLINE_SIZE_LIMIT_MB = int(os.getenv("INLINE_SIZE_LIMIT_MB", "90"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))
RETRY_BASE_DELAY_SEC = int(os.getenv("RETRY_BASE_DELAY_SEC", "5"))
SCENE_ANALYSIS_FPS = float(os.getenv("SCENE_ANALYSIS_FPS", "3"))
FILES_API_PROCESSING_TIMEOUT_SEC = int(
    os.getenv("FILES_API_PROCESSING_TIMEOUT_SEC", "300")
)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


# ---------------------------------------------------------------------------
# 공통 타입 / Structured Output Schema
# ---------------------------------------------------------------------------

Level = Literal["low", "medium", "high", "unknown"]

ShotSize = Literal[
    "extreme_close_up",
    "close_up",
    "medium_close_up",
    "medium",
    "medium_full",
    "full",
    "wide",
    "extreme_wide",
    "unknown",
]

CameraAngle = Literal[
    "eye_level",
    "high_angle",
    "low_angle",
    "top_down",
    "dutch_angle",
    "pov",
    "unknown",
]

CameraMovement = Literal[
    "fixed",
    "pan",
    "tilt",
    "zoom_in",
    "zoom_out",
    "dolly_in",
    "dolly_out",
    "tracking",
    "handheld",
    "digital_zoom",
    "subject_approach",
    "mixed",
    "unknown",
]

SubjectPosition = Literal[
    "center",
    "left",
    "right",
    "upper",
    "lower",
    "moving",
    "multiple",
    "unknown",
]

TransitionType = Literal[
    "cut",
    "jump_cut",
    "match_cut",
    "fade",
    "dissolve",
    "wipe",
    "zoom_transition",
    "whip_transition",
    "none",
    "unknown",
]


class CompositionAnalysis(BaseModel):
    subject_position: SubjectPosition
    subject_screen_occupancy: Level = Field(
        description=(
            "주 피사체가 화면을 차지하는 정도를 low/medium/high로 분류. "
            "정밀한 비율을 추측하지 않는다."
        )
    )
    symmetry: Optional[bool] = None
    rule_of_thirds: Optional[bool] = None
    headroom: Literal["tight", "normal", "large", "unknown"]
    negative_space: Level
    background_complexity: Level


class CameraAnalysis(BaseModel):
    main_shot_size: ShotSize
    main_angle: CameraAngle
    movement_types: list[CameraMovement]
    composition: CompositionAnalysis
    description: str
    confidence: float = Field(ge=0.0, le=1.0)


class HookAnalysis(BaseModel):
    description: str
    strength: Literal["weak", "medium", "strong"]
    duration_seconds: Optional[float] = Field(default=None, ge=0.0)
    types: list[
        Literal[
            "visual_hook",
            "spoken_hook",
            "text_hook",
            "question",
            "curiosity_gap",
            "product_reveal",
            "before_after",
            "transformation",
            "unexpected_action",
            "movement_hook",
            "result_first",
            "problem_hook",
            "other",
        ]
    ]
    first_action_seconds: Optional[float] = Field(default=None, ge=0.0)
    first_text_seconds: Optional[float] = Field(default=None, ge=0.0)
    first_speech_seconds: Optional[float] = Field(default=None, ge=0.0)
    curiosity_gap: Optional[bool] = None
    before_after_structure: Optional[bool] = None
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class TransitionEvent(BaseModel):
    timestamp_seconds: Optional[float] = Field(default=None, ge=0.0)
    type: TransitionType
    confidence: float = Field(ge=0.0, le=1.0)


class EditingAnalysis(BaseModel):
    styles: list[str]
    pace: Literal["slow", "medium", "fast"]
    transitions: list[TransitionEvent]
    rhythm_description: str
    confidence: float = Field(ge=0.0, le=1.0)


class SubtitleStyle(BaseModel):
    font_weight: Literal["light", "regular", "bold", "unknown"]
    text_color: Optional[str] = None
    outline: Optional[bool] = None
    background_box: Optional[bool] = None
    animation: Literal[
        "none",
        "fade",
        "pop",
        "slide",
        "typewriter",
        "bounce",
        "mixed",
        "unknown",
    ]


class SubtitleAnalysis(BaseModel):
    exists: bool
    main_position: Literal[
        "top",
        "upper_center",
        "center",
        "lower_center",
        "bottom",
        "mixed",
        "unknown",
    ]
    style: Optional[SubtitleStyle] = None
    speech_synced: Optional[bool] = None
    text_density: Level
    confidence: float = Field(ge=0.0, le=1.0)


class HumanActionAnalysis(BaseModel):
    person_present: bool
    person_count: int = Field(ge=0)
    actions: list[str]
    gestures: list[str]
    eye_contact: Optional[bool] = None
    facial_expression: Optional[str] = None
    interaction_with_object: Optional[bool] = None
    confidence: float = Field(ge=0.0, le=1.0)


class LightingAnalysis(BaseModel):
    type: Literal[
        "natural",
        "indoor_soft",
        "indoor_hard",
        "studio",
        "mixed",
        "low_light",
        "unknown",
    ]
    consistency: Literal["consistent", "changing", "unknown"]


class BackgroundAnalysis(BaseModel):
    type: str
    complexity: Level
    description: str


class VisualStyleAnalysis(BaseModel):
    brightness: Level
    contrast: Level
    saturation: Level
    color_temperature: Literal["warm", "neutral", "cool", "mixed", "unknown"]
    dominant_colors: list[str]
    lighting: LightingAnalysis
    background: BackgroundAnalysis
    overall_aesthetic: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


class ProductItem(BaseModel):
    name_or_description: str
    first_appearance_seconds: Optional[float] = Field(default=None, ge=0.0)
    exposure_types: list[
        Literal[
            "held_by_creator",
            "worn",
            "close_up",
            "background",
            "demonstration",
            "static_display",
            "other",
        ]
    ]
    visual_emphasis: Level
    close_up: Optional[bool] = None
    centered: Optional[bool] = None
    confidence: float = Field(ge=0.0, le=1.0)


class ProductExposureAnalysis(BaseModel):
    products_present: bool
    items: list[ProductItem]


class SoundEffect(BaseModel):
    timestamp_seconds: Optional[float] = Field(default=None, ge=0.0)
    type: str


class AudioUsageAnalysis(BaseModel):
    speech_present: bool
    bgm_present: bool
    sound_effects_present: bool
    speech_style: Optional[str] = None
    bgm_start_seconds: Optional[float] = Field(default=None, ge=0.0)
    sound_effects: list[SoundEffect]
    beat_synced_editing: Optional[bool] = None
    sync_strength: Optional[Level] = None
    audio_roles: list[
        Literal[
            "energy",
            "transition",
            "reveal_emphasis",
            "mood",
            "narration_support",
            "comedic_effect",
            "other",
        ]
    ]
    confidence: float = Field(ge=0.0, le=1.0)


class ContentStructureAnalysis(BaseModel):
    pattern: list[
        Literal[
            "hook",
            "setup",
            "body",
            "demonstration",
            "reveal",
            "comparison",
            "payoff",
            "cta",
            "ending",
        ]
    ]
    hook_end_seconds: Optional[float] = Field(default=None, ge=0.0)
    main_reveal_seconds: Optional[float] = Field(default=None, ge=0.0)
    has_result_reveal: bool
    has_loop_structure: bool
    has_cta: bool
    cta_type: Optional[
        Literal[
            "follow",
            "comment",
            "share",
            "save",
            "purchase",
            "visit_profile",
            "link",
            "implicit",
            "other",
        ]
    ] = None
    ending_type: str
    confidence: float = Field(ge=0.0, le=1.0)


class AttentionAnalysis(BaseModel):
    opening_attention_level: Level
    triggers: list[
        Literal[
            "rapid_motion",
            "camera_motion",
            "object_interaction",
            "spoken_hook",
            "text_overlay",
            "rapid_cuts",
            "gesture",
            "facial_expression",
            "product_reveal",
            "transformation",
            "surprise",
            "sound_effect",
            "before_after",
            "other",
        ]
    ]
    dominant_trigger: Optional[str] = None
    visual_stimulation: Level
    description: str
    confidence: float = Field(ge=0.0, le=1.0)


class SafeZoneAnalysis(BaseModel):
    main_subject_safe: Optional[bool] = None
    key_text_safe: Optional[bool] = None
    ui_overlap_risk: Level
    risk_area: Optional[str] = None
    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)


class LoopAnalysis(BaseModel):
    loopable: bool
    loop_type: Literal[
        "visual_loop",
        "narrative_loop",
        "audio_loop",
        "none",
        "unknown",
    ]
    ending_start_relation: Literal[
        "very_similar",
        "similar",
        "different",
        "unknown",
    ]
    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)


class SceneAnalysis(BaseModel):
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(ge=0.0)
    visual_description: str
    purpose: str
    shot_size: ShotSize
    camera_angle: CameraAngle
    camera_movement: CameraMovement
    subject_position: SubjectPosition
    motion_intensity: Level
    human_actions: list[str]
    gestures: list[str]
    product_visible: bool
    text_visible: bool
    text_content: Optional[str] = None
    speech_present: bool
    bgm_present: bool
    transition_in: TransitionType
    emotional_tone: str
    confidence: float = Field(ge=0.0, le=1.0)


class MarketingAnalysis(BaseModel):
    value_proposition: Optional[str] = None
    strengths: list[str]
    weaknesses: list[str]
    notable_elements: list[str]


class ReelVideoAnalysis(BaseModel):
    summary: str
    content_type: str
    hook: HookAnalysis
    camera: CameraAnalysis
    editing: EditingAnalysis
    subtitle: SubtitleAnalysis
    human_action: HumanActionAnalysis
    visual_style: VisualStyleAnalysis
    product_exposure: ProductExposureAnalysis
    audio_usage: AudioUsageAnalysis
    content_structure: ContentStructureAnalysis
    attention_analysis: AttentionAnalysis
    safe_zone_analysis: SafeZoneAnalysis
    loop_analysis: LoopAnalysis
    scenes: list[SceneAnalysis]
    marketing_analysis: MarketingAnalysis


# ---------------------------------------------------------------------------
# API 키 확인
# ---------------------------------------------------------------------------


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
# 1. Instagram 영상 획득
# ---------------------------------------------------------------------------


def get_instagram_video(reel_url: str) -> dict:
    """
    Instagram Reel을 디스크에 영구 저장하지 않고
    실제 영상 URL을 추출한 뒤 메모리에 올린다.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": (
            "best[ext=mp4][vcodec!=none][acodec!=none]/"
            "best[vcodec!=none][acodec!=none]/best"
        ),
        # 비공개 계정/로그인 필요 시 아래 주석을 해제하고 브라우저를 지정하세요.
        # "cookiesfrombrowser": ("edge", None, None, None),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(reel_url, download=False)

    video_url = info.get("url")
    if not video_url:
        raise RuntimeError("Instagram 영상의 실제 media URL을 찾지 못했습니다.")

    headers = info.get("http_headers", {})

    print(f"  - shortcode : {info.get('id')}")
    print(f"  - duration  : {info.get('duration')}초")

    # 메모리에서만 사용. 파일로 영구 저장하지 않는다.
    response = requests.get(video_url, headers=headers, timeout=(10, 90))
    response.raise_for_status()

    mime_type = response.headers.get("Content-Type", "video/mp4").split(";")[0]
    if not mime_type.startswith("video/"):
        mime_type = "video/mp4"

    return {
        "id": info.get("id"),
        "video_bytes": response.content,
        "mime_type": mime_type,
        "metadata": {
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "format_id": info.get("format_id"),
            "ext": info.get("ext"),
        },
    }


# ---------------------------------------------------------------------------
# 2. Gemini 분석 프롬프트
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """
이 Instagram Reel 영상을 숏폼 콘텐츠 분석용 데이터셋 관점에서 분석하세요.

목표는 단순한 영상 요약이 아니라, 수백~수천 개의 릴스를 서로 비교하고
조회수/좋아요/댓글/공유/저장 등의 외부 성과 데이터와 결합할 수 있는
일관된 영상 feature를 만드는 것입니다.

반드시 다음 원칙을 지키세요.

[공통 원칙]
1. 영상에서 직접 관찰할 수 있는 정보만 분석하세요.
2. 설명형 필드는 한국어로 작성하고, enum/category 값은 schema에 정의된 영문 값을 그대로 사용하세요.
3. 확실하지 않은 정보는 억지로 추측하지 말고 null 또는 unknown을 사용하세요.
4. 정밀하게 측정할 수 없는 값을 임의의 소수점 숫자로 만들어내지 마세요.
5. confidence는 영상의 성과나 품질 점수가 아니라 해당 해석에 대한 확신도입니다.
6. 성과(조회수, 좋아요 등)를 예측하지 말고 관찰 가능한 creative feature만 분석하세요.

[절대 추측하지 말 것]
- Instagram 조회수 / 좋아요 / 댓글 / 공유 / 저장 수
- creator follower 수
- 업로드 날짜
- caption / hashtag
- 곡 제목
- 가수명
- Instagram audio ID
- original audio 여부
- 음원의 인기 여부

위 Instagram/audio metadata는 다른 수집기에서 별도로 수집합니다.

[Audio 분석]
음악의 정체를 맞히지 말고 영상 안에서 어떻게 사용되는지만 분석하세요.
예: speech 존재 여부, BGM 존재 여부, 효과음, BGM 시작 시점,
컷/전환과 음악의 동기화, reveal 강조 여부 등.

[Camera / Composition]
- shot size, angle, movement, subject position을 schema enum에 맞춰 분류하세요.
- 카메라가 고정되어 있고 인물이 렌즈 쪽으로 다가오면 카메라 줌으로 오판하지 마세요.
- 화면 점유율을 정밀 비율로 추측하지 말고 low/medium/high 수준으로 분류하세요.

[Hook]
첫 3초를 중심으로 분석하되 실제 Hook이 더 짧거나 길다면 관찰 결과에 맞추세요.
시선을 끄는 요소를 visual/text/spoken/movement/transformation/result-first 등의
구조적인 feature로 분리하세요.

[Scene]
1. 실제 의미 있는 시각/편집 변화 기준으로 scene을 나누세요.
2. 컷, 구도 변화, 새로운 텍스트 등장, 핵심 행동 변화가 scene 경계가 될 수 있습니다.
3. 단순히 1초 간격으로 기계적으로 나누지 마세요.
4. 영상 전체 시간을 처음부터 끝까지 가능한 한 빠짐없이 커버하세요.
5. start_seconds와 end_seconds는 실제 재생 시간을 기준으로 작성하세요.
6. end_seconds는 반드시 start_seconds 이상이어야 합니다.
7. visual_description에는 실제 동작, 표정, 소품, 배경 등 관찰 내용을 구체적으로 적으세요.
8. human_actions / gestures는 가능하면 짧고 재사용 가능한 표현을 사용하세요.

[Editing]
- fast/medium/slow pace와 편집 스타일을 분석하세요.
- 실제 컷 수, 평균 scene 길이, 컷 빈도 등 계산 가능한 값은 후처리 코드가 계산하므로
  임의의 통계 수치를 만들지 마세요.

[Subtitle / On-screen text]
- 텍스트가 실제 보이는 경우만 기록하세요.
- OCR이 불확실하면 단어를 만들어내지 말고 text_content를 null로 둘 수 있습니다.
- 위치, 굵기, 테두리/박스, 애니메이션 스타일 등을 관찰하세요.

[Product]
- 브랜드나 정확한 모델이 명확하게 보이거나 영상에서 직접 언급된 경우가 아니면
  브랜드명을 추측하지 말고 외형/종류 중심으로 설명하세요.

[Visual style]
밝기, 대비, 채도, 색온도, 조명, 배경 복잡도, 전체 aesthetic을 일관된 기준으로 분류하세요.

[Attention]
초반에 시선을 끄는 장치를 구체적으로 분리하세요.
예: 빠른 움직임, 오브젝트 상호작용, 자막, 음성, 빠른 컷, 제스처,
제품 공개, 변신, 놀라움, 효과음, before/after.

[Safe zone]
Instagram/Reels와 같은 세로형 숏폼 UI가 화면 가장자리와 하단/우측 일부를 덮을 수 있다는
관점에서 중요한 피사체나 텍스트가 UI와 충돌할 위험이 있는지 정성적으로 분석하세요.
정확한 픽셀 좌표나 공식 safe-zone 수치를 만들어내지 마세요.

[Loop]
영상의 시작과 끝이 시각적/서사적/오디오적으로 자연스럽게 이어지도록 설계되었는지 분석하세요.
단순히 시작/끝이 비슷해 보인다는 이유만으로 loop라고 단정하지 마세요.

[Content structure]
hook → setup/body → demonstration/reveal/payoff → CTA/ending 등의 실제 구조를 분석하세요.
CTA는 직접적인 행동 유도 표현이 있을 때만 true로 판단하세요.
"""


RETRYABLE_MARKERS = (
    "503",
    "429",
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "overloaded",
    "deadline",
    "timeout",
)


# ---------------------------------------------------------------------------
# 3. Gemini 호출 / 재시도 / 검증
# ---------------------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker.lower() in msg for marker in RETRYABLE_MARKERS)


def _call_with_retry(fn, *args, **kwargs):
    """일시적 오류가 나면 지수 백오프로 재시도한다."""
    last_exc = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc

            if not _is_retryable(exc) or attempt == MAX_RETRIES:
                raise

            delay = min(RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)), 60)
            print(
                f"  - [재시도 {attempt}/{MAX_RETRIES}] "
                f"일시적 API 오류입니다. {delay}초 후 재시도합니다..."
            )
            time.sleep(delay)

    raise last_exc


def _generate_config() -> types.GenerateContentConfig:
    """모든 호출에서 동일한 Structured Output schema를 사용한다."""
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ReelVideoAnalysis,
        temperature=0.2,
    )


def _parse_and_validate_response(response) -> dict:
    """Gemini 응답을 Pydantic schema로 검증하고 일반 dict로 반환한다."""
    if not response.text:
        raise RuntimeError("Gemini 응답이 비어 있습니다.")

    try:
        parsed = ReelVideoAnalysis.model_validate_json(response.text)
    except ValidationError as exc:
        raise RuntimeError(f"Gemini 분석 결과 schema 검증 실패: {exc}") from exc

    result = parsed.model_dump()
    _validate_scene_times(result)
    return result


def _validate_scene_times(result: dict):
    """후처리 가능한 수준의 기본 scene 시간 검증."""
    for index, scene in enumerate(result.get("scenes", []), start=1):
        start = scene.get("start_seconds")
        end = scene.get("end_seconds")

        if start is None or end is None:
            raise RuntimeError(f"scene #{index}의 시간이 누락되었습니다.")

        if end < start:
            raise RuntimeError(
                f"scene #{index}의 end_seconds({end})가 "
                f"start_seconds({start})보다 작습니다."
            )


def _analyze_inline(client: genai.Client, video_bytes: bytes, mime_type: str) -> dict:
    """작은 영상: inline data로 바로 전송."""
    video_part = types.Part(
        inline_data=types.Blob(data=video_bytes, mime_type=mime_type),
        video_metadata=types.VideoMetadata(fps=SCENE_ANALYSIS_FPS),
    )

    response = _call_with_retry(
        client.models.generate_content,
        model=GEMINI_MODEL,
        contents=types.Content(
            parts=[
                video_part,
                types.Part(text=ANALYSIS_PROMPT),
            ]
        ),
        config=_generate_config(),
    )

    return _parse_and_validate_response(response)


def _analyze_via_files_api(
    client: genai.Client,
    video_bytes: bytes,
    mime_type: str,
) -> dict:
    """큰 영상: 임시 파일 → Gemini Files API → 분석 → 로컬/원격 파일 삭제."""
    tmp_path = None
    uploaded_file = None

    try:
        suffix_map = {
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
        }
        suffix = suffix_map.get(mime_type, ".mp4")

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

        print("  - 영상이 커서 Files API로 업로드합니다...")
        uploaded_file = client.files.upload(file=tmp_path)

        started_at = time.monotonic()

        while uploaded_file.state and uploaded_file.state.name == "PROCESSING":
            if time.monotonic() - started_at > FILES_API_PROCESSING_TIMEOUT_SEC:
                raise TimeoutError("Gemini Files API 처리 대기 시간이 초과되었습니다.")

            print("  - 파일 처리 중... (5초 후 재확인)")
            time.sleep(5)
            uploaded_file = client.files.get(name=uploaded_file.name)

        if not uploaded_file.state or uploaded_file.state.name != "ACTIVE":
            state_name = uploaded_file.state.name if uploaded_file.state else "UNKNOWN"
            raise RuntimeError(f"Gemini Files API 처리 실패: state={state_name}")

        file_part = types.Part(
            file_data=types.FileData(
                file_uri=uploaded_file.uri,
                mime_type=uploaded_file.mime_type,
            ),
            video_metadata=types.VideoMetadata(fps=SCENE_ANALYSIS_FPS),
        )

        response = _call_with_retry(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=types.Content(
                parts=[
                    file_part,
                    types.Part(text=ANALYSIS_PROMPT),
                ]
            ),
            config=_generate_config(),
        )

        return _parse_and_validate_response(response)

    finally:
        # 로컬 임시 파일 삭제
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

        # Gemini Files API에 업로드한 파일도 분석이 끝나면 정리
        if uploaded_file and uploaded_file.name:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception as exc:
                print(f"  - [경고] Gemini 업로드 파일 삭제 실패: {exc}")


def analyze_video(
    client: genai.Client,
    video_bytes: bytes,
    mime_type: str = "video/mp4",
) -> dict:
    size_mb = len(video_bytes) / (1024 * 1024)
    print(f"  - 영상 용량 : {size_mb:.2f} MB")
    print(f"  - 분석 FPS   : {SCENE_ANALYSIS_FPS}")
    print(f"  - Gemini     : {GEMINI_MODEL}")

    if size_mb > INLINE_SIZE_LIMIT_MB:
        return _analyze_via_files_api(client, video_bytes, mime_type)

    return _analyze_inline(client, video_bytes, mime_type)


# ---------------------------------------------------------------------------
# 4. 프로그램에서 계산할 파생 지표
# ---------------------------------------------------------------------------


def _distribution(values: list[str]) -> dict:
    if not values:
        return {}

    counter = Counter(values)
    total = len(values)

    return {
        key: round(count / total, 4)
        for key, count in sorted(counter.items())
    }


def _safe_ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _add_scene_display_fields(analysis: dict):
    """
    사람이 읽기 편한 timestamp / duration을 모델에게 계산시키지 않고 코드에서 만든다.
    """
    for scene in analysis.get("scenes", []):
        start = float(scene["start_seconds"])
        end = float(scene["end_seconds"])

        scene["duration_seconds"] = round(max(0.0, end - start), 3)
        scene["timestamp"] = f"{_format_seconds(start)}-{_format_seconds(end)}"


def _format_seconds(value: float) -> str:
    total_ms = int(round(value * 1000))
    minutes, remainder_ms = divmod(total_ms, 60_000)
    seconds = remainder_ms / 1000

    if total_ms % 1000 == 0:
        return f"{minutes:02d}:{int(seconds):02d}"

    return f"{minutes:02d}:{seconds:05.2f}"


def calculate_derived_metrics(
    analysis: dict,
    source_duration: Optional[float],
) -> dict:
    scenes = analysis.get("scenes", [])
    scene_count = len(scenes)

    scene_durations = [
        max(0.0, float(scene["end_seconds"]) - float(scene["start_seconds"]))
        for scene in scenes
    ]

    # 첫 scene의 transition_in은 컷으로 계산하지 않는다.
    transition_scenes = [
        scene
        for idx, scene in enumerate(scenes)
        if idx > 0 and scene.get("transition_in") not in (None, "none", "unknown")
    ]

    if scene_durations:
        avg_scene_duration = sum(scene_durations) / len(scene_durations)
        shortest_scene = min(scene_durations)
        longest_scene = max(scene_durations)
    else:
        avg_scene_duration = None
        shortest_scene = None
        longest_scene = None

    duration = source_duration
    if not duration and scenes:
        duration = max(float(scene["end_seconds"]) for scene in scenes)

    estimated_cut_count = len(transition_scenes)

    cuts_per_second = (
        estimated_cut_count / duration
        if duration and duration > 0
        else None
    )

    text_scene_count = sum(bool(scene.get("text_visible")) for scene in scenes)
    product_scene_count = sum(bool(scene.get("product_visible")) for scene in scenes)
    speech_scene_count = sum(bool(scene.get("speech_present")) for scene in scenes)
    bgm_scene_count = sum(bool(scene.get("bgm_present")) for scene in scenes)

    result = {
        "scene_count": scene_count,
        "estimated_transition_count": estimated_cut_count,
        "average_scene_duration_seconds": (
            round(avg_scene_duration, 3) if avg_scene_duration is not None else None
        ),
        "shortest_scene_duration_seconds": (
            round(shortest_scene, 3) if shortest_scene is not None else None
        ),
        "longest_scene_duration_seconds": (
            round(longest_scene, 3) if longest_scene is not None else None
        ),
        "transitions_per_second": (
            round(cuts_per_second, 4) if cuts_per_second is not None else None
        ),
        "text_scene_ratio": _safe_ratio(text_scene_count, scene_count),
        "product_scene_ratio": _safe_ratio(product_scene_count, scene_count),
        "speech_scene_ratio": _safe_ratio(speech_scene_count, scene_count),
        "bgm_scene_ratio": _safe_ratio(bgm_scene_count, scene_count),
        "shot_size_distribution": _distribution(
            [scene.get("shot_size", "unknown") for scene in scenes]
        ),
        "camera_angle_distribution": _distribution(
            [scene.get("camera_angle", "unknown") for scene in scenes]
        ),
        "camera_movement_distribution": _distribution(
            [scene.get("camera_movement", "unknown") for scene in scenes]
        ),
        "subject_position_distribution": _distribution(
            [scene.get("subject_position", "unknown") for scene in scenes]
        ),
        "motion_intensity_distribution": _distribution(
            [scene.get("motion_intensity", "unknown") for scene in scenes]
        ),
        "transition_distribution": _distribution(
            [scene.get("transition_in", "unknown") for scene in transition_scenes]
        ),
    }

    hook_duration = analysis.get("hook", {}).get("duration_seconds")
    result["hook_duration_ratio"] = (
        round(hook_duration / duration, 4)
        if duration and duration > 0 and hook_duration is not None
        else None
    )

    reveal_seconds = analysis.get("content_structure", {}).get("main_reveal_seconds")
    result["reveal_position_ratio"] = (
        round(reveal_seconds / duration, 4)
        if duration and duration > 0 and reveal_seconds is not None
        else None
    )

    return result


def build_technical_metadata(metadata: dict) -> dict:
    width = metadata.get("width")
    height = metadata.get("height")

    aspect_ratio = None
    if width and height:
        aspect_ratio = round(width / height, 4)

    return {
        "duration_seconds": metadata.get("duration"),
        "width": width,
        "height": height,
        "source_fps": metadata.get("fps"),
        "aspect_ratio": aspect_ratio,
        "format_id": metadata.get("format_id"),
        "file_extension": metadata.get("ext"),
        "analysis_fps": SCENE_ANALYSIS_FPS,
        "gemini_model": GEMINI_MODEL,
    }


# ---------------------------------------------------------------------------
# 5. 결과 저장 / 출력
# ---------------------------------------------------------------------------


def build_final_result(reel_url: str, reel: dict, analysis: dict) -> dict:
    _add_scene_display_fields(analysis)

    return {
        "source": {
            "platform": "instagram",
            "reel_id": reel.get("id"),
            "url": reel_url,
        },
        "technical_metadata": build_technical_metadata(reel.get("metadata", {})),
        "video_analysis": analysis,
        "derived_video_metrics": calculate_derived_metrics(
            analysis,
            reel.get("metadata", {}).get("duration"),
        ),
        "external_data_note": {
            "instagram_metrics": "별도 수집 데이터와 결합",
            "audio_metadata": "곡명/가수/original audio 여부는 별도 수집 데이터와 결합",
        },
    }


def save_result(shortcode: str, result: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{shortcode or 'reel'}_{timestamp}.json"
    filepath = os.path.join(OUTPUT_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as file:
        json.dump(result, file, indent=2, ensure_ascii=False)

    return filepath


def print_summary(result: dict):
    analysis = result.get("video_analysis", {})
    hook = analysis.get("hook", {})
    derived = result.get("derived_video_metrics", {})

    print("\n" + "=" * 60)
    print("[분석 요약]")
    print("-" * 60)
    print(f"요약        : {analysis.get('summary')}")
    print(f"Hook        : {hook.get('description')} (강도: {hook.get('strength')})")
    print(f"콘텐츠 유형 : {analysis.get('content_type')}")
    print(f"Scene 수    : {derived.get('scene_count')}")
    print(f"평균 구간   : {derived.get('average_scene_duration_seconds')}초")
    print(f"전환/초     : {derived.get('transitions_per_second')}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# 6. 메인 루프
# ---------------------------------------------------------------------------


def process_one(client: genai.Client, reel_url: str):
    print(f"\n[처리 시작] {reel_url}")

    print("1) 영상 가져오는 중...")
    reel = get_instagram_video(reel_url)

    print("2) Gemini 구조화 분석 중...")
    analysis = analyze_video(client, reel["video_bytes"], reel["mime_type"])

    print("3) 파생 지표 계산 및 결과 저장 중...")
    result = build_final_result(reel_url, reel, analysis)
    filepath = save_result(reel["id"], result)
    print(f"  - 저장 완료: {filepath}")

    # bytes는 결과 JSON에 넣지 않고 더 이상 필요 없으므로 참조 제거
    reel.pop("video_bytes", None)

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
        except yt_dlp.utils.DownloadError as exc:
            print(f"[오류] 영상을 가져오지 못했습니다: {exc}")
            print("      비공개 계정, 삭제된 게시물, 로그인 필요 여부를 확인하세요.\n")
        except requests.exceptions.RequestException as exc:
            print(f"[오류] 영상 가져오기 중 네트워크 오류: {exc}\n")
        except Exception as exc:
            print(f"[오류] 분석 중 예상치 못한 문제가 발생했습니다: {exc}\n")


if __name__ == "__main__":
    main()
