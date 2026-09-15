"""
key_pool.py

여러 개의 Gemini API 키 x 여러 개의 모델을 하나의 풀(pool)로 관리한다.

- .env의 GEMINI_API_KEYS ({별칭:키,...})로 키를 몇 개든 추가할 수 있다.
- .env의 GEMINI_MODELS (쉼표 구분)로 사용할 모델을 몇 개든 지정할 수 있다.
- model_limits.json 에 모델별 RPM/RPD 한도를 적어두면, 그 한도를 넘기지 않는 선에서
  현재 조합을 유지하고 사용 불가 시 같은 키의 다음 모델, 다음 키 순서로 전환한다.
- 429 중 일일 제한만 소진 처리하고, 분당/토큰 제한은 잠시 대기 후 재사용한다.
- 사용량은 gemini_usage_state.json 파일에 저장되어 프로그램을 껐다 켜도 유지된다.

주의: Gemini API의 비율 제한(rate limit)은 "API 키" 단위가 아니라 "프로젝트" 단위로 걸립니다.
      키 여러 개를 써도 같은 프로젝트 소속이면 한도가 공유되어 로테이션 효과가 없습니다.
      각 키가 서로 다른 프로젝트에서 발급된 것인지 꼭 확인하세요.
"""

import os
import json
import time
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]
STATE_PATH = BASE_DIR / "gemini_usage_state.json"
LIMITS_PATH = BASE_DIR / "model_limits.json"

# 일일 한도(RPD)는 태평양 시간 자정에 초기화됩니다. (Gemini API 공식 정책)
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

# model_limits.json이 없을 때 사용할 기본값.
# 실제 한도는 모델/계정마다 다를 수 있으니, AI Studio > Usage 페이지에서
# 확인한 뒤 model_limits.json을 직접 만들어 맞는 값으로 수정하는 걸 권장합니다.
DEFAULT_LIMITS = {
    "gemini-3.5-flash": {"rpm": 5, "rpd": 20},
    "gemini-3.6-flash": {"rpm": 5, "rpd": 20},
    "gemini-3.7-flash": {"rpm": 5, "rpd": 20},
}


def _load_limits() -> dict:
    if LIMITS_PATH.exists():
        with open(LIMITS_PATH, "r", encoding="utf-8") as f:
            user_limits = json.load(f)
        merged = dict(DEFAULT_LIMITS)
        merged.update(user_limits)
        return merged

    # 최초 실행 시 기본값으로 파일을 만들어줘서, 사용자가 바로 열어 수정할 수 있게 한다.
    with open(LIMITS_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_LIMITS, f, indent=2, ensure_ascii=False)
    return dict(DEFAULT_LIMITS)


def _today_str() -> str:
    return datetime.now(PACIFIC_TZ).strftime("%Y-%m-%d")


def _load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


@dataclass
class Combo:
    key_label: str
    api_key: str
    model: str

    @property
    def combo_id(self) -> str:
        return f"{self.key_label}:{self.model}"


class KeyPoolExhaustedError(Exception):
    """모든 (키, 모델) 조합이 오늘 한도를 다 써서 더 이상 쓸 수 없을 때."""


class GeminiKeyPool:
    def __init__(self):
        self.keys = self._load_keys()
        self.models = self._load_models()
        self.limits = _load_limits()
        self.state = _load_state()

        self.combos = [
            Combo(key_label=label, api_key=key, model=model)
            for label, key in self.keys
            for model in self.models
        ]
        if not self.combos:
            raise RuntimeError("사용 가능한 (API 키, 모델) 조합이 없습니다. .env를 확인하세요.")

        self._rr_index = 0  # 성공한 조합을 유지하는 포인터
        self._disabled = set()

    # -----------------------------------------------------------------
    # 설정 로드
    # -----------------------------------------------------------------

    @staticmethod
    def _load_keys() -> list:
        """
        큰따옴표로 감싼 GEMINI_API_KEYS의 중괄호 안에서 키를 한 줄씩 읽는다.
        기존 쉼표 목록과 GEMINI_API_KEY_LABELS 조합도 계속 지원한다.
        """
        raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY", "")
        raw = raw.strip()
        if raw.startswith("{") or raw.endswith("}"):
            if not (raw.startswith("{") and raw.endswith("}")):
                raise RuntimeError("GEMINI_API_KEYS는 큰따옴표로 감싼 {별칭:API키,...} 형식으로 입력하세요.")
            if not raw[1:-1].strip():
                return []
            result = []
            for entry in raw[1:-1].split(","):
                alias, separator, key = entry.partition(":")
                alias, key = alias.strip(), key.strip()
                if not separator or not alias or not key or any(c in alias for c in "{}:, "):
                    raise RuntimeError("GEMINI_API_KEYS는 큰따옴표로 감싼 {별칭:API키,...} 형식으로 입력하세요.")
                result.append((alias, key))
            if len(dict(result)) != len(result):
                raise RuntimeError("GEMINI_API_KEYS의 별칭이 중복되었습니다.")
            return result

        keys = [k.strip() for k in raw.split(",") if k.strip()]

        raw_labels = os.getenv("GEMINI_API_KEY_LABELS", "")
        labels = [l.strip() for l in raw_labels.split(",") if l.strip()]

        result = []
        for i, key in enumerate(keys):
            label = labels[i] if i < len(labels) else f"key{i + 1}"
            result.append((label, key))
        return result

    @staticmethod
    def _load_models() -> list:
        raw = os.getenv(
            "GEMINI_MODELS",
            os.getenv("GEMINI_MODEL") or "gemini-3.5-flash,gemini-3.6-flash,gemini-3.7-flash",
        )
        return [m.strip() for m in raw.split(",") if m.strip()]

    # -----------------------------------------------------------------
    # 사용량 추적
    # -----------------------------------------------------------------

    def _combo_state(self, combo: Combo) -> dict:
        entry = self.state.get(combo.combo_id)
        today = _today_str()

        if entry is None or entry.get("day") != today:
            entry = {
                "day": today,
                "day_count": 0,
                "minute_window_start": 0.0,
                "minute_count": 0,
            }
            self.state[combo.combo_id] = entry

        # 1분 지났으면 분당 카운트 리셋
        now = time.time()
        if now - entry["minute_window_start"] >= 60:
            entry["minute_window_start"] = now
            entry["minute_count"] = 0

        return entry

    def _is_available(self, combo: Combo) -> bool:
        limits = self.limits.get(combo.model, {"rpm": 5, "rpd": 20})
        entry = self._combo_state(combo)
        return (combo.combo_id not in self._disabled
                and time.time() >= entry.get("cooldown_until", 0)
                and entry["minute_count"] < limits["rpm"]
                and entry["day_count"] < limits["rpd"])

    def mark_cooldown(self, combo: Combo, seconds: float = 60):
        entry = self._combo_state(combo)
        entry["cooldown_until"] = time.time() + max(1, seconds)
        _save_state(self.state)

    def mark_quota_error(self, combo: Combo, exc: Exception) -> str:
        """일일 제한만 소진 처리하고 RPM/TPM 및 불명확한 429는 대기한다."""
        details = getattr(exc, "details", None)
        message = (str(exc) + " " + json.dumps(details, default=str)).lower()
        if any(marker in message for marker in ("perday", "per_day", "per day", "daily")):
            self.mark_exhausted(combo)
            return "일일 할당량 소진"
        retry = re.search(r'(?:retrydelay[\"\s:]+|retry in\s+)([0-9.]+)s', message)
        self.mark_cooldown(combo, float(retry.group(1)) if retry else 60)
        return "일시적 제한: 대기 후 재사용"

    def disable(self, combo: Combo, whole_key: bool = False):
        """인증/모델 오류 조합은 현재 실행 동안 제외한다."""
        for candidate in self.combos:
            if candidate.combo_id == combo.combo_id or (whole_key and candidate.api_key == combo.api_key):
                self._disabled.add(candidate.combo_id)

    def _reserve(self, combo: Combo):
        entry = self._combo_state(combo)
        entry["minute_count"] += 1
        entry["day_count"] += 1
        _save_state(self.state)

    def mark_exhausted(self, combo: Combo):
        """실제 API에서 일일 quota 에러를 받았을 때 호출. 이 조합을 오늘 한도만큼 다 쓴 걸로 강제 표시."""
        limits = self.limits.get(combo.model, {"rpm": 5, "rpd": 20})
        entry = self._combo_state(combo)
        entry["minute_count"] = limits["rpm"]
        entry["day_count"] = limits["rpd"]
        _save_state(self.state)

    # -----------------------------------------------------------------
    # 조합 획득
    # -----------------------------------------------------------------

    def acquire(self) -> Optional[Combo]:
        """
        현재 조합부터 키별 모델 순서로 쓸 수 있는 (키, 모델) 조합을 찾아 예약하고 반환한다.
        지금 당장 쓸 수 있는 조합이 없으면 None을 반환한다 (RPM 제한 때문에 일시적으로
        막힌 것일 수도 있으니, 호출 쪽에서 잠깐 대기 후 재시도할지 결정).
        """
        n = len(self.combos)
        for i in range(n):
            idx = (self._rr_index + i) % n
            combo = self.combos[idx]
            if self._is_available(combo):
                self._rr_index = idx
                self._reserve(combo)
                return combo
        return None

    def acquire_blocking(self, max_wait_sec: int = 90, poll_interval: int = 5) -> Combo:
        """
        쓸 수 있는 조합이 나올 때까지(대부분 RPM 리셋 대기) 최대 max_wait_sec초까지 기다린다.
        그래도 없으면 KeyPoolExhaustedError를 낸다 (=오늘 모든 조합의 일일 한도 소진 가능성).
        """
        waited = 0
        while waited <= max_wait_sec:
            combo = self.acquire()
            if combo:
                return combo
            if all(c.combo_id in self._disabled or
                   self._combo_state(c)["day_count"] >= self.limits.get(c.model, {"rpd": 20})["rpd"]
                   for c in self.combos):
                raise KeyPoolExhaustedError("모든 조합이 일일 한도 소진 또는 인증/모델 오류로 사용 불가합니다.")
            if waited >= max_wait_sec:
                break
            print(f"  - [풀 대기] 지금은 사용 가능한 (키, 모델) 조합이 없습니다. {poll_interval}초 후 재확인...")
            delay = min(poll_interval, max_wait_sec - waited)
            time.sleep(delay)
            waited += delay

        raise KeyPoolExhaustedError(
            "사용 가능한 조합을 기다리는 시간이 초과되었습니다. 일시적 제한이 풀린 뒤 재시도하세요."
        )

    # -----------------------------------------------------------------
    # 상태 확인용
    # -----------------------------------------------------------------

    def status(self) -> str:
        lines = ["=" * 72, f"{'조합':<28}{'분당(RPM)':<14}{'일일(RPD)':<14}", "-" * 72]
        for combo in self.combos:
            limits = self.limits.get(combo.model, {"rpm": 5, "rpd": 20})
            entry = self._combo_state(combo)
            rpm_str = f"{entry['minute_count']}/{limits['rpm']}"
            rpd_str = f"{entry['day_count']}/{limits['rpd']}"
            lines.append(f"{combo.combo_id:<28}{rpm_str:<14}{rpd_str:<14}")
        lines.append("=" * 72)
        return "\n".join(lines)

    def finish(self, combo, response=None, error=None):
        """로컬 풀은 예약 시 사용량을 기록하며 공용 예약 해제가 필요 없다."""
        pass


if __name__ == "__main__":
    # 단독 실행하면 현재 풀 구성과 사용량 현황만 출력한다.
    pool = GeminiKeyPool()
    print(f"등록된 키 수  : {len(pool.keys)}개 ({', '.join(l for l, _ in pool.keys)})")
    print(f"등록된 모델 수: {len(pool.models)}개 ({', '.join(pool.models)})")
    print(f"총 조합 수    : {len(pool.combos)}개\n")
    print(pool.status())
