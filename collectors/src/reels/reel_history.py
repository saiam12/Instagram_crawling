"""Snapshot history, recovery and public projections."""
from __future__ import annotations

import asyncio
import calendar
import json
import math
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree
from .android_metric_queue import REEL_HISTORY_DIRECTORY, _android_metric_queue_paths
from .android_reel_metrics import ANDROID_COLLECTION_FIELDS, WEB_PREFERRED_COUNT_FIELDS, AndroidMetricResult, has_web_count, merge_android_metrics
from .collection_settings import (
    ANDROID_METRIC_FIELDS,
    COUNT_HISTORY_METRIC_FIELDS,
    COUNT_HISTORY_XLSX_FILENAME,
    LEGACY_REEL_DROPPED_FIELDS,
    LEGACY_REEL_HISTORY_FILENAMES,
    PUBLIC_REELS_STEM,
    RECOLLECT_COOLDOWN_SCHEDULE,
    REEL_HISTORY_FILENAME,
    REEL_STORE_FLUSH_RECORD_COUNT,
    ROW_COLLECTION_FIELDS,
)
from .instagram_follower_enricher import ensure_user_history, read_csv_objects
from .korean_classification import apply_korean_overrides, classification_fields, record_is_korean
from .process_lock import AtomicProcessLock
from .reel_records import (
    CSV_FIELDS,
    REEL_CHANGE_FIELDS,
    UNAVAILABLE_LIKE_COUNT_MARKER,
    calculate_reel_derived_fields,
    enrich_reel_collection_rows,
    exact_nonnegative_integer,
    isoformat_utc,
    js_round,
    normalize_reel_url,
    parse_datetime,
    parse_snapshot_field,
    utc_now,
)
from .reel_store import write_csv_records, write_json_records


def elapsed_snapshot_label(initial_timestamp: Any, current_timestamp: Any) -> str:
    initial = parse_datetime(initial_timestamp)
    current = parse_datetime(current_timestamp)
    minutes = max(1, js_round((current - initial).total_seconds() / 60)) if initial and current else 1
    if minutes < 60:
        return f"+{minutes}Minute"
    hours = max(1, js_round(minutes / 60))
    if hours < 24:
        return f"+{hours}Hour"
    days = max(1, js_round(hours / 24))
    return f"+{days}Day" if days < 7 else f"+{max(1, js_round(days / 7))}Weeks"


def days_since_upload(uploaded_timestamp: Any, collected_timestamp: Any) -> float | str:
    uploaded = parse_datetime(uploaded_timestamp)
    collected = parse_datetime(collected_timestamp)
    if not uploaded or not collected:
        return ""
    elapsed = max(0.0, (collected - uploaded).total_seconds() / 86_400)
    return js_round(elapsed * 100) / 100


def collection_label(collection_number: int) -> str:
    remainder100 = collection_number % 100
    suffix = "th" if 11 <= remainder100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(collection_number % 10, "th")
    return f"{collection_number}{suffix} collect"


def recollect_cooldown_policy(completed_collections: int) -> dict[str, Any]:
    index = max(0, min(len(RECOLLECT_COOLDOWN_SCHEDULE) - 1, int(completed_collections) - 1))
    return RECOLLECT_COOLDOWN_SCHEDULE[index]


def add_calendar_months(timestamp: datetime, months: int) -> datetime:
    month_index = timestamp.month - 1 + months
    year = timestamp.year + month_index // 12
    month = month_index % 12 + 1
    day = min(timestamp.day, calendar.monthrange(year, month)[1])
    return timestamp.replace(year=year, month=month, day=day)


def next_recollect_available_at(previous_timestamp: Any, completed_collections: int) -> datetime | None:
    previous = previous_timestamp if isinstance(previous_timestamp, datetime) else parse_datetime(previous_timestamp)
    if not previous:
        return None
    policy = recollect_cooldown_policy(completed_collections)
    return add_calendar_months(previous, policy["calendar_months"]) if policy.get("calendar_months") else previous + timedelta(seconds=policy["seconds"])


def write_reel_xlsx(
    path: Path,
    rows: list[dict[str, Any]],
    fields: list[str],
    *,
    layout: str,
    sheet_name: str = "",
    project_rows: bool = True,
) -> Path:
    from src.exporters.instagram_collector import _xlsx_project_rows, write_xlsx_workbook

    matrix = [fields, *[[str(row.get(field, "") if row.get(field, "") is not None else "") for field in fields] for row in rows]]
    selected_sheet_name = sheet_name or "reels"
    projected = _xlsx_project_rows(selected_sheet_name, matrix) if project_rows else matrix
    try:
        write_xlsx_workbook(path, [(selected_sheet_name, projected)])
        return path
    except PermissionError:
        updated = path.with_name(f"{path.stem}_updated{path.suffix}")
        write_xlsx_workbook(updated, [(selected_sheet_name, projected)])
        print(f"{path.name} 파일이 열려 있어 최신 결과를 {updated.name}에 저장했습니다.", file=sys.stderr)
        return updated


def write_users_xlsx(data_dir: Path | str) -> Path:
    """Export matching user CSV, JSON and Excel, then refresh the combined workbook."""
    destination = Path(data_dir).resolve()
    fields, rows = read_csv_objects(ensure_user_history(destination))
    if not fields:
        fields = ["user_id", "username", "biography", "profile_category", "account_country", "post_count", "follower_count", "following_count", "collected_at"]
    public_fields, public_rows = project_public_records("users", rows, fields)
    write_csv_records(destination / "users.csv", public_rows, public_fields)
    write_json_records(destination / "users.json", public_rows, public_fields)
    write_reel_xlsx(
        destination / "users.xlsx", public_rows, public_fields,
        layout="columns", sheet_name="users", project_rows=False,
    )
    from src.exporters.instagram_collector import sync_combined_xlsx

    return sync_combined_xlsx(destination)


def project_public_records(
    sheet_name: str,
    rows: list[dict[str, Any]],
    fields: list[str],
) -> tuple[list[str], list[dict[str, str]]]:
    """Apply the workbook's display projection to every public file format."""
    from src.exporters.instagram_collector import _xlsx_project_rows

    matrix = [
        fields,
        *[
            [str(row.get(field, "") if row.get(field, "") is not None else "") for field in fields]
            for row in rows
        ],
    ]
    projected = _xlsx_project_rows(sheet_name, matrix)
    projected_fields = projected[0]
    return projected_fields, [
        dict(zip(projected_fields, values)) for values in projected[1:]
    ]


def write_long_output_bundle(
    csv_path: Path,
    rows: list[dict[str, Any]],
    fields: list[str],
    *,
    xlsx_layout: str = "columns",
    output_dir: Path | None = None,
) -> dict[str, Path]:
    del xlsx_layout  # Kept as a compatibility argument for older launch scripts.
    destination = output_dir or (
        csv_path.parent.parent
        if csv_path.parent.name == REEL_HISTORY_DIRECTORY
        else csv_path.parent
    )
    # Preserve one record per collection in every public export.  Recollecting
    # a Reel therefore appends a row instead of generating ``2nd collect_*``
    # snapshot columns.
    public_fields = [*fields, *([] if "reaction_rate" in fields else ["reaction_rate"])]
    public_fields = [
        *public_fields,
        *(field for field in REEL_CHANGE_FIELDS if field not in public_fields),
    ]
    public_source_rows = enrich_reel_collection_rows(apply_korean_overrides(rows, _history_data_dir(csv_path)))
    public_fields, public_rows = project_public_records(
        "reels_rows", public_source_rows, public_fields
    )
    public_csv = destination / f"{PUBLIC_REELS_STEM}.csv"
    public_json = destination / f"{PUBLIC_REELS_STEM}.json"
    public_xlsx = destination / "reels.xlsx"
    write_csv_records(public_csv, public_rows, public_fields)
    write_json_records(public_json, public_rows, public_fields)
    outputs = {
        "csv": public_csv,
        "json": public_json,
        "xlsx": write_reel_xlsx(
            public_xlsx,
            public_rows,
            public_fields,
            layout="rows",
            sheet_name="reels",
            project_rows=False,
        ),
    }
    write_count_history_xlsx(destination, public_source_rows)
    from src.exporters.instagram_collector import sync_combined_xlsx

    sync_combined_xlsx(destination)
    return outputs


def _xlsx_column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if letters is None:
        return 0
    index = 0
    for character in letters.group(0):
        index = index * 26 + ord(character) - 64
    return index - 1


def _read_xlsx_matrix(workbook: Path) -> list[list[str]]:
    main_namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(workbook) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.findall(f".//{main_namespace}t"))
                for item in shared.findall(f"{main_namespace}si")
            ]
        worksheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for source_row in worksheet.findall(f".//{main_namespace}row"):
        cells: dict[int, str] = {}
        for cell in source_row.findall(f"{main_namespace}c"):
            index = _xlsx_column_index(cell.attrib.get("r", "A1"))
            if cell.attrib.get("t") == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(f".//{main_namespace}t"))
            else:
                value_node = cell.find(f"{main_namespace}v")
                value = value_node.text if value_node is not None and value_node.text else ""
                if cell.attrib.get("t") == "s" and value:
                    value = shared_strings[int(value)]
            cells[index] = value
        if cells:
            rows.append([cells.get(index, "") for index in range(max(cells) + 1)])
    return rows


def _count_history_column(name: str) -> str:
    return f"{name}_xlsx_only"


def _count_history_key(row: dict[str, Any]) -> tuple[str, str]:
    normalized = normalize_reel_url(row.get("url"))
    url = normalized["url"] if normalized else str(row.get("url", "") or "")
    return url, str(row.get("collected_at", "") or "")


def _count_history_exact_integer(value: Any) -> int | None:
    exact = exact_nonnegative_integer(value)
    if exact is not None:
        return exact
    text = str(value if value is not None else "").strip()
    if re.fullmatch(r"\d[\d,]*", text):
        return int(text.replace(",", ""))
    return None


def _default_count_history_state(field: str, value: Any, *, legacy: bool) -> str:
    text = str(value if value is not None else "").strip()
    if text.upper() == UNAVAILABLE_LIKE_COUNT_MARKER:
        return "private_or_disabled"
    if _count_history_exact_integer(value) is not None:
        return "legacy_source_unknown" if legacy else "python_exact"
    if field == "follower_count":
        return "web_unavailable" if not text else "web_compact_untrusted"
    return "pending_android"


def write_count_history_xlsx(
    data_dir: Path | str,
    rows: Sequence[dict[str, Any]],
    *,
    android_results: Sequence[dict[str, Any]] = (),
    web_results: Sequence[dict[str, Any]] = (),
) -> Path:
    """Persist count provenance and collection failures only in an internal XLSX."""
    from src.exporters.instagram_collector import write_xlsx_workbook

    destination = Path(data_dir).resolve()
    workbook = destination / REEL_HISTORY_DIRECTORY / COUNT_HISTORY_XLSX_FILENAME
    workbook.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        _count_history_column(name)
        for name in (
            "collection_number", "collected_at", "url", "user_id", "username",
            "web_status", "web_error", "android_status", "android_error",
            *(
                item
                for metric in COUNT_HISTORY_METRIC_FIELDS
                for item in (metric, f"{metric}_source")
            ),
            "needs_recollection", "recollection_reason",
        )
    ]
    existing_by_key: dict[tuple[str, str], dict[str, str]] = {}
    legacy = not workbook.exists()
    if workbook.exists() and workbook.stat().st_size:
        matrix = _read_xlsx_matrix(workbook)
        if matrix:
            for values in matrix[1:]:
                record = dict(zip(matrix[0], values))
                key = (
                    str(record.get(_count_history_column("url"), "") or ""),
                    _xlsx_reel_value(
                        "collected_at",
                        str(record.get(_count_history_column("collected_at"), "") or ""),
                    ),
                )
                existing_by_key[key] = record

    usernames_by_user_id: dict[str, set[str]] = {}
    for row in rows:
        user_id = str(row.get("user_id", "") or "").strip()
        username = str(row.get("username", "") or "").strip().casefold()
        if user_id and username:
            usernames_by_user_id.setdefault(user_id, set()).add(username)
    conflicting_user_ids = {
        user_id for user_id, usernames in usernames_by_user_id.items() if len(usernames) > 1
    }

    records_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = _count_history_key(row)
        previous = existing_by_key.get(key, {})
        record = {
            _count_history_column("collection_number"): str(row.get("collection_number", "") or ""),
            _count_history_column("collected_at"): key[1],
            _count_history_column("url"): key[0],
            _count_history_column("user_id"): str(row.get("user_id", "") or ""),
            _count_history_column("username"): str(row.get("username", "") or ""),
            _count_history_column("web_status"): previous.get(_count_history_column("web_status"), ""),
            _count_history_column("web_error"): previous.get(_count_history_column("web_error"), ""),
            _count_history_column("android_status"): previous.get(_count_history_column("android_status"), ""),
            _count_history_column("android_error"): previous.get(_count_history_column("android_error"), ""),
        }
        for metric in COUNT_HISTORY_METRIC_FIELDS:
            value = row.get(metric, "")
            source_column = _count_history_column(f"{metric}_source")
            record[_count_history_column(metric)] = str(value if value is not None else "")
            record[source_column] = previous.get(source_column) or _default_count_history_state(
                metric, value, legacy=legacy
            )
        if str(row.get("user_id", "") or "").strip() in conflicting_user_ids:
            record[_count_history_column("follower_count_source")] = "identity_conflict"
        records_by_key[key] = record

    for result in web_results:
        target = result.get("target") if isinstance(result.get("target"), dict) else {}
        record = records_by_key.get(_count_history_key(target))
        if record is None:
            continue
        record[_count_history_column("web_status")] = str(result.get("status", "") or "")
        record[_count_history_column("web_error")] = str(result.get("error", "") or "")[:500]
        for metric in result.get("fallback_exact_fields", ()):
            if metric in COUNT_HISTORY_METRIC_FIELDS:
                record[_count_history_column(f"{metric}_source")] = "web_fallback_exact"

    for result in android_results:
        target = result.get("target") if isinstance(result.get("target"), dict) else {}
        key = _count_history_key(target)
        record = records_by_key.get(key)
        if record is None:
            continue
        status = str(result.get("status", "") or "")
        record[_count_history_column("android_status")] = status
        record[_count_history_column("android_error")] = str(result.get("error", "") or "")[:500]
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        python_metrics = result.get("python_metrics") if isinstance(result.get("python_metrics"), dict) else {}
        compact_fields = set(result.get("compact_fields") or ())
        for metric in COUNT_HISTORY_METRIC_FIELDS[:-1]:
            source_column = _count_history_column(f"{metric}_source")
            if metric in compact_fields:
                if record[source_column] != "web_fallback_exact":
                    record[source_column] = (
                        "android_compact_display" if metric in {"share_count", "saved_count"}
                        else "android_compact_pending_web"
                    )
                continue
            android_value = _count_history_exact_integer(metrics.get(metric))
            python_value = _count_history_exact_integer(python_metrics.get(metric))
            if android_value is not None:
                if python_value is None:
                    record[source_column] = "android_collected"
                elif python_value == android_value:
                    record[source_column] = "python_exact_android_verified"
                else:
                    record[source_column] = "source_conflict"
            elif status != "collected" and record[source_column] == "pending_android":
                record[source_column] = "android_unavailable"
        if result.get("like_count_private") is True:
            record[_count_history_column("like_count_source")] = "private_or_disabled"
        if result.get("comment_count_disabled") is True:
            record[_count_history_column("comment_count_source")] = "private_or_disabled"

    failure_states = {
        "android_unavailable", "identity_conflict", "source_conflict",
        "web_unavailable", "web_compact_untrusted", "pending_android", "android_compact_pending_web",
    }
    for record in records_by_key.values():
        reasons = [
            f"{metric}:{record[_count_history_column(f'{metric}_source')]}"
            for metric in COUNT_HISTORY_METRIC_FIELDS
            if record[_count_history_column(f"{metric}_source")] in failure_states
        ]
        record[_count_history_column("needs_recollection")] = "true" if reasons else "false"
        record[_count_history_column("recollection_reason")] = ", ".join(reasons)

    matrix = [fields, *[[record.get(field, "") for field in fields] for record in records_by_key.values()]]
    write_xlsx_workbook(workbook, [("count_history", matrix)])
    return workbook


def _xlsx_reel_value(field: str, value: str) -> str:
    snapshot = parse_snapshot_field(field)
    base_field = snapshot["baseField"] if snapshot else field
    text = str(value or "").strip()
    if not text:
        return ""
    if base_field in {"collected_at", "uploaded_at"} and re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)", text):
        local_value = datetime(1899, 12, 30) + timedelta(days=float(text))
        local_zone = timezone(timedelta(hours=9))
        return isoformat_utc(local_value.replace(tzinfo=local_zone))
    if base_field in {"view_count", "like_count", "comment_count", "share_count", "repost_count", "saved_count", "follower_count"}:
        metric = re.match(r"^(-?[\d,]+)(?:\([+-][\d,]+\))?$", text)
        if metric:
            return metric.group(1).replace(",", "")
    return text


def long_collected_record_cooldown(
    rows: list[dict[str, Any]],
    record: dict[str, Any],
) -> dict[str, Any] | None:
    matching = [row for row in rows if row.get("url") == record.get("url") and parse_datetime(row.get("collected_at"))]
    if not matching:
        return None
    previous_row = max(matching, key=lambda row: parse_datetime(row.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc))
    previous = parse_datetime(previous_row.get("collected_at"))
    current = parse_datetime(record.get("collected_at"))
    completed = len(matching)
    next_available = next_recollect_available_at(previous, completed)
    policy = recollect_cooldown_policy(completed)
    if previous and current and next_available and previous <= current < next_available:
        return {
            "addedRow": False,
            "skipped": True,
            "label": "Cooldown",
            "cooldownLabel": policy["label"],
            "nextCollectionAt": isoformat_utc(next_available),
            "secondsSincePreviousCollection": math.floor((current - previous).total_seconds()),
        }
    return None


def integrate_long_collected_record(
    rows: list[dict[str, Any]],
    record: dict[str, Any],
    *,
    enforce_cooldown: bool = True,
) -> dict[str, Any]:
    matching = [row for row in rows if row.get("url") == record.get("url")]
    cooldown = long_collected_record_cooldown(rows, record) if enforce_cooldown else None
    if cooldown:
        return cooldown
    previous = max(
        matching,
        key=lambda row: parse_datetime(row.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc),
        default=None,
    )
    collection_number = len(matching) + 1
    label = "Initial" if collection_number == 1 else collection_label(collection_number)
    current = {field: record.get(field, "") for field in CSV_FIELDS}
    if str(current.get("ad", "")).strip().casefold() == "true" or any(
        str(previous_row.get("ad", "")).strip().casefold() == "true" for previous_row in matching
    ):
        # A missing ad marker on a later visit does not negate a confirmed ad.
        current["ad"] = "true"
        for previous_row in matching:
            previous_row["ad"] = "true"
    current["days_since_upload"] = days_since_upload(current.get("uploaded_at"), current.get("collected_at"))
    previous_time = parse_datetime(previous.get("collected_at")) if previous else None
    current_time = parse_datetime(current.get("collected_at"))
    elapsed = (
        js_round(max(0.0, (current_time - previous_time).total_seconds() / 86_400) * 100) / 100
        if previous_time and current_time
        else ""
    )
    row = {
        "collection_number": collection_number,
        "days_since_previous": elapsed,
        **current,
        **calculate_reel_derived_fields(current, previous),
    }
    rows.append(row)
    return {
        "addedRow": True,
        "label": label,
        "collection_number": collection_number,
        "elapsed": elapsed_snapshot_label(previous.get("collected_at"), current.get("collected_at")) if previous else "",
    }


class LongReelStore:
    def __init__(
        self,
        csv_path: Path,
        flush_record_count: int = REEL_STORE_FLUSH_RECORD_COUNT,
        xlsx_layout: str = "columns",
        disable_recollect_cooldown: bool = False,
    ) -> None:
        self.csv_path = csv_path
        self.flush_record_count = flush_record_count
        self.xlsx_layout = xlsx_layout
        self.disable_recollect_cooldown = disable_recollect_cooldown
        self.fields = list(ROW_COLLECTION_FIELDS)
        self.rows: list[dict[str, Any]] = []
        self.journal_path = Path(f"{csv_path}.pending.jsonl")
        self.dirty = False
        self.pending_record_count = 0
        self.lock = asyncio.Lock()
        self.android_overrides: dict[tuple[str, str], set[str]] = {}

    @classmethod
    async def create(
        cls,
        csv_path: Path | str,
        flush_record_count: int = REEL_STORE_FLUSH_RECORD_COUNT,
        xlsx_layout: str = "columns",
        disable_recollect_cooldown: bool = False,
    ) -> "LongReelStore":
        store = cls(Path(csv_path), flush_record_count, xlsx_layout, disable_recollect_cooldown)
        migrated_journal: Path | None = None
        if not store.csv_path.exists() and store.csv_path.name == REEL_HISTORY_FILENAME:
            for legacy_name in LEGACY_REEL_HISTORY_FILENAMES:
                legacy_path = store.csv_path.with_name(legacy_name)
                if not legacy_path.exists() or not legacy_path.stat().st_size:
                    continue
                legacy_fields, legacy_rows = read_csv_objects(legacy_path)
                compatible = all(field in {*ROW_COLLECTION_FIELDS, *LEGACY_REEL_DROPPED_FIELDS} for field in legacy_fields)
                if not compatible:
                    continue
                store.rows = enrich_reel_collection_rows([
                    {field: row.get(field, "") for field in store.fields}
                    for row in legacy_rows
                ])
                await asyncio.to_thread(write_csv_records, store.csv_path, store.rows, store.fields)
                legacy_journal = Path(f"{legacy_path}.pending.jsonl")
                if legacy_journal.exists() and legacy_journal.stat().st_size:
                    store.journal_path.write_text(legacy_journal.read_text(encoding="utf-8"), encoding="utf-8")
                    migrated_journal = legacy_journal
                print(f"잠긴 기존 내부 이력을 새 저장소로 승계했습니다: {len(store.rows)}개")
                break
        if store.csv_path.exists() and store.csv_path.stat().st_size:
            fields, rows = read_csv_objects(store.csv_path)
            # Retired columns are read once and omitted on the next atomic write.
            compatible = all(field in {*ROW_COLLECTION_FIELDS, *LEGACY_REEL_DROPPED_FIELDS} for field in fields)
            if compatible:
                store.rows = enrich_reel_collection_rows([
                    {field: row.get(field, "") for field in store.fields}
                    for row in rows
                ])
            else:
                stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
                legacy = store.csv_path.with_name(f"{store.csv_path.stem}_legacy_{stamp}{store.csv_path.suffix}")
                store.csv_path.replace(legacy)
                print(f"기존 형식 CSV 보관: {legacy}")
        if store.csv_path.name == REEL_HISTORY_FILENAME:
            known = {(str(row.get("url", "")), str(row.get("collected_at", ""))) for row in store.rows}
            recovered_rows: list[dict[str, Any]] = []
            for legacy in sorted(store.csv_path.parent.glob(f"{store.csv_path.stem}_legacy_*{store.csv_path.suffix}")):
                legacy_fields, legacy_rows = read_csv_objects(legacy)
                if not all(field in {*ROW_COLLECTION_FIELDS, *LEGACY_REEL_DROPPED_FIELDS} for field in legacy_fields):
                    continue
                for row in legacy_rows:
                    key = (str(row.get("url", "")), str(row.get("collected_at", "")))
                    if key in known:
                        continue
                    known.add(key)
                    recovered_rows.append({field: row.get(field, "") for field in store.fields})
            if recovered_rows:
                store.rows = enrich_reel_collection_rows([*recovered_rows, *store.rows])
                store.rows = await asyncio.to_thread(
                    _write_history_preserving_external_android_metrics,
                    store.csv_path,
                    store.rows,
                    store.fields,
                )
        await store._recover_journal()
        if migrated_journal is not None:
            migrated_journal.unlink(missing_ok=True)
        return store

    async def _recover_journal(self) -> None:
        if not self.journal_path.exists() or self.journal_path.stat().st_size == 0:
            return
        invalid: list[str] = []
        recovered = 0
        snapshots = {(str(row.get("url", "")), str(row.get("collected_at", ""))) for row in self.rows}
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or not record.get("url") or not parse_datetime(record.get("collected_at")):
                    raise ValueError("invalid journal record")
                key = (str(record["url"]), str(record["collected_at"]))
                if key in snapshots:
                    continue
                integrate_long_collected_record(self.rows, record, enforce_cooldown=False)
                snapshots.add(key)
                recovered += 1
            except (ValueError, TypeError, json.JSONDecodeError):
                invalid.append(line)
        if recovered:
            self.rows = await asyncio.to_thread(
                _write_history_preserving_external_android_metrics,
                self.csv_path, self.rows, self.fields, self.android_overrides,
            )
            print(f"비정상 종료 전 임시 저장 릴스 복구 완료: {recovered}개")
        if invalid:
            corrupt = Path(f"{self.journal_path}.corrupt_{utc_now().strftime('%Y%m%dT%H%M%SZ')}")
            self.journal_path.replace(corrupt)
            print(f"손상된 임시 저장 줄 {len(invalid)}개를 보관했습니다: {corrupt}", file=sys.stderr)
        else:
            self.journal_path.unlink(missing_ok=True)

    async def append(self, record: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            cooldown = None if self.disable_recollect_cooldown else long_collected_record_cooldown(self.rows, record)
            if cooldown:
                return cooldown
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                file.flush()
            result = integrate_long_collected_record(self.rows, record, enforce_cooldown=False)
            self.dirty = True
            self.pending_record_count += 1
            if self.pending_record_count >= self.flush_record_count:
                await self._flush_locked(False)
            return result

    async def enrich_latest_snapshot(
        self, record: dict[str, Any], android_result: AndroidMetricResult | None = None,
        *, fields_to_update: set[str] | None = None,
    ) -> bool:
        """Update the matching row without reverting web fields collected meanwhile.

        The first journal entry deliberately contains browser identity/static
        fields.  Android enrichment then updates that same in-memory snapshot
        rather than appending a second collection row for one Reel visit.
        """
        target_url = str(record.get("url", ""))
        target_collected_at = str(record.get("collected_at", ""))
        async with self.lock:
            for row in reversed(self.rows):
                if str(row.get("url", "")) != target_url or str(row.get("collected_at", "")) != target_collected_at:
                    continue
                key = (target_url, target_collected_at)
                current = merge_android_metrics(row, android_result) if android_result is not None else record
                protected = self.android_overrides.get(key, set()) if android_result is None else set()
                if record_is_korean(row) is True or row.get("korean_source") == "manual":
                    protected = {*protected, "korean", "korean_source", "account_country"}
                selected = fields_to_update if fields_to_update is not None else set(CSV_FIELDS)
                row.update({field: current.get(field, "") for field in CSV_FIELDS if field in selected and field not in protected})
                if fields_to_update is not None:
                    self.android_overrides.setdefault(key, set()).update(fields_to_update)
                if android_result is not None:
                    fields = (set(android_result.metrics) - set(android_result.compact_fields)) & set(ANDROID_COLLECTION_FIELDS)
                    if android_result.recollection and "comment_count" in android_result.metrics and "comment_count" not in android_result.compact_fields:
                        fields.add("comment_count")
                    if android_result.like_count_private is True:
                        fields.add("like_count")
                    self.android_overrides.setdefault(key, set()).update(fields - set(WEB_PREFERRED_COUNT_FIELDS))
                if record.get("android_mismatch"):
                    row["android_mismatch"] = record["android_mismatch"]
                self.rows = enrich_reel_collection_rows(self.rows)
                self.dirty = True
                return True
        return False

    async def _flush_locked(self, force: bool) -> bool:
        if not self.dirty or (not force and self.pending_record_count < self.flush_record_count):
            return False
        saved = self.pending_record_count
        writing = asyncio.create_task(asyncio.to_thread(
            _write_history_preserving_external_android_metrics,
            self.csv_path,
            self.rows,
            self.fields,
            self.android_overrides,
        ))
        try:
            self.rows = await asyncio.shield(writing)
        except asyncio.CancelledError:
            # The thread cannot be cancelled; retain the lock until it finishes.
            await asyncio.gather(writing, return_exceptions=True)
            raise
        self.journal_path.unlink(missing_ok=True)
        self.dirty = False
        self.pending_record_count = 0
        if saved:
            print(f"{'남은 릴스' if force else f'릴스 {self.flush_record_count}개 체크포인트'} 저장 완료: {saved}개")
        return True

    async def flush(self) -> bool:
        async with self.lock:
            return await self._flush_locked(True)

    async def export_outputs(self) -> dict[str, Path]:
        async with self.lock:
            writing = asyncio.create_task(asyncio.to_thread(
                _export_history_with_external_android_metrics,
                self.csv_path,
                self.rows,
                self.fields,
                self.xlsx_layout,
                self.android_overrides,
            ))
            try:
                self.rows, outputs = await asyncio.shield(writing)
            except asyncio.CancelledError:
                await asyncio.gather(writing, return_exceptions=True)
                raise
            return outputs

    async def merge_external_android_metrics(self) -> int:
        """Refresh this process's rows after the detached worker updates history."""
        async with self.lock:
            if not self.csv_path.exists() or not self.csv_path.stat().st_size:
                return 0
            _fields, disk_rows = await asyncio.to_thread(read_csv_objects, self.csv_path)
            self.rows, changed = _merge_external_android_metrics(self.rows, disk_rows, self.android_overrides)
            return changed

    def stats(self) -> dict[str, Any]:
        return {"rows": len(self.rows), "pending": self.pending_record_count, "journalPath": str(self.journal_path)}


def _merge_external_android_metrics(
    local_rows: list[dict[str, Any]],
    external_rows: Sequence[dict[str, Any]],
    local_android_fields: dict[tuple[str, str], set[str]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    external_by_target = {
        (str(row.get("url", "")), str(row.get("collected_at", ""))): row
        for row in external_rows
    }
    changed = 0
    for row in local_rows:
        key = (str(row.get("url", "")), str(row.get("collected_at", "")))
        external = external_by_target.get(key)
        if external is None:
            continue
        if external.get("korean_source") == "manual" or (
            row.get("korean_source") != "manual" and record_is_korean(external) is not None
            and (record_is_korean(external) is True or record_is_korean(row) is not True)
        ):
            row.update(classification_fields(external))
            changed += 1
        if exact_visible_reel_count(str(row.get("view_count", ""))) is None or (
            external.get("view_count_source") == "instagram_android_profile"
            and row.get("view_count_source") != "instagram_android_profile"
        ):
            view = exact_visible_reel_count(str(external.get("view_count", "")))
            if view is not None and apply_observed_view_count(
                local_rows, row, view, str(external.get("view_count_source") or "legacy_external"),
                str(external.get("view_count_observed_at") or external.get("collected_at") or ""),
            ):
                changed += 1
        mismatch = str(external.get("android_mismatch", "") or "")
        if mismatch and row.get("android_mismatch") != mismatch:
            row["android_mismatch"] = mismatch
            changed += 1
        for field in ANDROID_METRIC_FIELDS:
            if field in (local_android_fields or {}).get(key, set()):
                continue
            value = external.get(field, "")
            if value in (None, "") or str(row.get(field, "")) == str(value):
                continue
            if field in WEB_PREFERRED_COUNT_FIELDS and has_web_count(row.get(field)):
                continue
            elif field not in {*ANDROID_COLLECTION_FIELDS, "comment_count"}:
                continue
            row[field] = value
            changed += 1
    return enrich_reel_collection_rows(local_rows), changed


def _history_data_dir(csv_path: Path) -> Path:
    return csv_path.parent.parent if csv_path.parent.name == REEL_HISTORY_DIRECTORY else csv_path.parent


def _write_history_preserving_external_android_metrics(
    csv_path: Path,
    local_rows: list[dict[str, Any]],
    fields: list[str],
    local_android_fields: dict[tuple[str, str], set[str]] | None = None,
) -> list[dict[str, Any]]:
    paths = _android_metric_queue_paths(_history_data_dir(csv_path))
    lock = AtomicProcessLock(paths["history_lock"], wait_seconds=30)
    if not lock.acquire():
        raise RuntimeError(f"Timed out waiting to write Reel history; storage lock is busy: {lock.path}")
    try:
        disk_rows: list[dict[str, Any]] = []
        if csv_path.exists() and csv_path.stat().st_size:
            _disk_fields, disk_rows = read_csv_objects(csv_path)
        merged, _changed = _merge_external_android_metrics(local_rows, disk_rows, local_android_fields)
        merged = apply_korean_overrides(merged, _history_data_dir(csv_path))
        fields = list(dict.fromkeys([*fields, "korean", "korean_source", "account_country"]))
        write_csv_records(csv_path, merged, fields)
        return merged
    finally:
        lock.release()


def _export_history_with_external_android_metrics(
    csv_path: Path,
    local_rows: list[dict[str, Any]],
    fields: list[str],
    xlsx_layout: str,
    local_android_fields: dict[tuple[str, str], set[str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    paths = _android_metric_queue_paths(_history_data_dir(csv_path))
    lock = AtomicProcessLock(paths["history_lock"], wait_seconds=30)
    if not lock.acquire():
        raise RuntimeError(f"Timed out waiting to export Reel history; storage lock is busy: {lock.path}")
    try:
        disk_rows: list[dict[str, Any]] = []
        if csv_path.exists() and csv_path.stat().st_size:
            _disk_fields, disk_rows = read_csv_objects(csv_path)
        merged, _changed = _merge_external_android_metrics(local_rows, disk_rows, local_android_fields)
        merged = apply_korean_overrides(merged, _history_data_dir(csv_path))
        fields = list(dict.fromkeys([*fields, "korean", "korean_source", "account_country"]))
        return merged, write_long_output_bundle(csv_path, merged, fields, xlsx_layout=xlsx_layout)
    finally:
        lock.release()


def exact_visible_reel_count(value: Any) -> int | None:
    """Accept only a fully rendered integer, never a compact metric label."""
    text = str(value or "").replace("\u00a0", " ").strip()
    if not re.fullmatch(r"\d+|\d{1,3}(?:,\d{3})+", text):
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def apply_observed_view_count(
    rows: Iterable[dict[str, Any]], row: dict[str, Any], count: int, source: str, observed_at: str,
) -> bool:
    """Never insert a later observation into an already superseded snapshot."""
    if exact_visible_reel_count(str(row.get("view_count", ""))) is not None:
        return False
    collected = parse_datetime(row.get("collected_at"))
    observed = parse_datetime(observed_at)
    if collected is None or observed is None or observed < collected:
        return False
    if any(other.get("url") == row.get("url") and (when := parse_datetime(other.get("collected_at")))
           and collected < when <= observed for other in rows):
        return False
    row.update(view_count=count, view_count_observed_at=observed_at, view_count_source=source)
    return True

