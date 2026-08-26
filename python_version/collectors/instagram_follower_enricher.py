"""Follower history and queue management for the Python Instagram collector."""

from __future__ import annotations

import asyncio
import csv
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


USER_FIELDS = [
    "user_id",
    "username",
    "biography",
    "follower_count",
    "collected_at",
]
FOLLOWER_SNAPSHOT_FIELDS = ["follower_count", "collected_at"]
LEGACY_FOLLOWER_TIMESTAMP_FIELD = "follower_count_collected_at"
USER_HISTORY_DIRECTORY = ".collector"
USER_HISTORY_FILENAME = "users_history_active.csv"
USER_FLUSH_CHANGE_COUNT = 500
MAX_CONSECUTIVE_WEB_ERRORS = 5
FOLLOWER_CACHE_HOURS = 6

Lookup = Callable[[dict[str, str]], Awaitable[dict[str, Any]]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _exact_nonnegative_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _validate_exact_follower_result(result: dict[str, Any]) -> dict[str, Any]:
    validated = dict(result)
    if validated.get("status") != "success":
        return validated
    source_field = validated.get("sourceField")
    if source_field != "edge_followed_by.count":
        validated.update({
            "status": "untrusted_follower_source",
            "error": f"Rejected follower count from untrusted source field: {source_field or '<missing>'}.",
            "followerCount": None,
        })
        return validated
    exact_count = _exact_nonnegative_integer(validated.get("followerCount"))
    if exact_count is None:
        validated.update({
            "status": "invalid_follower_count",
            "error": "edge_followed_by.count was not an exact nonnegative integer.",
            "followerCount": None,
        })
        return validated
    validated["followerCount"] = exact_count
    return validated


def read_csv_objects(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists() or path.stat().st_size == 0:
        return [], []
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def atomic_write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore", lineterminator="\r\n")
            writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def user_history_path(data_dir: Path | str) -> Path:
    return Path(data_dir).resolve() / USER_HISTORY_DIRECTORY / USER_HISTORY_FILENAME


def ensure_user_history(data_dir: Path | str) -> Path:
    """Migrate the old public users.csv into the collector-only active history once."""
    destination = Path(data_dir).resolve()
    history_path = user_history_path(destination)
    if history_path.exists() and history_path.stat().st_size:
        return history_path
    fields, rows = read_csv_objects(destination / "users.csv")
    if fields:
        migrated_fields, migrated_rows = migrate_user_rows(fields, rows)
        atomic_write_csv(history_path, migrated_rows, migrated_fields)
    return history_path


def ordinal(number: int) -> str:
    remainder = number % 100
    if 11 <= remainder <= 13:
        return f"{number}th"
    return f"{number}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th') }"


def follower_snapshot_labels(fields: list[str]) -> list[str]:
    labels: list[str] = []
    for field in fields:
        match = re.match(r"^(\d+(?:st|nd|rd|th) collect)_(.+)$", field)
        if (
            match
            and match.group(2) in {*FOLLOWER_SNAPSHOT_FIELDS, LEGACY_FOLLOWER_TIMESTAMP_FIELD}
            and match.group(1) not in labels
        ):
            labels.append(match.group(1))
    return labels


def follower_snapshot_field(label: str, field: str) -> str:
    return field if label == "Initial" else f"{label}_{field}"


def latest_follower_snapshot(row: dict[str, Any], fields: list[str]) -> dict[str, str]:
    count = str(row.get("follower_count", "") or "")
    collected_at = str(row.get("collected_at", "") or row.get(LEGACY_FOLLOWER_TIMESTAMP_FIELD, "") or "")
    for label in follower_snapshot_labels(fields):
        snapshot_count = str(row.get(follower_snapshot_field(label, "follower_count"), "") or "")
        snapshot_time = str(
            row.get(follower_snapshot_field(label, "collected_at"), "")
            or row.get(follower_snapshot_field(label, LEGACY_FOLLOWER_TIMESTAMP_FIELD), "")
            or ""
        )
        if snapshot_count:
            count = snapshot_count
        if snapshot_time:
            collected_at = snapshot_time
    return {"follower_count": count, "collected_at": collected_at}


def migrate_user_rows(
    fields: list[str], rows: list[dict[str, str]]
) -> tuple[list[str], list[dict[str, str]]]:
    """Rename the retired follower timestamp columns without losing history."""
    labels = follower_snapshot_labels(fields)
    migrated_fields = [
        *USER_FIELDS,
        *(f"{label}_{field}" for label in labels for field in FOLLOWER_SNAPSHOT_FIELDS),
    ]
    migrated_rows: list[dict[str, str]] = []
    for source in rows:
        migrated: dict[str, str] = {}
        for field in migrated_fields:
            legacy_field = (
                LEGACY_FOLLOWER_TIMESTAMP_FIELD
                if field == "collected_at"
                else field.replace("_collected_at", f"_{LEGACY_FOLLOWER_TIMESTAMP_FIELD}")
                if field.endswith("_collected_at")
                else field
            )
            migrated[field] = str(source.get(field, "") or source.get(legacy_field, "") or "")
        migrated_rows.append(migrated)
    return migrated_fields, migrated_rows


class FollowerEnricher:
    """Collect follower counts in the background and persist horizontal snapshots."""

    def __init__(
        self,
        *,
        data_dir: Path | str,
        lookup_impl: Lookup,
        concurrency: int = 1,
        source: str = "instagram_web",
        on_progress: Callable[[dict[str, Any]], Any] | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if not callable(lookup_impl):
            raise TypeError("FollowerEnricher requires a web lookup function.")
        self.data_dir = Path(data_dir).resolve()
        self.users_path = user_history_path(self.data_dir)
        self.lookup_impl = lookup_impl
        self.concurrency = max(1, min(10, int(concurrency or 1)))
        self.source = source
        self.on_progress = on_progress
        self.now = now
        self.user_fields = list(USER_FIELDS)
        self.users: dict[str, dict[str, Any]] = {}
        self.user_id_index: dict[str, dict[str, Any]] = {}
        self.username_index: dict[str, dict[str, Any]] = {}
        self.last_lookup_at: dict[str, str] = {}
        self.queue: list[dict[str, Any]] = []
        self.queued: set[str] = set()
        self.active_tasks: set[asyncio.Task[None]] = set()
        self.stopped = False
        self.loaded = False
        self.dirty_changes = 0
        self.flush_task: asyncio.Task[None] | None = None
        self.flush_task_is_delayed = False
        self.write_lock = asyncio.Lock()
        self.consecutive_web_errors = 0
        self.stats: dict[str, Any] = {
            "queued": 0,
            "success": 0,
            "unavailable": 0,
            "failed": 0,
            "completed": 0,
            "stopStatus": "",
            "stopError": "",
        }

    async def ready(self) -> None:
        if self.loaded:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        ensure_user_history(self.data_dir)
        fields, rows = read_csv_objects(self.users_path)
        if fields:
            self.user_fields, rows = migrate_user_rows(fields, rows)
            if fields != self.user_fields:
                atomic_write_csv(self.users_path, rows, self.user_fields)
        for source in rows:
            row = {field: source.get(field, "") for field in self.user_fields}
            key = f"id:{row['user_id']}" if row["user_id"] else f"username:{row['username'].lower()}"
            if key.endswith(":"):
                continue
            row["_key"] = key
            self.users[key] = row
            self._index_row(row)
            latest = latest_follower_snapshot(row, self.user_fields)
            if latest["collected_at"]:
                self.last_lookup_at[key] = latest["collected_at"]
        self.loaded = True

    def set_lookup_impl(self, lookup_impl: Lookup) -> None:
        if not callable(lookup_impl):
            raise TypeError("FollowerEnricher requires a web lookup function.")
        self.lookup_impl = lookup_impl

    def _index_row(self, row: dict[str, Any], previous_username: str = "") -> None:
        if row.get("user_id"):
            self.user_id_index[str(row["user_id"])] = row
        if previous_username and self.username_index.get(previous_username.lower()) is row:
            self.username_index.pop(previous_username.lower(), None)
        if row.get("username"):
            self.username_index[str(row["username"]).lower()] = row

    def _find_or_create(self, user_id: str, username: str) -> dict[str, Any]:
        row = self.user_id_index.get(user_id) if user_id else None
        if row is None and username:
            row = self.username_index.get(username.lower())
        if row is None:
            key = f"id:{user_id}" if user_id else f"username:{username.lower()}"
            row = {field: "" for field in self.user_fields}
            row["_key"] = key
            self.users[key] = row
        previous_username = str(row.get("username", ""))
        if user_id:
            row["user_id"] = user_id
        if username:
            row["username"] = username
        self._index_row(row, previous_username)
        return row

    def _is_fresh(self, row: dict[str, Any]) -> bool:
        raw = self.last_lookup_at.get(str(row["_key"])) or latest_follower_snapshot(row, self.user_fields)["collected_at"]
        timestamp = parse_timestamp(raw) if raw else None
        return timestamp is not None and self.now().astimezone(timezone.utc) - timestamp < timedelta(hours=FOLLOWER_CACHE_HOURS)

    def _next_collection_label(self, row: dict[str, Any]) -> str:
        completed = int(bool(row.get("collected_at")))
        completed += sum(
            bool(row.get(follower_snapshot_field(label, "collected_at")))
            for label in follower_snapshot_labels(self.user_fields)
        )
        return f"{ordinal(completed + 1)} collect" if completed else "Initial"

    def _ensure_snapshot_fields(self, label: str) -> None:
        if label == "Initial":
            return
        for field in FOLLOWER_SNAPSHOT_FIELDS:
            snapshot_field = follower_snapshot_field(label, field)
            if snapshot_field not in self.user_fields:
                self.user_fields.append(snapshot_field)
                for row in self.users.values():
                    row.setdefault(snapshot_field, "")

    def _record_snapshot(self, row: dict[str, Any], label: str, count: Any, collected_at: str) -> None:
        self._ensure_snapshot_fields(label)
        row[follower_snapshot_field(label, "follower_count")] = str(count)
        row[follower_snapshot_field(label, "collected_at")] = collected_at

    def _enqueue(self, row: dict[str, Any], *, force: bool = False) -> bool:
        key = str(row["_key"])
        has_biography = bool(str(row.get("biography", "") or "").strip())
        if self.stopped or not row.get("username") or key in self.queued or (not force and has_biography and self._is_fresh(row)):
            return False
        row["lookup_status"] = "queued"
        row["last_error"] = ""
        self.queue.append(row)
        self.queued.add(key)
        self.stats["queued"] += 1
        self._pump()
        return True

    async def track_user(
        self,
        *,
        user_id: Any = "",
        username: Any = "",
        seen_at: str = "",
        enqueue: bool = True,
    ) -> dict[str, Any] | None:
        await self.ready()
        normalized_id = str(user_id or "").strip()
        normalized_username = str(username or "").strip().lstrip("@")
        if not normalized_id and not normalized_username:
            return None
        row = self._find_or_create(normalized_id, normalized_username)
        if enqueue:
            self._enqueue(row)
        self._mark_dirty()
        latest = latest_follower_snapshot(row, self.user_fields)
        return {**row, **latest}

    async def enqueue_all(self) -> int:
        await self.ready()
        count = sum(1 for row in list(self.users.values()) if self._enqueue(row))
        self._mark_dirty()
        return count

    async def enqueue_all_exact(self) -> int:
        """Refresh every known user even when a legacy snapshot is still fresh."""
        await self.ready()
        count = sum(1 for row in list(self.users.values()) if self._enqueue(row, force=True))
        self._mark_dirty()
        return count

    async def lookup_user_now(
        self,
        *,
        user_id: Any = "",
        username: Any = "",
        seen_at: str = "",
    ) -> dict[str, Any]:
        """Run a fresh lookup immediately, bypassing legacy cached values."""
        del seen_at
        await self.ready()
        normalized_id = str(user_id or "").strip()
        normalized_username = str(username or "").strip().lstrip("@")
        if not normalized_id and not normalized_username:
            return {"status": "profile_unavailable", "error": "Missing Instagram user identity.", "follower_count": ""}
        row = self._find_or_create(normalized_id, normalized_username)
        self.stats["queued"] += 1
        result = await self._lookup_one(row)
        latest = latest_follower_snapshot(row, self.user_fields)
        succeeded = result.get("status") == "success"
        return {
            **row,
            **latest,
            "status": str(result.get("status", "web_error")),
            "error": str(result.get("error", "")),
            "followerCount": result.get("followerCount") if succeeded else None,
            "sourceField": result.get("sourceField"),
        }

    def _pump(self) -> None:
        while not self.stopped and self.queue and len(self.active_tasks) < self.concurrency:
            row = self.queue.pop(0)
            task = asyncio.create_task(self._lookup_one(row))
            self.active_tasks.add(task)
            task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        self.active_tasks.discard(task)
        try:
            task.result()
        except Exception:
            # _lookup_one converts lookup failures to a result; this only protects the queue.
            pass
        self._pump()

    async def _lookup_one(self, row: dict[str, Any]) -> dict[str, Any]:
        key = str(row["_key"])
        label = self._next_collection_label(row)
        try:
            result = await self.lookup_impl({"username": str(row.get("username", "")), "userId": str(row.get("user_id", ""))})
        except Exception as error:
            result = {"status": "web_error", "error": str(error)[:500], "source": self.source}
        result = _validate_exact_follower_result(result)
        collected_at = isoformat_utc(self.now())
        self.last_lookup_at[key] = collected_at
        status = str(result.get("status", "web_error"))
        row["lookup_status"] = status
        row["last_error"] = str(result.get("error", ""))[:500]
        if "biography" in result:
            row["biography"] = str(result.get("biography") or "")
        if status == "success":
            self._record_snapshot(row, label, result.get("followerCount", ""), collected_at)
            self.stats["success"] += 1
        elif status in {"not_professional_or_unavailable", "profile_unavailable"}:
            self.stats["unavailable"] += 1
        else:
            self.stats["failed"] += 1
        self.consecutive_web_errors = self.consecutive_web_errors + 1 if status == "web_error" else 0
        self.stats["completed"] += 1
        if self.on_progress:
            try:
                self.on_progress({
                    "completed": self.stats["completed"],
                    "queued": self.stats["queued"],
                    "username": row.get("username", ""),
                    "status": status,
                    "followerCount": result.get("followerCount", "") if status == "success" else "",
                    "error": row["last_error"],
                })
            except Exception:
                pass
        self._mark_dirty()
        repeated_error = self.consecutive_web_errors >= MAX_CONSECUTIVE_WEB_ERRORS
        if status in {"rate_limited", "login_required", "challenge_required"} or repeated_error:
            if not self.stats["stopStatus"]:
                self.stats["stopStatus"] = "repeated_web_error" if repeated_error else status
                self.stats["stopError"] = (
                    f"{MAX_CONSECUTIVE_WEB_ERRORS} consecutive follower web errors."
                    if repeated_error else row["last_error"] or "Follower lookup stopped."
                )
            self.stopped = True
            deferred_status = f"deferred_{self.stats['stopStatus']}"
            for pending in self.queue:
                pending["lookup_status"] = deferred_status
                self.queued.discard(str(pending["_key"]))
            self.queue.clear()
            self._mark_dirty()
        self.queued.discard(key)
        return result

    def _mark_dirty(self) -> None:
        self.dirty_changes += 1
        if self.dirty_changes >= USER_FLUSH_CHANGE_COUNT:
            if self.flush_task and not self.flush_task.done() and self.flush_task_is_delayed:
                self.flush_task.cancel()
                self.flush_task = None
            if self.flush_task is None or self.flush_task.done():
                self.flush_task_is_delayed = False
                self.flush_task = asyncio.create_task(self._flush_users())
        elif self.flush_task is None or self.flush_task.done():
            self.flush_task_is_delayed = True
            self.flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(60)
        self.flush_task_is_delayed = False
        await self._flush_users()

    async def _flush_users(self) -> None:
        if not self.dirty_changes:
            return
        async with self.write_lock:
            while self.dirty_changes:
                self.dirty_changes = 0
                fields = list(self.user_fields)
                rows = [
                    {field: row.get(field, "") for field in fields}
                    for row in sorted(self.users.values(), key=lambda item: str(item.get("username", "")).lower())
                ]
                await asyncio.to_thread(atomic_write_csv, self.users_path, rows, fields)

    async def drain(self) -> dict[str, Any]:
        await self.ready()
        while self.queue or self.active_tasks:
            self._pump()
            if self.active_tasks:
                await asyncio.wait(self.active_tasks, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(0)
        if self.flush_task and not self.flush_task.done():
            if self.flush_task_is_delayed:
                self.flush_task.cancel()
                try:
                    await self.flush_task
                except asyncio.CancelledError:
                    pass
            else:
                await self.flush_task
        await self._flush_users()
        return {**self.stats, "stopped": self.stopped}
