"""공용 시트에서 매 분석 시 두 후보를 예약/비교한다. API 키는 전송하지 않는다."""

import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

from .local import Combo, GeminiKeyPool, KeyPoolExhaustedError


class SheetsPoolError(RuntimeError):
    pass


class SheetsKeyPool(GeminiKeyPool):
    def __init__(self):
        self.keys = self._load_keys()
        self.models = self._load_models()
        if not self.models or any(m not in {
            "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
        } for m in self.models):
            raise SheetsPoolError("공용 풀은 Gemini 3.5/3.6/3.7/3.8 Flash만 지원합니다. GEMINI_MODELS에서 Lite 및 다른 모델을 제외하세요.")
        if not self.keys or len(dict(self.keys)) != len(self.keys):
            raise SheetsPoolError("API 키와 중복 없는 키 별칭을 .env에 설정하세요.")
        self.combos = [Combo(label, key, model) for label, key in self.keys for model in self.models]
        self.url = os.getenv("GEMINI_POOL_URL", "").strip()
        self.token = os.getenv("GEMINI_POOL_TOKEN", "").strip()
        self.user = os.getenv("GEMINI_POOL_USER", "").strip()
        parsed = urlparse(self.url)
        if (parsed.scheme != "https" or parsed.hostname != "script.google.com"
                or not parsed.path.endswith("/exec") or not self.token or not self.user):
            raise SheetsPoolError("시트 연동 설정이 필요합니다: GEMINI_POOL_URL(/exec), GEMINI_POOL_TOKEN, GEMINI_POOL_USER. pool/apps_script/README.md를 참고하세요.")
        self._disabled = set()
        self._active = None
        self._outcome = {}
        self.pending_dir = Path(__file__).resolve().parents[1] / ".pool_pending"

    def _post(self, action, **payload):
        # 동일 요청 ID로만 재전송한다. 응답 유실 시 이중 예약/기록 방지.
        for attempt in range(3):
            try:
                response = requests.post(self.url, json={
                    "action": action, "token": self.token, "user": self.user, **payload,
                }, timeout=30)
                response.raise_for_status()
                result = response.json()
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise SheetsPoolError("공용 시트 응답을 확인할 수 없습니다. 중복 사용을 막기 위해 로컬 풀로 전환하지 않습니다.") from None
                time.sleep(attempt + 1)
                continue
            if not isinstance(result, dict) or not result.get("ok"):
                raise SheetsPoolError("공용 시트 오류: " + str(result.get("error", "응답 형식 오류") if isinstance(result, dict) else "응답 형식 오류"))
            return result

    def _retry_pending(self):
        for path in sorted(self.pending_dir.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["endpoint"] != self.url or record["user"] != self.user:
                continue
            self._post("finish", **record["payload"])
            path.unlink()

    def acquire_blocking(self, max_wait_sec=90, poll_interval=5):
        if self._active:
            raise SheetsPoolError("이전 예약이 아직 해제되지 않았습니다.")
        self._retry_pending()
        deadline = time.monotonic() + max_wait_sec
        request_id = str(uuid.uuid4())
        while True:
            result = self._post("acquire", request_id=request_id, candidates=[
                {"key_label": c.key_label, "model": c.model}
                for c in self.combos if c.combo_id not in self._disabled
            ])
            selected = result.get("selected")
            if selected:
                combo = next((c for c in self.combos if c.key_label == selected["key_label"]
                              and c.model == selected["model"]), None)
                if combo is None:
                    raise SheetsPoolError("서버가 로컬에 없는 키/모델을 선택했습니다.")
                self._active = selected["request_id"]
                self._outcome = {}
                print("  - 무작위 후보: " + ", ".join(
                    f"{c['key_label']}:{c['model']} (잔여 {c.get('remaining', '?')}회, 일일 사용 {c['day_count']}회)"
                    for c in result["sampled"]))
                print(f"  - 선택: {combo.combo_id}; 미선택 후보 예약 해제 완료")
                return combo
            if time.monotonic() >= deadline or result.get("terminal"):
                raise KeyPoolExhaustedError(result.get("reason", "시트에서 사용 가능한 조합이 없습니다."))
            print("  - 사용 중/일시 제한인 조합이 풀리기를 기다립니다...")
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))

    def mark_exhausted(self, combo):
        self._outcome.update(limit="일일")

    def mark_cooldown(self, combo, seconds=60):
        self._outcome.update(limit="일시", retry_seconds=max(1, seconds))

    def finish(self, combo, response=None, error=None):
        if self._active is None:
            return
        usage = getattr(response, "usage_metadata", None)
        def count(name):
            value = getattr(usage, name, None)
            return value if type(value) is int and value >= 0 else None
        payload = {
            "request_id": self._active,
            "status": "성공" if response is not None else ("실패" if getattr(error, "code", None) else "결과 미확인"),
            "error_code": getattr(error, "code", None),
            "input_tokens": count("prompt_token_count"),
            "output_tokens": count("candidates_token_count"),
            "total_tokens": count("total_token_count"),
            **self._outcome,
        }
        self.pending_dir.mkdir(exist_ok=True)
        path = self.pending_dir / (self._active.replace(":", "_") + ".json")
        # API 응답 본문/영상/키/인증 토큰은 저장하거나 시트로 전송하지 않는다.
        path.write_text(json.dumps({"endpoint": self.url, "user": self.user, "payload": payload}), encoding="utf-8")
        try:
            self._post("finish", **payload)
            path.unlink()
        except SheetsPoolError:
            print("  - 결과 기록 전송 실패: 로컬 보관 후 다음 분석 전에 재전송합니다. 시트 예약은 유지됩니다.")
        finally:
            self._active = None

    def status(self):
        result = self._post("status")
        return json.dumps(result["rows"], ensure_ascii=False, indent=2)


def create_pool():
    mode = os.getenv("GEMINI_POOL_MODE", "sheets").strip().lower()
    if mode == "local":
        return GeminiKeyPool()
    if mode != "sheets":
        raise SheetsPoolError("GEMINI_POOL_MODE는 sheets 또는 local이어야 합니다.")
    return SheetsKeyPool()
