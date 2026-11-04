"""Import collector-format workbook snapshots through the existing history journal."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.exporters.instagram_collector import read_reel_records_from_xlsx
from .fashion_beauty_collection import read_history
from .fashion_beauty_scheduler import DatasetConfig
from .process_lock import AtomicProcessLock
from .instagram_reels_browser import CSV_FIELDS, REEL_HISTORY_DIRECTORY, REEL_HISTORY_FILENAME


def _timestamp(value: str) -> str:
    if not value:
        return ""
    try:
        kst = timezone(timedelta(hours=9))
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            parsed = datetime(1899, 12, 30, tzinfo=kst) + timedelta(days=float(value))
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=kst)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise ValueError(f"Invalid imported collection timestamp: {value}") from None


def import_refresh_snapshots(workbook: Path, data_dir: Path, *, start_row=None, end_row=None, limit_urls=None) -> int:
    selected = {row["url"] for row in read_reel_records_from_xlsx(workbook, start_row=start_row, end_row=end_row)}
    if limit_urls is not None:
        ordered = list(dict.fromkeys(row["url"] for row in read_reel_records_from_xlsx(workbook, start_row=start_row, end_row=end_row)))
        selected = set(ordered[:limit_urls])
    records = [row for row in read_reel_records_from_xlsx(workbook) if row["url"] in selected]
    incoming = []
    for record in records:
        base = {key: value for key, value in record.items() if key in CSV_FIELDS or key == "collection_number"}
        snapshots = {}
        for key, value in record.items():
            match = re.match(r"^(\d+(?:st|nd|rd|th) collect|\+\d+(?:minute|hour|day|weeks)(?:_\d+)?)_(.+)$", key)
            if match and match[2] in CSV_FIELDS:
                snapshots.setdefault(match[1], {})[match[2]] = value
        if snapshots:
            base["collection_number"] = "1"
        for row in [base, *({**base, **values, "collection_number": ""} for values in snapshots.values())]:
            if not row.get("collected_at"):
                continue  # A URL-only input is bootstrapped by the collector, never a fabricated past snapshot.
            row["collected_at"] = _timestamp(row["collected_at"])
            if row.get("uploaded_at"):
                row["uploaded_at"] = _timestamp(row["uploaded_at"])
            for key, value in row.items():
                if key.endswith("_count"):
                    row[key] = re.sub(r"\([+-][\d,.]+\)$", "", str(value)).replace(",", "")
            incoming.append(row)
    lock = AtomicProcessLock(data_dir / "collector.lock.json")
    if not lock.acquire():
        raise RuntimeError("Another collector is using the refresh output directory.")
    try:
        existing = read_history(DatasetConfig("refresh", data_dir, (), base_output=True))
        seen = {(row["url"], _timestamp(str(row["collected_at"]))) for row in existing}
        additions = []
        counts = {}
        for row in existing:
            counts[row["url"]] = counts.get(row["url"], 0) + 1
        for row in sorted(incoming, key=lambda item: (item["url"], item["collected_at"])):
            key = (row["url"], row["collected_at"])
            if key in seen:
                continue  # Existing saved metrics take precedence over imported copies.
            counts[row["url"]] = counts.get(row["url"], 0) + 1
            row["collection_number"] = row.get("collection_number") or str(counts[row["url"]])
            additions.append(row)
            seen.add(key)
        if additions:
            journal = data_dir / REEL_HISTORY_DIRECTORY / f"{REEL_HISTORY_FILENAME}.pending.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            with journal.open("a", encoding="utf-8") as file:
                for row in additions:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
        return len(additions)
    finally:
        lock.release()
