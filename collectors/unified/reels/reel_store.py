"""Atomic persistence helpers for Reel collection outputs."""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .reel_records import CSV_FIELDS, REEL_METRIC_CHANGE_FIELDS, parse_snapshot_field


def replace_file_with_retry(source: Path, destination: Path, attempts: int = 8) -> None:
    """Atomically replace a file, tolerating brief Windows scanner locks."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt >= attempts:
                raise
            time.sleep(0.1 * attempt)


def write_csv_records(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    selected_fields = fields or CSV_FIELDS
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=selected_fields, extrasaction="ignore", lineterminator="\r\n", quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in selected_fields} for row in rows)
        replace_file_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_field_value(field: str, value: Any) -> Any:
    if value in (None, ""):
        return None
    parsed_snapshot = parse_snapshot_field(field)
    base_field = parsed_snapshot["baseField"] if parsed_snapshot else field
    if base_field == "ad":
        return str(value).strip().lower() == "true"
    if base_field in {
        "collection_number",
        "view_count",
        "like_count",
        "comment_count",
        "share_count",
        "repost_count",
        "saved_count",
        "follower_count",
        *REEL_METRIC_CHANGE_FIELDS,
    }:
        text = str(value).strip().replace(",", "")
        return int(text) if re.fullmatch(r"-?\d+", text) else value
    if base_field in {"days_since_previous", "days_since_upload", "reaction_rate", "reaction_rate_change"}:
        text = str(value).strip()
        return float(text) if re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)", text) else value
    if base_field == "video_duration_seconds":
        text = str(value).strip()
        if re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", text):
            number = float(text)
            return int(number) if number.is_integer() else number
    return value


def write_json_records(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {field: _json_field_value(field, row.get(field, "")) for field in fields}
        for row in rows
    ]
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        replace_file_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
