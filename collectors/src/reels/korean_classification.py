"""Classify original Reel text and explicit account-country evidence."""

from __future__ import annotations

import re
import unicodedata
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7af\ud7b0-\ud7ff]")
_KOREAN_COUNTRIES = {
    "kr", "kor", "korea", "southkorea", "republicofkorea", "korearepublicof", "대한민국", "한국",
}


def normalize_account_country(value: Any) -> str:
    country = str(value or "").strip()
    key = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", country).casefold())
    if key in {"", "unknown", "unavailable", "none", "null", "na", "notavailable", "notprovided", "notshared", "미확인", "알수없음", "정보없음", "미공개", "공개안함", "공유되지않음"}:
        return ""
    return country


def classify_korean(caption: Any = "", hashtags: Any = (), account_country: Any = None) -> bool | None:
    """Unknown country is unknown, rather than evidence of a foreign account."""
    tags = hashtags if isinstance(hashtags, (list, tuple, set)) else (hashtags,)
    text = unicodedata.normalize("NFKC", " ".join(str(value or "") for value in (caption, *tags)))
    country = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", normalize_account_country(account_country)).casefold())
    if _HANGUL.search(text) or country in _KOREAN_COUNTRIES:
        return True
    if country in {"", "unknown", "unavailable", "none", "null", "na", "notavailable", "notprovided", "notshared", "미확인", "알수없음", "정보없음", "미공개", "공개안함"}:
        return None
    return False


def record_is_korean(record: Mapping[str, Any]) -> bool | None:
    # Keep evidence from full original captions that were truncated for storage.
    confirmed = record.get("korean")
    if record.get("korean_source") == "manual":
        return {"true": True, "false": False}.get(str(confirmed).strip().casefold())
    if confirmed is True or isinstance(confirmed, str) and confirmed.strip().casefold() == "true":
        return True
    classified = classify_korean(
        caption=" ".join(str(record.get(field) or "") for field in ("title", "caption", "description")),
        hashtags=record.get("hashtags") or (),
        account_country=record.get("account_country") or record.get("account_based_in") or record.get("accountCountry"),
    )
    if classified is None and record.get("korean_source") == "account_country" and str(confirmed).strip().casefold() == "false":
        return False
    return classified


def korean_field(value: bool | None) -> str:
    return "-" if value is None else str(value).lower()


def classification_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    flag = record_is_korean(record)
    country = normalize_account_country(record.get("account_country") or record.get("account_based_in") or record.get("accountCountry"))
    source = record.get("korean_source")
    if source != "manual":
        source = "unknown" if flag is None else ("account_country" if country or flag is False else "caption")
    return {"korean": korean_field(flag), "korean_source": source, "account_country": country}


def read_korean_overrides(data_dir: Path | str) -> dict[str, Any]:
    path = Path(data_dir) / ".collector" / "korean_overrides.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("records"), dict):
        raise ValueError("Invalid Korean classification overrides")
    return payload["records"]


def read_account_countries(data_dir: Path | str) -> dict[str, Any]:
    path = Path(data_dir) / ".collector" / "account_countries.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("records"), dict):
        raise ValueError("Invalid account-country evidence")
    return payload["records"]


def apply_account_countries(rows: Any, data_dir: Path | str) -> list[dict[str, Any]]:
    evidence = read_account_countries(data_dir)
    result = [dict(original) for original in rows]
    countries: dict[str, tuple[str, str]] = {}
    for row in result:
        if "account_country" in row:
            row["account_country"] = normalize_account_country(row["account_country"])
        user_id = str(row.get("user_id") or "").strip()
        country = normalize_account_country(row.get("account_country"))
        if user_id and country:
            candidate = (str(row.get("collected_at") or ""), country)
            if user_id not in countries or candidate[0] > countries[user_id][0]:
                countries[user_id] = candidate
    for user_id, saved in evidence.items():
        if isinstance(saved, dict) and (country := normalize_account_country(saved.get("account_country"))):
            countries[str(user_id)] = (str(saved.get("checked_at") or ""), country)
    for row in result:
        if country := countries.get(str(row.get("user_id") or "").strip()):
            row["account_country"] = country[1]
    return result


def save_account_country(data_dir: Path | str, user_id: str, username: str, country: str, *, checked_at: str | None = None) -> None:
    from .process_lock import AtomicProcessLock

    if not str(user_id).isdigit() or not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", username) or classify_korean(account_country=country) is None:
        raise ValueError("A verified account identity and country are required")
    path = Path(data_dir) / ".collector" / "account_countries.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = AtomicProcessLock(path.with_suffix(".lock"), wait_seconds=30)
    if not lock.acquire():
        raise RuntimeError("Account-country storage is busy")
    try:
        entries = read_account_countries(data_dir)
        entry = {"username": username, "account_country": country, "checked_at": checked_at or datetime.now(timezone.utc).isoformat(), "source": "instagram_about"}
        previous = entries.get(str(user_id), {})
        if not isinstance(previous, dict):
            previous = {}
        if previous.get("checked_at", "") > entry["checked_at"]:
            return
        entries[str(user_id)] = entry
        descriptor, temporary_name = tempfile.mkstemp(prefix=".countries-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "records": entries}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        lock.release()


def apply_korean_overrides(rows: Any, data_dir: Path | str) -> list[dict[str, Any]]:
    from .reel_records import normalize_reel_url

    overrides = read_korean_overrides(data_dir)
    result = []
    for original in apply_account_countries(rows, data_dir):
        row = dict(original)
        normalized = normalize_reel_url(row.get("url") or row.get("reel_url"))
        code = normalized["shortcode"] if normalized else str(row.get("reel_id") or "")
        override = overrides.get(code)
        if row.get("account_country") and row.get("korean_source") in {"unknown", "account_country", "manual"} and not isinstance(override, dict):
            # An old default '-' -> false is superseded by confirmed country;
            # explicit per-Reel overrides below remain authoritative.
            row.pop("korean", None)
            row["korean_source"] = "account_country"
        if isinstance(override, dict) and "korean" in override and (override["korean"] is None or type(override["korean"]) is bool):
            row.update(korean=override["korean"], korean_source="manual")
        row.update(classification_fields(row))
        result.append(row)
    return result


def materialize_korean_overrides(data_dir: Path | str) -> int:
    """Refresh collector exports using the same lock and writers as collection."""
    from .android_metric_queue import _android_metric_queue_paths
    from .process_lock import AtomicProcessLock
    from .instagram_reels_browser import read_csv_objects
    from .reel_history import write_long_output_bundle
    from .reel_records import enrich_reel_collection_rows
    from .reel_store import write_csv_records

    data_dir = Path(data_dir)
    history = data_dir / ".collector" / "reels_history_active.csv"
    lock = AtomicProcessLock(_android_metric_queue_paths(data_dir)["history_lock"], wait_seconds=30)
    if not lock.acquire():
        raise RuntimeError("Classification saved; collector storage is busy")
    try:
        fields, rows = read_csv_objects(history)
        fields = list(dict.fromkeys([*fields, "korean", "korean_source", "account_country"]))
        rows = enrich_reel_collection_rows(apply_korean_overrides(rows, data_dir))
        write_csv_records(history, rows, fields)
        write_long_output_bundle(history, rows, fields)
        from .instagram_follower_enricher import ensure_user_history, atomic_write_csv
        from .reel_history import write_users_xlsx
        user_history = ensure_user_history(data_dir)
        if user_history.is_file():
            user_fields, users = read_csv_objects(user_history)
            if "account_country" not in user_fields:
                user_fields.append("account_country")
            atomic_write_csv(user_history, apply_account_countries(users, data_dir), user_fields)
            write_users_xlsx(data_dir)
        if data_dir.parent.name == ".datasets":
            from .fashion_beauty_collection import publish_dataset_outputs
            from .fashion_beauty_scheduler import DatasetConfig
            publish_dataset_outputs(DatasetConfig(data_dir.name, data_dir, ()))
            analysis = data_dir.parent.parent / f"{data_dir.name}_reel_analysis.json"
            if analysis.is_file():
                from .fashion_analyzer import _write_json_atomic
                _write_json_atomic(analysis, None)
        return len(rows)
    finally:
        lock.release()


def save_korean_override(data_dir: Path | str, reel_url: str, value: bool | None) -> dict[str, Any]:
    from .process_lock import AtomicProcessLock
    from .reel_records import normalize_reel_url

    if value is not None and type(value) is not bool:
        raise ValueError("korean must be true, false, or null")
    normalized = normalize_reel_url(reel_url)
    if normalized is None:
        raise ValueError("Invalid Reel URL")
    path = Path(data_dir) / ".collector" / "korean_overrides.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = AtomicProcessLock(path.with_suffix(".lock"), wait_seconds=30)
    if not lock.acquire():
        raise RuntimeError("Classification storage is busy; retry shortly")
    try:
        entries = read_korean_overrides(data_dir)
        entry = {"korean": value, "source": "manual", "updated_at": datetime.now(timezone.utc).isoformat()}
        entries[normalized["shortcode"]] = entry
        descriptor, temporary_name = tempfile.mkstemp(prefix=".korean-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "records": entries}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        lock.release()
    try:
        count = materialize_korean_overrides(data_dir)
        return {**entry, "exported": True, "rows": count}
    except (OSError, ValueError, RuntimeError) as error:
        return {**entry, "exported": False, "export_error": str(error)}
