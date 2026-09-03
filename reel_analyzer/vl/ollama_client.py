from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from .parser import QwenResponseError, parse_json_response
from .reel_prompt import build_final_prompt, build_window_prompt


class OllamaError(RuntimeError):
    pass


def response_text(message: dict[str, Any]) -> str:
    """Qwen3-VL may place structured output in ``thinking`` despite think=False."""
    return str(message.get("content") or message.get("thinking") or "").strip()


def build_window_ranges(frame_count: int, batch_size: int, overlap: int) -> list[tuple[int, int]]:
    if batch_size <= 0 or overlap < 0 or overlap >= batch_size:
        raise ValueError("batch size must be positive and overlap must be smaller than it")
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < frame_count:
        end = min(start + batch_size, frame_count)
        ranges.append((start, end))
        if end == frame_count:
            break
        start += batch_size - overlap
    return ranges


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen3-vl:8b", num_ctx: int = 4_096) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx

    def ensure_ready(self) -> None:
        requests = _requests()
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            response.raise_for_status()
        except requests.RequestException as error:
            raise OllamaError("Ollama server is not running") from error
        names = {str(model.get("name", "")) for model in response.json().get("models", [])}
        if self.model not in names:
            raise OllamaError(f"{self.model} model is not installed")

    def analyze_frames(
        self,
        frame_paths: list[Path],
        timestamps: list[float],
        batch_size: int = 3,
        batch_overlap: int = 1,
        progress: Any = None,
    ) -> dict[str, Any]:
        ranges = build_window_ranges(len(frame_paths), batch_size, batch_overlap)
        state: dict[str, Any] = {}
        for index, (start, end) in enumerate(ranges, start=1):
            state = self._request(
                build_window_prompt(timestamps[start:end], state),
                frame_paths[start:end],
                num_predict=384,
            )
            if progress:
                progress(index, len(ranges))
        return self._request(build_final_prompt(state), [], num_predict=1_024)

    def _request(self, prompt: str, frame_paths: list[Path], num_predict: int) -> dict[str, Any]:
        requests = _requests()
        encoded_images = [base64.b64encode(path.read_bytes()).decode("ascii") for path in frame_paths]
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"num_ctx": self.num_ctx, "num_predict": num_predict},
            "messages": [{"role": "user", "content": prompt, "images": encoded_images}],
        }
        try:
            response = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=600)
            if not response.ok:
                detail = response.text.strip().replace("\n", " ")
                raise OllamaError(f"Qwen response failed (HTTP {response.status_code}): {detail[:1_000]}")
            raw_response = response_text(response.json()["message"])
            if not raw_response:
                raise OllamaError("Qwen returned an empty response; update Ollama and retry")
        except requests.RequestException as error:
            raise OllamaError("Qwen response failed") from error
        except (KeyError, TypeError, ValueError) as error:
            raise OllamaError("Qwen response failed") from error
        try:
            return parse_json_response(raw_response)
        except QwenResponseError:
            raise


def _requests():
    try:
        import requests
    except ImportError as error:
        raise OllamaError("requests is not installed; install requirements.txt") from error
    return requests
