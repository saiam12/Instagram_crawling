"""Cached, read-only Android availability checks; never send input or restart apps."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone


class AndroidHealth:
    INTERVAL_SECONDS = 10
    CONFIRM_FAILURES = 2

    def __init__(self):
        self.lock = threading.Lock()
        self.records = {}

    def check(self, serial=""):
        serial = serial or os.environ.get("INSTAGRAM_ANDROID_DEVICE_ID", "").strip()
        with self.lock:
            record = self.records.setdefault(serial, {"checked": 0, "running": False, "failures": 0,
                                                       "value": {"state": "checking", "alert": "", "checked_at": ""}})
            if not record["running"] and time.monotonic() - record["checked"] >= self.INTERVAL_SECONDS:
                record["running"] = True
                threading.Thread(target=self._refresh, args=(serial,), daemon=True).start()
            return dict(record["value"])

    def _refresh(self, serial):
        try:
            result = self._probe(serial)
        except (OSError, ValueError) as error:
            result = {"state": "unknown", "label": "기기 상태 확인 필요", "detail": str(error)[:300]}
        except subprocess.TimeoutExpired:
            result = {"state": "unresponsive", "label": "Android 명령 응답 없음", "detail": "ADB 상태 조회 명령이 5초 안에 응답하지 않았습니다."}
        except Exception:
            result = {"state": "unknown", "label": "기기 상태 확인 필요", "detail": "상태 조회에 실패했습니다. 기기 중단으로 단정하지 않고 다시 확인합니다."}
        with self.lock:
            record = self.records[serial]
            # UI dump errors and an ADB timeout do not prove input is blocked.
            failed = result["state"] in {"disconnected", "input_blocked", "app_error"}
            failure_kind = (result.get("serial", serial), result["state"])
            record["failures"] = (record["failures"] + 1 if record.get("failure_kind") == failure_kind else 1) if failed else 0
            record["failure_kind"] = failure_kind
            result.update(checked_at=datetime.now(timezone.utc).isoformat(),
                          consecutive_failures=record["failures"],
                          alert=result["label"] if record["failures"] >= self.CONFIRM_FAILURES else "")
            record.update(value=result, checked=time.monotonic(), running=False)

    @staticmethod
    def _probe(serial):
        from src.reels.android_reel_metrics import default_adb_path
        adb = default_adb_path()
        if not adb.is_file():
            return {"state": "unknown", "label": "기기 상태 확인 필요", "detail": "ADB 경로를 찾지 못해 기기 상태를 확인할 수 없습니다."}

        def run(*arguments):
            return subprocess.run([str(adb), *arguments], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=5,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        devices = run("devices")
        if devices.returncode:
            return {"state": "unknown", "label": "기기 상태 확인 필요", "detail": (devices.stderr or devices.stdout).strip()[:300]}
        states = {parts[0]: parts[1] for line in devices.stdout.splitlines()
                  if len(parts := line.split()) >= 2 and parts[1] in {"device", "offline", "unauthorized", "recovery", "bootloader"}}
        if not serial:
            if not states:
                return {"state": "disconnected", "label": "Android 기기를 찾을 수 없음", "detail": "ADB에 연결된 기기가 없습니다."}
            if len(states) != 1:
                return {"state": "unknown", "label": "기기 상태 확인 필요", "detail": "기기가 여러 대입니다. 수집기의 기기 serial을 지정하세요."}
            serial = next(iter(states))
        device_state = states.get(serial, "missing")
        if device_state != "device":
            return {"state": "disconnected", "serial": serial, "label": "Android 연결 끊김 · 기기 확인 필요",
                    "detail": f"{serial}: {device_state}"}
        boot = run("-s", serial, "shell", "getprop", "sys.boot_completed")
        if boot.returncode:
            return {"state": "unresponsive", "serial": serial, "label": "Android 명령 실행 불가",
                    "detail": (boot.stderr or boot.stdout).strip()[:300]}
        if boot.stdout.strip() != "1":
            return {"state": "booting", "serial": serial, "label": "Android 부팅 중", "detail": "기기 부팅이 아직 완료되지 않았습니다."}
        input_state = run("-s", serial, "shell", "dumpsys", "input")
        if input_state.returncode:
            return {"state": "unknown", "serial": serial, "label": "입력 상태 확인 필요",
                    "detail": "ADB는 연결됐지만 입력 상태 조회에 실패했습니다."}
        text = input_state.stdout
        disabled = re.search(r"\bDispatchEnabled:\s*(?:false|0)\b", text, re.I)
        frozen = re.search(r"\bDispatchFrozen:\s*(?:true|1)\b", text, re.I)
        if disabled or frozen:
            return {"state": "input_blocked", "serial": serial, "label": "Android 입력 처리가 중단됨",
                    "detail": "입력 디스패처가 비활성 또는 정지 상태입니다."}
        if not re.search(r"\bDispatchEnabled:\s*(?:true|1)\b", text, re.I) or not re.search(r"\bDispatchFrozen:\s*(?:false|0)\b", text, re.I):
            return {"state": "unknown", "serial": serial, "label": "입력 상태 확인 필요",
                    "detail": "입력 서비스 응답에서 디스패처 상태를 확인하지 못했습니다."}
        focused = []
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if re.match(r"\s*FocusedWindow(?:s)?:", line):
                focused.append(line)
                for following in lines[index + 1:]:
                    if not following.startswith("    "):
                        break
                    focused.append(following)
        if re.search(r"Application (?:Not Responding|Error)|isn.t responding|응답하지 않|응답 없음", "\n".join(focused), re.I):
            return {"state": "app_error", "serial": serial, "label": "Android 앱 응답 없음 · 화면 확인 필요",
                    "detail": "현재 입력 대상에 앱 응답 없음 또는 오류 창이 표시되어 있습니다."}
        return {"state": "responsive", "serial": serial, "label": "Android 연결·명령 응답 확인",
                "detail": "ADB 연결과 입력 서비스 조회가 응답했습니다. 모든 앱 터치의 성공을 보장하는 판정은 아닙니다."}
