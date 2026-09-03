from __future__ import annotations

import json
from typing import Any


class QwenResponseError(RuntimeError):
    def __init__(self, message: str, raw_response: str):
        super().__init__(message)
        self.raw_response = raw_response


def parse_json_response(raw_response: str) -> dict[str, Any]:
    """Extract the first valid JSON object, tolerating accidental surrounding text."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(raw_response):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw_response[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise QwenResponseError("Qwen JSON parsing failed", raw_response)
