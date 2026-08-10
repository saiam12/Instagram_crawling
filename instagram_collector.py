"""Instagram Professional-account time-series collector.

The collector uses Meta's official Instagram Graph API. It supports the
authorized Professional account and public Professional accounts available
through Business Discovery. Repeated runs append engagement snapshots to CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import tempfile
import time
import uuid
import zipfile
from xml.etree import ElementTree
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape as xml_escape


DEFAULT_API_VERSION = "v26.0"
DEFAULT_USAGE_THRESHOLD = 90.0
DEFAULT_PAGE_SIZE = 100
XLSX_FILENAME = "instagram_data.xlsx"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]{1,30}$")
UNSUPPORTED_FIELD_PATTERN = re.compile(
    r"Tried accessing nonexisting field \((?P<field>[^)]+)\)", re.IGNORECASE
)
RATE_LIMIT_ERROR_CODES = {4, 17, 32, 613, 80001, 80002}
XLSX_NUMERIC_FIELDS = {
    "followers_count",
    "follower_count",
    "follows_count",
    "media_count",
    "like_count",
    "comment_count",
    "repost_count",
    "comments_count",
    "request_number",
    "call_count",
    "total_cputime",
    "total_time",
    "estimated_time_to_regain_access",
    "sample_size",
    "offset_hours",
    "attempts",
    "delay_minutes",
    "targets_requested",
    "targets_completed",
    "posts_seen",
    "requests_made",
    "max_usage_percent",
}
XLSX_DECIMAL_FIELDS = {"location_latitude", "location_longitude"}
XLSX_TWO_DECIMAL_FIELDS = {"days_since_upload"}
XLSX_PERCENT_FIELDS = {"reaction_rate"}
XLSX_DELTA_FIELDS = {
    "like_count",
    "comment_count",
    "repost_count",
    "follower_count",
}
XLSX_DATE_FIELDS = {
    "added_at",
    "collected_at",
    "completed_at",
    "created_at",
    "first_seen_at",
    "last_seen_at",
    "published_at",
    "scheduled_for",
    "started_at",
    "finished_at",
    "uploaded_at",
    "follower_count_collected_at",
    "last_lookup_at",
}
XLSX_TEXT_IDENTIFIER_FIELDS = {
    "user_id",
    "api_user_id",
    "post_id",
}
XLSX_USERS_FIELDS = [
    "user_id",
    "username",
    "follower_count_collected_at",
    "follower_count",
    "api_user_id",
    "lookup_status",
]
XLSX_REELS_WEB_FIELDS = [
    "url",
    "collected_at",
    "user_id",
    "username",
    "title",
    "hashtags",
    "audio_name",
    "ad",
    "uploaded_at",
    "days_since_upload",
    "like_count",
    "comment_count",
    "repost_count",
    "follower_count",
    "reaction_rate",
    "follower_count_collected_at",
]
XLSX_REELS_WEB_HIDDEN_FIELDS = {
    "location_name",
    "location_latitude",
    "location_longitude",
    "follower_lookup_status",
}
XLSX_FOLLOWER_LOOKUP_FIELDS = [
    "collected_at",
    "user_id",
    "username",
    "api_user_id",
    "follower_count",
    "error",
]
SCHEDULE_PRESETS: dict[str, list[tuple[str, float]]] = {
    "test": [("baseline", 0), ("1h", 1), ("4h", 4), ("12h", 12), ("24h", 24)],
    "production": [
        ("baseline", 0),
        ("6h", 6),
        ("12h", 12),
        ("1d", 24),
        ("3d", 72),
        ("1w", 168),
        ("2w", 336),
    ],
}

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TARGETS_FILE = BASE_DIR / "targets.csv"
DEFAULT_DATA_DIR = BASE_DIR / "data"

TARGET_FIELDS = [
    "target_key",
    "target_type",
    "username",
    "enabled",
    "notes",
    "added_at",
]
ACCOUNT_SNAPSHOT_FIELDS = [
    "collected_at",
    "run_id",
    "experiment_id",
    "milestone",
    "scheduled_for",
    "target_key",
    "target_type",
    "username",
    "name",
    "biography",
    "website",
    "profile_picture_url",
    "followers_count",
    "follows_count",
    "media_count",
]
POST_FIELDS = [
    "target_key",
    "target_type",
    "target_username",
    "post_id",
    "post_username",
    "caption",
    "media_type",
    "media_product_type",
    "permalink",
    "media_url",
    "thumbnail_url",
    "published_at",
    "first_seen_at",
    "last_seen_at",
]
MEDIA_SNAPSHOT_FIELDS = [
    "collected_at",
    "run_id",
    "experiment_id",
    "milestone",
    "scheduled_for",
    "target_key",
    "target_username",
    "post_id",
    "like_count",
    "comments_count",
]
USAGE_FIELDS = [
    "observed_at",
    "run_id",
    "experiment_id",
    "milestone",
    "scheduled_for",
    "request_number",
    "context",
    "header_source",
    "call_count",
    "total_cputime",
    "total_time",
    "estimated_time_to_regain_access",
]
RUN_FIELDS = [
    "run_id",
    "experiment_id",
    "milestone",
    "scheduled_for",
    "started_at",
    "finished_at",
    "status",
    "targets_requested",
    "targets_completed",
    "posts_seen",
    "requests_made",
    "max_usage_percent",
    "message",
]
EXPERIMENT_FIELDS = [
    "experiment_id",
    "created_at",
    "schedule_name",
    "sample_size",
    "random_seed",
    "target_keys",
    "status",
]
EXPERIMENT_JOB_FIELDS = [
    "job_id",
    "experiment_id",
    "target_key",
    "milestone",
    "offset_hours",
    "scheduled_for",
    "status",
    "attempts",
    "last_attempt_at",
    "completed_at",
    "delay_minutes",
    "run_id",
    "last_error",
]

# ``biography``, ``website``, and ``profile_picture_url`` remain CSV columns
# for compatibility, but Facebook Login's Instagram Graph API does not expose
# them on this node.
ACCOUNT_FIELDS = [
    "id",
    "username",
    "name",
    "followers_count",
    "follows_count",
    "media_count",
]
MEDIA_FIELDS = [
    "id",
    "username",
    "caption",
    "media_type",
    "media_product_type",
    "permalink",
    "media_url",
    "thumbnail_url",
    "timestamp",
    "like_count",
    "comments_count",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_dotenv(path: Path) -> None:
    """Load a minimal .env file without overwriting existing environment values."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def append_csv(path: Path, rows: Iterable[Mapping[str, Any]], fieldnames: list[str]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_csv_schema(path, fieldnames)
    new_file = not path.exists() or path.stat().st_size == 0
    encoding = "utf-8-sig" if new_file else "utf-8"
    with path.open("a", newline="", encoding=encoding) as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def ensure_csv_schema(path: Path, fieldnames: list[str]) -> None:
    """Add newly introduced columns before appending to an existing CSV."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        current_fields = reader.fieldnames or []
        if current_fields == fieldnames:
            return
        rows = list(reader)
    atomic_write_csv(path, rows, fieldnames)


def atomic_write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8-sig",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_name = file.name
            writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


@dataclass
class Target:
    target_key: str
    target_type: str
    username: str
    enabled: bool
    notes: str
    added_at: str

    def as_row(self) -> dict[str, Any]:
        return {
            "target_key": self.target_key,
            "target_type": self.target_type,
            "username": self.username,
            "enabled": "true" if self.enabled else "false",
            "notes": self.notes,
            "added_at": self.added_at,
        }


class TargetRegistry:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[Target]:
        if not self.path.exists():
            return []
        with self.path.open("r", newline="", encoding="utf-8-sig") as file:
            return [
                Target(
                    target_key=row.get("target_key", "").strip(),
                    target_type=row.get("target_type", "business_discovery").strip(),
                    username=row.get("username", "").strip(),
                    enabled=parse_bool(row.get("enabled")),
                    notes=row.get("notes", "").strip(),
                    added_at=row.get("added_at", "").strip(),
                )
                for row in csv.DictReader(file)
                if row.get("target_key", "").strip()
            ]

    def save(self, targets: list[Target]) -> None:
        atomic_write_csv(self.path, (target.as_row() for target in targets), TARGET_FIELDS)

    def add(self, username: str | None, is_self: bool, notes: str) -> Target:
        targets = self.load()
        if is_self:
            key = "self"
            target_type = "authorized"
            normalized_username = ""
        else:
            normalized_username = (username or "").strip().lstrip("@").lower()
            if not USERNAME_PATTERN.fullmatch(normalized_username):
                raise ValueError("Instagram username must contain only letters, numbers, periods, and underscores.")
            key = normalized_username
            target_type = "business_discovery"
        if any(target.target_key.lower() == key.lower() for target in targets):
            raise ValueError(f"Target '{key}' already exists.")
        target = Target(key, target_type, normalized_username, True, notes.strip(), utc_now())
        targets.append(target)
        self.save(targets)
        return target

    def import_usernames(self, usernames: Iterable[str], notes: str = "imported pool") -> tuple[int, int, int]:
        targets = self.load()
        existing = {target.target_key.lower() for target in targets}
        added = 0
        skipped = 0
        invalid = 0
        for raw_username in usernames:
            username = raw_username.strip().lstrip("@").lower()
            if not username:
                continue
            if not USERNAME_PATTERN.fullmatch(username):
                invalid += 1
                continue
            if username in existing:
                skipped += 1
                continue
            targets.append(
                Target(username, "business_discovery", username, True, notes, utc_now())
            )
            existing.add(username)
            added += 1
        if added:
            self.save(targets)
        return added, skipped, invalid

    def set_enabled(self, key: str, enabled: bool) -> Target:
        targets = self.load()
        for target in targets:
            if target.target_key.lower() == key.lower().lstrip("@"):
                target.enabled = enabled
                self.save(targets)
                return target
        raise ValueError(f"Target '{key}' was not found.")

    def remove(self, key: str) -> Target:
        targets = self.load()
        normalized = key.lower().lstrip("@")
        kept = [target for target in targets if target.target_key.lower() != normalized]
        if len(kept) == len(targets):
            raise ValueError(f"Target '{key}' was not found.")
        removed = next(target for target in targets if target.target_key.lower() == normalized)
        self.save(kept)
        return removed

    def select(self, keys: list[str] | None) -> list[Target]:
        targets = self.load()
        if not keys:
            return [target for target in targets if target.enabled]
        wanted = {key.lower().lstrip("@") for key in keys}
        selected = [target for target in targets if target.target_key.lower() in wanted]
        missing = wanted - {target.target_key.lower() for target in selected}
        if missing:
            raise ValueError(f"Unknown target(s): {', '.join(sorted(missing))}")
        return selected


@dataclass
class APIUsage:
    call_count: float = 0.0
    total_cputime: float = 0.0
    total_time: float = 0.0
    estimated_time_to_regain_access: float = 0.0
    header_source: str = ""

    @property
    def max_percent(self) -> float:
        return max(self.call_count, self.total_cputime, self.total_time)

    def update(self, headers: Mapping[str, Any] | None) -> bool:
        if headers is None:
            return False
        candidates = [
            ("X-Business-Use-Case-Usage", headers.get("X-Business-Use-Case-Usage")),
            ("X-App-Usage", headers.get("X-App-Usage")),
        ]
        updated = False
        for source, raw_value in candidates:
            if not raw_value:
                continue
            try:
                payload = json.loads(str(raw_value))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for record in iter_usage_records(payload):
                self.call_count = max(self.call_count, to_float(record.get("call_count")))
                self.total_cputime = max(self.total_cputime, to_float(record.get("total_cputime")))
                self.total_time = max(self.total_time, to_float(record.get("total_time")))
                self.estimated_time_to_regain_access = max(
                    self.estimated_time_to_regain_access,
                    to_float(record.get("estimated_time_to_regain_access")),
                )
                self.header_source = source
                updated = True
        return updated


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def iter_usage_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if any(key in value for key in ("call_count", "total_cputime", "total_time")):
            yield value
        for nested in value.values():
            yield from iter_usage_records(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_usage_records(nested)


class GraphAPIError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class RateLimitReached(GraphAPIError):
    def __init__(self, message: str, usage: APIUsage):
        super().__init__(message, code=80002)
        self.usage = usage


class GraphAPIClient:
    def __init__(
        self,
        access_token: str,
        api_version: str,
        usage_threshold: float,
        request_delay: float,
        timeout: float,
        retries: int,
    ):
        self.access_token = access_token
        self.api_version = api_version if api_version.startswith("v") else f"v{api_version}"
        self.usage_threshold = usage_threshold
        self.request_delay = request_delay
        self.timeout = timeout
        self.retries = retries
        self.usage = APIUsage()
        self.request_count = 0
        self.stop_before_next = False
        self.last_request_at = 0.0
        self.usage_events: list[dict[str, Any]] = []

    def get(self, path: str, params: Mapping[str, Any], context: str) -> dict[str, Any]:
        if self.stop_before_next:
            raise RateLimitReached(
                f"API usage reached the configured {self.usage_threshold:g}% threshold.",
                self.usage,
            )

        query = dict(params)
        query["access_token"] = self.access_token
        clean_path = path if path.startswith("/") else f"/{path}"
        url = f"https://graph.facebook.com/{self.api_version}{clean_path}?{urlencode(query)}"

        for attempt in range(self.retries + 1):
            self._pace_requests()
            self.request_count += 1
            request_number = self.request_count
            request = Request(url, headers={"User-Agent": "instagram-timeseries-collector/1.0"})
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw_body = response.read().decode("utf-8")
                    self._record_usage(response.headers, request_number, context)
                payload = json.loads(raw_body)
                if isinstance(payload, dict) and payload.get("error"):
                    self._raise_graph_error(payload, None)
                if not isinstance(payload, dict):
                    raise GraphAPIError("Graph API returned an unexpected response type.")
                return payload
            except HTTPError as error:
                raw_body = error.read().decode("utf-8", errors="replace")
                self._record_usage(error.headers, request_number, context)
                payload = parse_json_object(raw_body)
                code, subcode, message = extract_graph_error(payload, error.code)
                if error.code == 429 or code in RATE_LIMIT_ERROR_CODES:
                    raise RateLimitReached(
                        f"Instagram API rate limit reached: {message}", self.usage
                    ) from error
                if error.code >= 500 and attempt < self.retries:
                    self._backoff(attempt)
                    continue
                raise GraphAPIError(
                    f"Graph API request failed ({error.code}): {message}", code, subcode
                ) from error
            except (URLError, TimeoutError) as error:
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                reason = getattr(error, "reason", str(error))
                raise GraphAPIError(f"Network request failed: {reason}") from error
            except json.JSONDecodeError as error:
                raise GraphAPIError("Graph API returned invalid JSON.") from error

        raise GraphAPIError("Graph API request failed after retries.")

    def _raise_graph_error(self, payload: dict[str, Any], status: int | None) -> None:
        code, subcode, message = extract_graph_error(payload, status)
        if code in RATE_LIMIT_ERROR_CODES:
            raise RateLimitReached(f"Instagram API rate limit reached: {message}", self.usage)
        raise GraphAPIError(f"Graph API error: {message}", code, subcode)

    def _record_usage(
        self, headers: Mapping[str, Any] | None, request_number: int, context: str
    ) -> None:
        changed = self.usage.update(headers)
        if changed:
            self.usage_events.append(
                {
                    "observed_at": utc_now(),
                    "request_number": request_number,
                    "context": context,
                    "header_source": self.usage.header_source,
                    "call_count": format_number(self.usage.call_count),
                    "total_cputime": format_number(self.usage.total_cputime),
                    "total_time": format_number(self.usage.total_time),
                    "estimated_time_to_regain_access": format_number(
                        self.usage.estimated_time_to_regain_access
                    ),
                }
            )
        if self.usage.max_percent >= self.usage_threshold:
            self.stop_before_next = True

    def _pace_requests(self) -> None:
        if self.last_request_at:
            remaining = self.request_delay - (time.monotonic() - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_at = time.monotonic()

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(30.0, (2**attempt) + random.random()))


def parse_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def extract_graph_error(
    payload: dict[str, Any], fallback_code: int | None
) -> tuple[int | None, int | None, str]:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return fallback_code, None, "Unknown API error"
    code = error.get("code", fallback_code)
    subcode = error.get("error_subcode")
    message = str(error.get("message") or "Unknown API error")
    return int(code) if code is not None else None, int(subcode) if subcode else None, message


def format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.2f}"


@dataclass
class CollectionResult:
    target: Target
    account: dict[str, Any]
    posts: list[dict[str, Any]]
    pages: int
    complete: bool
    stopped_by_limit: bool


class InstagramCollector:
    def __init__(self, client: GraphAPIClient, owner_ig_user_id: str):
        self.client = client
        self.owner_ig_user_id = owner_ig_user_id

    def fetch_target(
        self, target: Target, max_posts: int, page_size: int
    ) -> CollectionResult:
        if target.target_type == "authorized":
            return self._fetch_authorized_target(target, max_posts, page_size)

        after: str | None = None
        seen_cursors: set[str] = set()
        posts_by_id: dict[str, dict[str, Any]] = {}
        account: dict[str, Any] = {}
        pages = 0
        complete = False
        stopped_by_limit = False
        account_fields = list(ACCOUNT_FIELDS)
        media_fields = list(MEDIA_FIELDS)

        while max_posts == 0 or len(posts_by_id) < max_posts:
            fields = self._build_fields(target, page_size, after, account_fields, media_fields)
            try:
                payload = self.client.get(
                    f"/{self.owner_ig_user_id}",
                    {"fields": fields},
                    context=f"{target.target_key}:page_{pages + 1}",
                )
            except RateLimitReached:
                if account or posts_by_id:
                    stopped_by_limit = True
                    break
                raise
            except GraphAPIError as error:
                if self._drop_unsupported_field(error, account_fields, media_fields):
                    continue
                raise

            current_account = (
                payload if target.target_type == "authorized" else payload.get("business_discovery")
            )
            if not isinstance(current_account, dict):
                raise GraphAPIError(
                    f"No Business Discovery data returned for @{target.username}. "
                    "The target must be a visible Instagram Professional account."
                )
            if not account:
                account = {key: current_account.get(key, "") for key in ACCOUNT_FIELDS}

            media = current_account.get("media") or {}
            if not isinstance(media, dict):
                media = {}
            for post in media.get("data") or []:
                if isinstance(post, dict) and post.get("id"):
                    posts_by_id[str(post["id"])] = post
                    if max_posts and len(posts_by_id) >= max_posts:
                        break
            pages += 1

            if max_posts and len(posts_by_id) >= max_posts:
                complete = True
                break
            next_after = ((media.get("paging") or {}).get("cursors") or {}).get("after")
            if not next_after:
                complete = True
                break
            next_after = str(next_after)
            if next_after in seen_cursors:
                raise GraphAPIError("Pagination cursor repeated; collection stopped to avoid a loop.")
            seen_cursors.add(next_after)
            after = next_after
            if self.client.stop_before_next:
                stopped_by_limit = True
                break

        return CollectionResult(
            target=target,
            account=account,
            posts=list(posts_by_id.values()),
            pages=pages,
            complete=complete,
            stopped_by_limit=stopped_by_limit,
        )

    def _fetch_authorized_target(
        self, target: Target, max_posts: int, page_size: int
    ) -> CollectionResult:
        account_fields = list(ACCOUNT_FIELDS)
        media_fields = list(MEDIA_FIELDS)
        while True:
            try:
                payload = self.client.get(
                    f"/{self.owner_ig_user_id}",
                    {"fields": ",".join(account_fields)},
                    context=f"{target.target_key}:account",
                )
                account = {key: payload.get(key, "") for key in ACCOUNT_FIELDS}
                break
            except GraphAPIError as error:
                if self._drop_unsupported_field(error, account_fields, []):
                    continue
                raise

        after: str | None = None
        seen_cursors: set[str] = set()
        posts_by_id: dict[str, dict[str, Any]] = {}
        pages = 0
        complete = False
        stopped_by_limit = False

        while max_posts == 0 or len(posts_by_id) < max_posts:
            params: dict[str, Any] = {"fields": ",".join(media_fields), "limit": page_size}
            if after:
                params["after"] = after
            try:
                payload = self.client.get(
                    f"/{self.owner_ig_user_id}/media",
                    params,
                    context=f"{target.target_key}:media_page_{pages + 1}",
                )
            except RateLimitReached:
                if posts_by_id:
                    stopped_by_limit = True
                    break
                raise
            except GraphAPIError as error:
                if self._drop_unsupported_field(error, [], media_fields):
                    continue
                raise

            for post in payload.get("data") or []:
                if isinstance(post, dict) and post.get("id"):
                    posts_by_id[str(post["id"])] = post
                    if max_posts and len(posts_by_id) >= max_posts:
                        break
            pages += 1

            if max_posts and len(posts_by_id) >= max_posts:
                complete = True
                break
            next_after = ((payload.get("paging") or {}).get("cursors") or {}).get("after")
            if not next_after:
                complete = True
                break
            next_after = str(next_after)
            if next_after in seen_cursors:
                raise GraphAPIError("Pagination cursor repeated; collection stopped to avoid a loop.")
            seen_cursors.add(next_after)
            after = next_after
            if self.client.stop_before_next:
                stopped_by_limit = True
                break

        return CollectionResult(
            target=target,
            account=account,
            posts=list(posts_by_id.values()),
            pages=pages,
            complete=complete,
            stopped_by_limit=stopped_by_limit,
        )

    @staticmethod
    def _build_fields(
        target: Target,
        page_size: int,
        after: str | None,
        account_fields: list[str] | None = None,
        media_fields: list[str] | None = None,
    ) -> str:
        account_fields = account_fields if account_fields is not None else ACCOUNT_FIELDS
        media_fields = media_fields if media_fields is not None else MEDIA_FIELDS
        media_edge = f"media.limit({page_size})"
        if after:
            media_edge += f".after({after})"
        media_edge += "{" + ",".join(media_fields) + "}"
        inner_fields = ",".join([*account_fields, media_edge])
        if target.target_type == "authorized":
            return inner_fields
        if not USERNAME_PATTERN.fullmatch(target.username):
            raise ValueError(f"Invalid Instagram username: {target.username}")
        return f"business_discovery.username({target.username}){{{inner_fields}}}"

    @staticmethod
    def _drop_unsupported_field(
        error: GraphAPIError, account_fields: list[str], media_fields: list[str]
    ) -> bool:
        match = UNSUPPORTED_FIELD_PATTERN.search(str(error))
        if not match:
            return False
        field = match.group("field")
        for fields in (account_fields, media_fields):
            if field in fields:
                fields.remove(field)
                return True
        return False


class DataStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.account_snapshots = data_dir / "account_timeseries.csv"
        self.posts = data_dir / "posts.csv"
        self.media_snapshots = data_dir / "media_timeseries.csv"
        self.api_usage = data_dir / "api_usage.csv"
        self.runs = data_dir / "runs.csv"
        self.experiments = data_dir / "experiments.csv"
        self.experiment_jobs = data_dir / "experiment_jobs.csv"
        self.workbook = data_dir / XLSX_FILENAME

    def persist_result(
        self,
        result: CollectionResult,
        run_id: str,
        collected_at: str,
        experiment_id: str = "",
        milestone: str = "",
        scheduled_for: str = "",
    ) -> None:
        target = result.target
        account_username = str(result.account.get("username") or target.username)
        append_csv(
            self.account_snapshots,
            [
                {
                    "collected_at": collected_at,
                    "run_id": run_id,
                    "experiment_id": experiment_id,
                    "milestone": milestone,
                    "scheduled_for": scheduled_for,
                    "target_key": target.target_key,
                    "target_type": target.target_type,
                    "username": account_username,
                    "name": result.account.get("name", ""),
                    "biography": result.account.get("biography", ""),
                    "website": result.account.get("website", ""),
                    "profile_picture_url": result.account.get("profile_picture_url", ""),
                    "followers_count": result.account.get("followers_count", ""),
                    "follows_count": result.account.get("follows_count", ""),
                    "media_count": result.account.get("media_count", ""),
                }
            ],
            ACCOUNT_SNAPSHOT_FIELDS,
        )

        append_csv(
            self.media_snapshots,
            (
                {
                    "collected_at": collected_at,
                    "run_id": run_id,
                    "experiment_id": experiment_id,
                    "milestone": milestone,
                    "scheduled_for": scheduled_for,
                    "target_key": target.target_key,
                    "target_username": account_username,
                    "post_id": post.get("id", ""),
                    "like_count": post.get("like_count", ""),
                    "comments_count": post.get("comments_count", ""),
                }
                for post in result.posts
            ),
            MEDIA_SNAPSHOT_FIELDS,
        )
        self._upsert_posts(result, collected_at, account_username)

    def _upsert_posts(
        self, result: CollectionResult, collected_at: str, account_username: str
    ) -> None:
        existing: dict[tuple[str, str], dict[str, Any]] = {}
        if self.posts.exists():
            with self.posts.open("r", newline="", encoding="utf-8-sig") as file:
                for row in csv.DictReader(file):
                    existing[(row.get("target_key", ""), row.get("post_id", ""))] = row

        for post in result.posts:
            post_id = str(post.get("id", ""))
            key = (result.target.target_key, post_id)
            previous = existing.get(key, {})
            existing[key] = {
                "target_key": result.target.target_key,
                "target_type": result.target.target_type,
                "target_username": account_username,
                "post_id": post_id,
                "post_username": post.get("username", account_username),
                "caption": post.get("caption", ""),
                "media_type": post.get("media_type", ""),
                "media_product_type": post.get("media_product_type", ""),
                "permalink": post.get("permalink", ""),
                "media_url": post.get("media_url", ""),
                "thumbnail_url": post.get("thumbnail_url", ""),
                "published_at": post.get("timestamp", ""),
                "first_seen_at": previous.get("first_seen_at") or collected_at,
                "last_seen_at": collected_at,
            }
        ordered = sorted(
            existing.values(),
            key=lambda row: (row.get("target_key", ""), row.get("published_at", "")),
            reverse=True,
        )
        atomic_write_csv(self.posts, ordered, POST_FIELDS)

    def persist_usage(
        self,
        events: list[dict[str, Any]],
        run_id: str,
        experiment_id: str = "",
        milestone: str = "",
        scheduled_for: str = "",
    ) -> None:
        append_csv(
            self.api_usage,
            (
                {
                    **event,
                    "run_id": run_id,
                    "experiment_id": experiment_id,
                    "milestone": milestone,
                    "scheduled_for": scheduled_for,
                }
                for event in events
            ),
            USAGE_FIELDS,
        )

    def persist_run(self, row: Mapping[str, Any]) -> None:
        append_csv(self.runs, [row], RUN_FIELDS)

    def sync_xlsx(self, destination: Path | None = None) -> None:
        """Mirror every generated CSV into a worksheet in one Excel workbook."""
        workbook = destination or self.workbook
        csv_files = [
            path
            for path in sorted(self.data_dir.glob("*.csv"), key=lambda path: path.name.lower())
            if "_legacy_" not in path.stem
        ]
        if not csv_files:
            return
        sheets: list[tuple[str, list[list[str]]]] = []
        used_names: set[str] = set()
        for csv_path in csv_files:
            with csv_path.open("r", newline="", encoding="utf-8-sig") as file:
                rows = [row for row in csv.reader(file)]
            if rows:
                sheets.append(
                    (
                        _xlsx_sheet_name(csv_path.stem, used_names),
                        _xlsx_project_rows(csv_path.stem, rows),
                    )
                )
        if sheets:
            try:
                write_xlsx_workbook(workbook, sheets)
            except PermissionError as error:
                raise PermissionError(
                    f"Cannot update '{workbook}' because it is open in Excel. "
                    "Close the workbook and run the XLSX sync command again; CSV data is safe."
                ) from error


def _xlsx_sheet_name(stem: str, used_names: set[str]) -> str:
    base = re.sub(r"[\\[\\]:*?/\\\\]", "_", stem)[:31] or "Sheet"
    candidate = base
    suffix = 2
    while candidate.casefold() in used_names:
        ending = f"_{suffix}"
        candidate = f"{base[:31 - len(ending)]}{ending}"
        suffix += 1
    used_names.add(candidate.casefold())
    return candidate


def _xlsx_project_rows(stem: str, rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return rows
    if stem.casefold() == "follower_lookups":
        indexes = {header: index for index, header in enumerate(rows[0])}
        return [
            XLSX_FOLLOWER_LOOKUP_FIELDS,
            *[
                [
                    row[indexes[field]]
                    if field in indexes and indexes[field] < len(row)
                    else ""
                    for field in XLSX_FOLLOWER_LOOKUP_FIELDS
                ]
                for row in rows[1:]
            ],
        ]
    if stem.casefold() == "reels_web":
        indexes = {header: index for index, header in enumerate(rows[0])}
        extra_fields = [
            field
            for field in rows[0]
            if field not in XLSX_REELS_WEB_FIELDS
            and _xlsx_base_field_name(field) not in XLSX_REELS_WEB_HIDDEN_FIELDS
        ]
        fields = [*XLSX_REELS_WEB_FIELDS, *extra_fields]
        projected = [fields]
        for row in rows[1:]:
            values = {
                field: row[indexes[field]] if field in indexes and indexes[field] < len(row) else ""
                for field in fields
            }
            title = values.get("title", "").strip()
            if (
                not values.get("username", "").strip()
                and not values.get("user_id", "").strip()
                and re.fullmatch(r"[A-Za-z0-9._]{1,30}", title)
            ):
                values["username"] = title
                values["title"] = ""
            if not values.get("follower_count_collected_at", "").strip():
                values["follower_count_collected_at"] = "api_error"
            previous_counts = {
                field: values.get(field, "").strip() for field in XLSX_DELTA_FIELDS
            }
            for field in fields:
                base_field = _xlsx_base_field_name(field)
                if (
                    base_field == "days_since_upload"
                    and values.get(field, "").strip()
                ):
                    values[field] = _xlsx_days_since_upload(values[field])
                elif (
                    field != "collected_at"
                    and base_field == "collected_at"
                    and values.get(field, "").strip()
                ):
                    values[field] = _xlsx_recollect_collected_at(
                        values.get("collected_at", ""), values[field]
                    )
                elif field != base_field and base_field in XLSX_DELTA_FIELDS:
                    current = values.get(field, "").strip()
                    if current:
                        values[field] = _xlsx_metric_with_delta(
                            current, previous_counts[base_field]
                        )
                        previous_counts[base_field] = current
            projected.append([values[field] for field in fields])
        return projected
    if stem.casefold() != "users":
        return rows
    indexes = {header: index for index, header in enumerate(rows[0])}
    return [
        XLSX_USERS_FIELDS,
        *[
            [
                row[indexes[field]] if field in indexes and indexes[field] < len(row) else ""
                for field in XLSX_USERS_FIELDS
            ]
            for row in rows[1:]
        ],
    ]


def _xlsx_column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xlsx_text(value: str) -> str:
    escaped = xml_escape(value, {'"': "&quot;"})
    preserve = ' xml:space="preserve"' if value[:1].isspace() or value[-1:].isspace() else ""
    return f"<is><t{preserve}>{escaped}</t></is>"


def _xlsx_base_field_name(field_name: str) -> str:
    match = re.match(
        r"^(?:\d+(?:st|nd|rd|th) collect|\+\d+(?:Minute|Hour|Day|Weeks)(?:_\d+)?)_(.+)$",
        field_name,
    )
    return match.group(1) if match else field_name


def _xlsx_recollect_collected_at(initial_value: str, collected_value: str) -> str:
    try:
        initial = datetime.fromisoformat(initial_value.replace("Z", "+00:00"))
        collected = datetime.fromisoformat(collected_value.replace("Z", "+00:00"))
    except ValueError:
        return collected_value
    if initial.tzinfo is None:
        initial = initial.replace(tzinfo=timezone.utc)
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    elapsed_minutes = max(
        1, int(((collected - initial).total_seconds() / 60) + 0.5)
    )
    if elapsed_minutes < 60:
        elapsed = f"+{elapsed_minutes}Minute"
    else:
        hours = max(1, int((elapsed_minutes / 60) + 0.5))
        if hours < 24:
            elapsed = f"+{hours}Hour"
        else:
            days = max(1, int((hours / 24) + 0.5))
            elapsed = (
                f"+{days}Day"
                if days < 7
                else f"+{max(1, int((days / 7) + 0.5))}Weeks"
            )
    collected_kst = collected.astimezone(timezone(timedelta(hours=9)))
    return f"{collected_kst:%Y-%m-%d %H:%M:%S} ({elapsed})"


def _xlsx_metric_with_delta(current_value: str, previous_value: str) -> str:
    current = current_value.strip()
    previous = previous_value.strip()
    if not re.fullmatch(r"-?\d+", current):
        return current_value
    current_number = int(current)
    current_display = f"{current_number:,}"
    if not re.fullmatch(r"-?\d+", previous):
        return current_display
    delta = current_number - int(previous)
    return f"{current_display}({delta:+,d})"


def _xlsx_days_since_upload(value: str) -> str:
    candidate = value.strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", candidate):
        return value
    elapsed_days = float(candidate)
    if elapsed_days < 1:
        return f"+{int(elapsed_days * 24)}hours"
    return f"+{int(elapsed_days)}day"


def _xlsx_cell(reference: str, value: str, field_name: str, is_header: bool) -> str:
    base_field_name = _xlsx_base_field_name(field_name)
    if not is_header and base_field_name in XLSX_DATE_FIELDS:
        candidate = value.strip()
        if candidate:
            try:
                parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                parsed = parsed.astimezone(timezone(timedelta(hours=9))).replace(tzinfo=None)
                excel_epoch = datetime(1899, 12, 30)
                serial = (parsed - excel_epoch).total_seconds() / 86400
                return f'<c r="{reference}" s="2"><v>{serial:.10f}</v></c>'
            except ValueError:
                pass
    if not is_header and base_field_name in XLSX_DECIMAL_FIELDS:
        candidate = value.strip()
        if re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", candidate):
            return f'<c r="{reference}" s="5"><v>{candidate}</v></c>'
    if not is_header and base_field_name in XLSX_TWO_DECIMAL_FIELDS:
        candidate = value.strip()
        if re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", candidate):
            return f'<c r="{reference}" s="6"><v>{candidate}</v></c>'
    if not is_header and base_field_name in XLSX_PERCENT_FIELDS:
        candidate = value.strip()
        if re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", candidate):
            return f'<c r="{reference}" s="7"><v>{candidate}</v></c>'
    if not is_header and base_field_name in XLSX_NUMERIC_FIELDS:
        candidate = value.strip()
        if re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", candidate):
            return f'<c r="{reference}" s="4"><v>{candidate}</v></c>'
    style = ' s="1"' if is_header else (
        ' s="3"' if base_field_name in XLSX_TEXT_IDENTIFIER_FIELDS else ""
    )
    return f'<c r="{reference}"{style} t="inlineStr">{_xlsx_text(value)}</c>'


def _xlsx_worksheet_xml(rows: list[list[str]]) -> str:
    headers = rows[0]
    row_count = len(rows)
    column_count = max((len(row) for row in rows), default=1)
    last_cell = f"{_xlsx_column_name(column_count)}{row_count}"
    widths: list[int] = []
    for column_index in range(column_count):
        longest = max(
            (len(row[column_index]) if column_index < len(row) else 0 for row in rows[:201]),
            default=8,
        )
        widths.append(min(max(longest + 2, 10), 42))
    columns_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    rows_xml: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column_index in range(column_count):
            value = row[column_index] if column_index < len(row) else ""
            field_name = headers[column_index] if column_index < len(headers) else ""
            reference = f"{_xlsx_column_name(column_index + 1)}{row_number}"
            cells.append(_xlsx_cell(reference, value, field_name, row_number == 1))
        rows_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    filter_xml = f'<autoFilter ref="A1:{_xlsx_column_name(column_count)}1"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_cell}"/>'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        '</sheetView></sheetViews>'
        f'<cols>{columns_xml}</cols><sheetData>{"".join(rows_xml)}</sheetData>{filter_xml}'
        '</worksheet>'
    )


def write_xlsx_workbook(destination: Path, sheets: list[tuple[str, list[list[str]]]]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.stem}.", suffix=".xlsx", dir=destination.parent, delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                + "".join(
                    f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for index in range(1, len(sheets) + 1)
                )
                + "</Types>",
            )
            archive.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                "</Relationships>",
            )
            archive.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets>'
                + "".join(
                    f'<sheet name="{xml_escape(name, {chr(34): "&quot;"})}" sheetId="{index}" r:id="rId{index}"/>'
                    for index, (name, _) in enumerate(sheets, start=1)
                )
                + "</sheets></workbook>",
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(
                    f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
                    for index in range(1, len(sheets) + 1)
                )
                + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                "</Relationships>",
            )
            archive.writestr(
                "xl/styles.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<numFmts count="5"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/><numFmt numFmtId="165" formatCode="@"/><numFmt numFmtId="166" formatCode="0.000000"/><numFmt numFmtId="167" formatCode="0.00"/><numFmt numFmtId="168" formatCode="0.00%"/></numFmts>'
                '<fonts count="2"><font><sz val="11"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/></font></fonts>'
                '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/><bgColor indexed="64"/></patternFill></fill></fills>'
                '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="8"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="167" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="168" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
                '</styleSheet>',
            )
            for index, (_, rows) in enumerate(sheets, start=1):
                archive.writestr(
                    f"xl/worksheets/sheet{index}.xml", _xlsx_worksheet_xml(rows)
                )
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def read_reel_urls_from_xlsx(workbook: Path) -> list[str]:
    """Read unique Reel URLs from the reels_web sheet in row order."""
    main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    document_rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package_rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"

    with zipfile.ZipFile(workbook) as archive:
        workbook_xml = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheet = next(
            (
                item
                for item in workbook_xml.findall(f".//{main_ns}sheet")
                if item.attrib.get("name", "").casefold() == "reels_web"
            ),
            None,
        )
        if sheet is None:
            raise ValueError("The XLSX workbook does not contain a reels_web sheet.")

        relationship_id = sheet.attrib[f"{document_rel_ns}id"]
        relationships_xml = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        target = next(
            item.attrib["Target"]
            for item in relationships_xml.findall(f"{package_rel_ns}Relationship")
            if item.attrib.get("Id") == relationship_id
        )
        sheet_path = target.lstrip("/")
        if not sheet_path.startswith("xl/"):
            sheet_path = f"xl/{sheet_path}"

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_xml = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.findall(f".//{main_ns}t"))
                for item in shared_xml.findall(f"{main_ns}si")
            ]

        sheet_xml = ElementTree.fromstring(archive.read(sheet_path))
        rows: list[list[str]] = []
        for row in sheet_xml.findall(f".//{main_ns}row"):
            values: dict[int, str] = {}
            for cell in row.findall(f"{main_ns}c"):
                reference = cell.attrib.get("r", "A1")
                letters = re.match(r"[A-Z]+", reference)
                if letters is None:
                    continue
                column = 0
                for character in letters.group(0):
                    column = column * 26 + ord(character) - 64
                cell_type = cell.attrib.get("t", "")
                if cell_type == "inlineStr":
                    value = "".join(
                        node.text or "" for node in cell.findall(f".//{main_ns}t")
                    )
                else:
                    value_node = cell.find(f"{main_ns}v")
                    value = value_node.text if value_node is not None and value_node.text else ""
                    if cell_type == "s" and value:
                        value = shared_strings[int(value)]
                values[column - 1] = value
            if values:
                rows.append([values.get(index, "") for index in range(max(values) + 1)])

    if not rows:
        return []
    headers = [value.strip().casefold() for value in rows[0]]
    if "url" not in headers:
        raise ValueError("The reels_web sheet does not contain a url column.")
    url_index = headers.index("url")
    urls: list[str] = []
    seen: set[str] = set()
    for row in rows[1:]:
        value = row[url_index].strip() if url_index < len(row) else ""
        match = re.match(
            r"^https://(?:www\.)?instagram\.com/reels?/([A-Za-z0-9_-]+)",
            value,
            re.IGNORECASE,
        )
        if not match:
            continue
        url = f"https://www.instagram.com/reels/{match.group(1)}/"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def milestone_label(hours: float) -> str:
    if hours == 0:
        return "baseline"
    if hours < 24:
        return f"{hours:g}h"
    if hours % 168 == 0:
        return f"{hours / 168:g}w"
    if hours % 24 == 0:
        return f"{hours / 24:g}d"
    return f"{hours:g}h"


class ExperimentManager:
    """Persist random samples and milestone jobs so schedules survive restarts."""

    TERMINAL_JOB_STATUSES = {"completed", "completed_partial", "failed"}

    def __init__(self, store: DataStore):
        self.store = store

    def load_experiments(self) -> list[dict[str, str]]:
        return self._read(self.store.experiments)

    def load_jobs(self) -> list[dict[str, str]]:
        return self._read(self.store.experiment_jobs)

    @staticmethod
    def _read(path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with path.open("r", newline="", encoding="utf-8-sig") as file:
            return list(csv.DictReader(file))

    def start(
        self,
        candidates: list[Target],
        sample_size: int,
        schedule_name: str,
        offsets: list[float] | None,
        seed: int | None,
        include_self: bool,
    ) -> tuple[dict[str, Any], list[Target], list[dict[str, Any]]]:
        pool = [
            target
            for target in candidates
            if target.enabled and (include_self or target.target_type != "authorized")
        ]
        if sample_size < 1:
            raise ValueError("--sample-size must be at least 1.")
        if sample_size > len(pool):
            raise ValueError(
                f"Requested {sample_size} random targets, but only {len(pool)} enabled candidates are available."
            )
        actual_seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**63)
        selected = random.Random(actual_seed).sample(pool, sample_size)

        if offsets is not None:
            unique_offsets = sorted(set(offsets))
            if any(hours < 0 for hours in unique_offsets):
                raise ValueError("Schedule offsets cannot be negative.")
            if 0 not in unique_offsets:
                unique_offsets.insert(0, 0.0)
            milestones = [(milestone_label(hours), hours) for hours in unique_offsets]
            stored_schedule_name = "custom"
        else:
            milestones = SCHEDULE_PRESETS[schedule_name]
            stored_schedule_name = schedule_name

        created = datetime.now(timezone.utc).replace(microsecond=0)
        experiment_id = f"exp_{created.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
        experiment = {
            "experiment_id": experiment_id,
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "schedule_name": stored_schedule_name,
            "sample_size": sample_size,
            "random_seed": actual_seed,
            "target_keys": "|".join(target.target_key for target in selected),
            "status": "running",
        }
        jobs: list[dict[str, Any]] = []
        for target in selected:
            for label, hours in milestones:
                scheduled = created + timedelta(hours=hours)
                jobs.append(
                    {
                        "job_id": f"{experiment_id}:{target.target_key}:{label}",
                        "experiment_id": experiment_id,
                        "target_key": target.target_key,
                        "milestone": label,
                        "offset_hours": f"{float(hours):g}",
                        "scheduled_for": scheduled.isoformat().replace("+00:00", "Z"),
                        "status": "pending",
                        "attempts": "0",
                        "last_attempt_at": "",
                        "completed_at": "",
                        "delay_minutes": "",
                        "run_id": "",
                        "last_error": "",
                    }
                )

        experiments = self.load_experiments()
        experiments.append(experiment)
        all_jobs = self.load_jobs()
        all_jobs.extend(jobs)
        atomic_write_csv(self.store.experiments, experiments, EXPERIMENT_FIELDS)
        atomic_write_csv(self.store.experiment_jobs, all_jobs, EXPERIMENT_JOB_FIELDS)
        return experiment, selected, jobs

    def due_jobs(
        self, experiment_id: str | None, max_jobs: int, now: datetime | None = None
    ) -> list[dict[str, str]]:
        current = now or datetime.now(timezone.utc)
        jobs = [
            job
            for job in self.load_jobs()
            if job.get("status") == "pending"
            and (not experiment_id or job.get("experiment_id") == experiment_id)
            and parse_utc(job["scheduled_for"]) <= current
        ]
        jobs.sort(key=lambda job: (job["scheduled_for"], job["target_key"]))
        return jobs[:max_jobs] if max_jobs else jobs

    def update_job(self, updated: Mapping[str, Any]) -> None:
        jobs = self.load_jobs()
        found = False
        for index, job in enumerate(jobs):
            if job.get("job_id") == updated.get("job_id"):
                jobs[index] = {key: str(updated.get(key, "")) for key in EXPERIMENT_JOB_FIELDS}
                found = True
                break
        if not found:
            raise ValueError(f"Experiment job not found: {updated.get('job_id', '')}")
        atomic_write_csv(self.store.experiment_jobs, jobs, EXPERIMENT_JOB_FIELDS)
        self._refresh_experiment_status(str(updated.get("experiment_id", "")), jobs)

    def _refresh_experiment_status(
        self, experiment_id: str, jobs: list[dict[str, str]] | None = None
    ) -> None:
        jobs = jobs or self.load_jobs()
        related = [job for job in jobs if job.get("experiment_id") == experiment_id]
        if not related:
            return
        if all(job.get("status") in self.TERMINAL_JOB_STATUSES for job in related):
            new_status = (
                "completed_with_errors"
                if any(job.get("status") == "failed" for job in related)
                else "completed"
            )
        else:
            new_status = "running"
        experiments = self.load_experiments()
        changed = False
        for experiment in experiments:
            if experiment.get("experiment_id") == experiment_id:
                experiment["status"] = new_status
                changed = True
                break
        if changed:
            atomic_write_csv(self.store.experiments, experiments, EXPERIMENT_FIELDS)

    def status_rows(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        experiments = [
            experiment
            for experiment in self.load_experiments()
            if not experiment_id or experiment.get("experiment_id") == experiment_id
        ]
        jobs = self.load_jobs()
        rows: list[dict[str, Any]] = []
        for experiment in experiments:
            related = [
                job for job in jobs if job.get("experiment_id") == experiment["experiment_id"]
            ]
            counts: dict[str, int] = {}
            for job in related:
                counts[job.get("status", "unknown")] = counts.get(job.get("status", "unknown"), 0) + 1
            pending_times = [
                job["scheduled_for"] for job in related if job.get("status") == "pending"
            ]
            rows.append(
                {
                    **experiment,
                    "job_counts": ", ".join(f"{key}={value}" for key, value in sorted(counts.items())),
                    "next_due": min(pending_times) if pending_times else "",
                }
            )
        return rows


class CollectorLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "CollectorLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise RuntimeError(
                f"Another collector may be running. If it is not, remove {self.path}."
            ) from error
        os.write(self.fd, f"pid={os.getpid()} started={utc_now()}\n".encode("utf-8"))
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


@dataclass
class RuntimeConfig:
    access_token: str
    owner_ig_user_id: str
    api_version: str
    data_dir: Path
    usage_threshold: float
    request_delay: float
    timeout: float
    retries: int
    max_posts: int
    page_size: int


@dataclass
class RunSummary:
    run_id: str
    status: str
    targets_requested: int
    targets_completed: int
    posts_seen: int
    requests_made: int
    max_usage_percent: float
    wait_minutes: float
    finished_at: str
    data_saved: bool
    message: str = ""


def run_collection(
    config: RuntimeConfig,
    targets: list[Target],
    experiment_id: str = "",
    milestone: str = "",
    scheduled_for: str = "",
) -> RunSummary:
    run_id = uuid.uuid4().hex
    started_at = utc_now()
    store = DataStore(config.data_dir)
    client = GraphAPIClient(
        config.access_token,
        config.api_version,
        config.usage_threshold,
        config.request_delay,
        config.timeout,
        config.retries,
    )
    collector = InstagramCollector(client, config.owner_ig_user_id)
    status = "success"
    message = ""
    targets_completed = 0
    posts_seen = 0
    data_saved = False

    try:
        with CollectorLock(config.data_dir / ".collector.lock"):
            for target in targets:
                if client.stop_before_next:
                    status = "usage_threshold"
                    message = f"Stopped before the next target at {client.usage.max_percent:g}% usage."
                    break
                try:
                    result = collector.fetch_target(target, config.max_posts, config.page_size)
                except RateLimitReached as error:
                    status = "rate_limited"
                    message = str(error)
                    break
                collected_at = utc_now()
                store.persist_result(
                    result,
                    run_id,
                    collected_at,
                    experiment_id,
                    milestone,
                    scheduled_for,
                )
                data_saved = True
                posts_seen += len(result.posts)
                if result.complete:
                    targets_completed += 1
                if result.stopped_by_limit or client.stop_before_next:
                    status = "usage_threshold"
                    message = (
                        f"Saved the last response and stopped at "
                        f"{client.usage.max_percent:g}% API usage."
                    )
                    break
    except (GraphAPIError, RuntimeError, OSError, ValueError) as error:
        status = "error"
        message = str(error)
    finally:
        store.persist_usage(
            client.usage_events,
            run_id,
            experiment_id,
            milestone,
            scheduled_for,
        )
        finished_at = utc_now()
        store.persist_run(
            {
                "run_id": run_id,
                "experiment_id": experiment_id,
                "milestone": milestone,
                "scheduled_for": scheduled_for,
                "started_at": started_at,
                "finished_at": finished_at,
                "status": status,
                "targets_requested": len(targets),
                "targets_completed": targets_completed,
                "posts_seen": posts_seen,
                "requests_made": client.request_count,
                "max_usage_percent": format_number(client.usage.max_percent),
                "message": message,
            }
        )
        try:
            store.sync_xlsx()
        except OSError as error:
            print(f"Warning: CSV saved but XLSX export failed: {error}", file=sys.stderr)

    return RunSummary(
        run_id=run_id,
        status=status,
        targets_requested=len(targets),
        targets_completed=targets_completed,
        posts_seen=posts_seen,
        requests_made=client.request_count,
        max_usage_percent=client.usage.max_percent,
        wait_minutes=client.usage.estimated_time_to_regain_access,
        finished_at=finished_at,
        data_saved=data_saved,
        message=message,
    )


def execute_due_jobs(
    config: RuntimeConfig,
    registry: TargetRegistry,
    manager: ExperimentManager,
    experiment_id: str | None,
    max_jobs: int,
    max_attempts: int,
) -> tuple[int, int]:
    completed = 0
    attempted = 0
    with CollectorLock(config.data_dir / ".experiment_scheduler.lock"):
        due = manager.due_jobs(experiment_id, max_jobs)
        if not due:
            return 0, 0
        target_map = {target.target_key: target for target in registry.load()}
        for job in due:
            attempted += 1
            target = target_map.get(job["target_key"])
            job["attempts"] = str(int(job.get("attempts") or 0) + 1)
            job["last_attempt_at"] = utc_now()
            if target is None:
                job["last_error"] = "Target is no longer present in targets.csv."
                job["status"] = "failed" if int(job["attempts"]) >= max_attempts else "pending"
                manager.update_job(job)
                continue

            print(
                f"Collecting {job['target_key']} milestone={job['milestone']} "
                f"scheduled={job['scheduled_for']}"
            )
            summary = run_collection(
                config,
                [target],
                experiment_id=job["experiment_id"],
                milestone=job["milestone"],
                scheduled_for=job["scheduled_for"],
            )
            job["run_id"] = summary.run_id
            job["last_error"] = summary.message
            if summary.data_saved:
                job["status"] = (
                    "completed" if summary.status == "success" else "completed_partial"
                )
                job["completed_at"] = summary.finished_at
                delay = max(
                    0.0,
                    (parse_utc(summary.finished_at) - parse_utc(job["scheduled_for"])).total_seconds()
                    / 60,
                )
                job["delay_minutes"] = f"{delay:.2f}"
                completed += 1
            else:
                job["status"] = (
                    "failed" if int(job["attempts"]) >= max_attempts else "pending"
                )
            manager.update_job(job)
            print_summary(summary, config.data_dir)
            if not summary.data_saved and summary.status in {"rate_limited", "error"}:
                break
    return attempted, completed


def read_username_pool(path: Path) -> list[str]:
    if not path.exists():
        raise ValueError(f"Candidate file was not found: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        rows = list(csv.reader(file))
    if not rows:
        return []
    header = [cell.strip().lower() for cell in rows[0]]
    if "username" in header:
        index = header.index("username")
        return [row[index] for row in rows[1:] if len(row) > index and row[index].strip()]
    return [row[0] for row in rows if row and row[0].strip()]


def parse_offsets(value: str | None) -> list[float] | None:
    if value is None:
        return None
    try:
        offsets = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise ValueError("--offsets-hours must be comma-separated numbers.") from error
    if not offsets:
        raise ValueError("--offsets-hours cannot be empty.")
    return offsets


def add_collection_arguments(
    parser: argparse.ArgumentParser, include_target: bool = True
) -> None:
    if include_target:
        parser.add_argument(
            "--target",
            action="append",
            help="Collect only this target key/username. Repeat for multiple targets.",
        )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=0,
        help="Maximum posts per target; 0 collects every page available (default: 0).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help="Posts requested per API page, 1-100 (default: 100).",
    )
    parser.add_argument(
        "--usage-threshold",
        type=float,
        default=DEFAULT_USAGE_THRESHOLD,
        help="Stop before another request at this API usage percentage (default: 90).",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.5,
        help="Minimum seconds between API requests (default: 0.5).",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect Instagram Professional-account time-series data into CSV/XLSX."
    )
    parser.add_argument(
        "--targets-file",
        type=Path,
        default=DEFAULT_TARGETS_FILE,
        help=f"Target registry CSV (default: {DEFAULT_TARGETS_FILE}).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.getenv("INSTAGRAM_DATA_DIR", DEFAULT_DATA_DIR)),
        help=f"CSV output directory (default: {DEFAULT_DATA_DIR}).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    target_parser = subparsers.add_parser("target", help="Manage collection targets.")
    target_actions = target_parser.add_subparsers(dest="target_action", required=True)
    target_add = target_actions.add_parser("add", help="Add a target.")
    target_add.add_argument("username", nargs="?", help="Public Professional username.")
    target_add.add_argument("--self", action="store_true", help="Add the authorized account.")
    target_add.add_argument("--notes", default="")
    target_import = target_actions.add_parser("import", help="Import candidate usernames from CSV.")
    target_import.add_argument("csv_file", type=Path)
    target_import.add_argument("--notes", default="imported pool")
    target_actions.add_parser("list", help="List targets.")
    for action in ("enable", "disable", "remove"):
        action_parser = target_actions.add_parser(action)
        action_parser.add_argument("target_key")

    collect_parser = subparsers.add_parser("collect", help="Run one collection now.")
    add_collection_arguments(collect_parser)

    xlsx_parser = subparsers.add_parser(
        "xlsx", help="Synchronize existing data CSV files into XLSX."
    )
    xlsx_parser.add_argument(
        "--output",
        type=Path,
        help="Optional alternate XLSX path, useful when the main workbook is open in Excel.",
    )
    xlsx_urls_parser = subparsers.add_parser(
        "xlsx-reel-urls", help="Export unique Reel URLs from the reels_web XLSX sheet."
    )
    xlsx_urls_parser.add_argument("--output", type=Path, required=True)

    watch_parser = subparsers.add_parser("watch", help="Collect repeatedly at a fixed interval.")
    add_collection_arguments(watch_parser)
    watch_parser.add_argument(
        "--interval-minutes",
        type=float,
        required=True,
        help="Minutes between collection starts.",
    )

    experiment_parser = subparsers.add_parser(
        "experiment", help="Run a persistent random-sample milestone experiment."
    )
    experiment_actions = experiment_parser.add_subparsers(
        dest="experiment_action", required=True
    )
    experiment_start = experiment_actions.add_parser(
        "start", help="Randomly sample targets and create milestone jobs."
    )
    experiment_start.add_argument("--sample-size", type=int, required=True)
    experiment_start.add_argument(
        "--schedule", choices=sorted(SCHEDULE_PRESETS), default="test"
    )
    experiment_start.add_argument(
        "--offsets-hours",
        help="Custom comma-separated offsets; baseline 0 is added automatically.",
    )
    experiment_start.add_argument("--seed", type=int)
    experiment_start.add_argument("--include-self", action="store_true")

    experiment_due = experiment_actions.add_parser(
        "run-due", help="Collect every milestone currently due, then exit."
    )
    add_collection_arguments(experiment_due, include_target=False)
    experiment_due.add_argument("--experiment-id")
    experiment_due.add_argument("--max-jobs", type=int, default=0)
    experiment_due.add_argument("--max-attempts", type=int, default=5)

    experiment_watch = experiment_actions.add_parser(
        "watch", help="Keep running and collect milestones as they become due."
    )
    add_collection_arguments(experiment_watch, include_target=False)
    experiment_watch.add_argument("--experiment-id")
    experiment_watch.add_argument("--max-jobs", type=int, default=0)
    experiment_watch.add_argument("--max-attempts", type=int, default=5)
    experiment_watch.add_argument("--poll-minutes", type=float, default=1.0)

    experiment_status = experiment_actions.add_parser("status", help="Show experiment progress.")
    experiment_status.add_argument("--experiment-id")
    return parser


def runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    load_dotenv(BASE_DIR / ".env")
    access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
    owner_id = os.getenv("INSTAGRAM_IG_USER_ID", "").strip()
    if not access_token or not owner_id:
        raise ValueError(
            "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_IG_USER_ID in .env before collecting."
        )
    if not (1 <= args.page_size <= 100):
        raise ValueError("--page-size must be between 1 and 100.")
    if args.max_posts < 0:
        raise ValueError("--max-posts must be 0 or greater.")
    if not (1 <= args.usage_threshold <= 100):
        raise ValueError("--usage-threshold must be between 1 and 100.")
    if args.request_delay < 0 or args.timeout <= 0 or args.retries < 0:
        raise ValueError("Delay/retry values must not be negative, and timeout must be positive.")
    return RuntimeConfig(
        access_token=access_token,
        owner_ig_user_id=owner_id,
        api_version=os.getenv("INSTAGRAM_API_VERSION", DEFAULT_API_VERSION).strip(),
        data_dir=args.data_dir.resolve(),
        usage_threshold=args.usage_threshold,
        request_delay=args.request_delay,
        timeout=args.timeout,
        retries=args.retries,
        max_posts=args.max_posts,
        page_size=args.page_size,
    )


def print_targets(targets: list[Target]) -> None:
    if not targets:
        print("No targets registered.")
        return
    print(f"{'KEY':<32} {'TYPE':<20} {'ENABLED':<8} USERNAME")
    for target in targets:
        print(
            f"{target.target_key:<32} {target.target_type:<20} "
            f"{str(target.enabled):<8} {target.username or '(authorized account)'}"
        )


def print_summary(summary: RunSummary, data_dir: Path) -> None:
    print(
        f"status={summary.status} targets={summary.targets_completed}/"
        f"{summary.targets_requested} posts={summary.posts_seen} "
        f"requests={summary.requests_made} api_usage={summary.max_usage_percent:g}%"
    )
    print(f"CSV directory: {data_dir}")
    if summary.message:
        print(summary.message)


def print_experiment_status(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No matching experiments.")
        return
    for row in rows:
        print(
            f"{row['experiment_id']} status={row['status']} schedule={row['schedule_name']} "
            f"sample={row['sample_size']} jobs=[{row['job_counts']}] "
            f"next_due={row['next_due'] or '-'}"
        )


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    parser = build_parser()
    args = parser.parse_args()
    registry = TargetRegistry(args.targets_file.resolve())

    try:
        if args.command == "target":
            if args.target_action == "add":
                if args.self == bool(args.username):
                    raise ValueError("Provide either a username or --self, but not both.")
                target = registry.add(args.username, args.self, args.notes)
                print(f"Added target: {target.target_key}")
            elif args.target_action == "import":
                usernames = read_username_pool(args.csv_file.resolve())
                added, skipped, invalid = registry.import_usernames(usernames, args.notes)
                print(f"Imported targets: added={added} skipped={skipped} invalid={invalid}")
            elif args.target_action == "list":
                print_targets(registry.load())
            elif args.target_action == "enable":
                print(f"Enabled target: {registry.set_enabled(args.target_key, True).target_key}")
            elif args.target_action == "disable":
                print(f"Disabled target: {registry.set_enabled(args.target_key, False).target_key}")
            elif args.target_action == "remove":
                print(f"Removed target: {registry.remove(args.target_key).target_key}")
            return 0

        store = DataStore(args.data_dir.resolve())
        if args.command == "xlsx":
            workbook = args.output.resolve() if args.output else store.workbook
            store.sync_xlsx(workbook)
            print(f"XLSX saved: {workbook}")
            return 0
        if args.command == "xlsx-reel-urls":
            urls = read_reel_urls_from_xlsx(store.workbook)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                "".join(f"{url}\n" for url in urls), encoding="utf-8"
            )
            print(f"Reel URLs exported: {len(urls)}")
            return 0
        manager = ExperimentManager(store)
        if args.command == "experiment" and args.experiment_action == "start":
            experiment, selected, jobs = manager.start(
                registry.load(),
                args.sample_size,
                args.schedule,
                parse_offsets(args.offsets_hours),
                args.seed,
                args.include_self,
            )
            print(f"Created experiment: {experiment['experiment_id']}")
            print(f"Random seed: {experiment['random_seed']}")
            print("Selected targets: " + ", ".join(target.target_key for target in selected))
            print(f"Scheduled jobs: {len(jobs)}")
            store.sync_xlsx()
            print("Start background collection with: .\\start_background.ps1")
            return 0
        if args.command == "experiment" and args.experiment_action == "status":
            print_experiment_status(manager.status_rows(args.experiment_id))
            return 0

        config = runtime_config(args)

        if args.command == "experiment":
            if args.max_jobs < 0 or args.max_attempts < 1:
                raise ValueError("--max-jobs must be 0 or greater and --max-attempts at least 1.")
            if args.experiment_action == "run-due":
                attempted, completed = execute_due_jobs(
                    config,
                    registry,
                    manager,
                    args.experiment_id,
                    args.max_jobs,
                    args.max_attempts,
                )
                print(f"Due jobs attempted={attempted} completed={completed}")
                return 0
            if args.poll_minutes <= 0:
                raise ValueError("--poll-minutes must be positive.")
            print("Experiment scheduler is running. Press Ctrl+C to stop.", flush=True)
            while True:
                attempted, completed = execute_due_jobs(
                    config,
                    registry,
                    manager,
                    args.experiment_id,
                    args.max_jobs,
                    args.max_attempts,
                )
                if attempted:
                    print(f"Due jobs attempted={attempted} completed={completed}", flush=True)
                if args.experiment_id:
                    status_rows = manager.status_rows(args.experiment_id)
                    if status_rows and status_rows[0]["status"] in {
                        "completed",
                        "completed_with_errors",
                    }:
                        print_experiment_status(status_rows)
                        return 0
                time.sleep(args.poll_minutes * 60)

        targets = registry.select(args.target)
        if not targets:
            raise ValueError("No enabled targets. Add one with 'target add USERNAME' or 'target add --self'.")

        if args.command == "collect":
            summary = run_collection(config, targets)
            print_summary(summary, config.data_dir)
            return 0 if summary.status in {"success", "usage_threshold"} else 1

        if args.interval_minutes <= 0:
            raise ValueError("--interval-minutes must be positive.")
        print(f"Watching {len(targets)} target(s). Press Ctrl+C to stop.")
        while True:
            cycle_started = time.monotonic()
            summary = run_collection(config, targets)
            print_summary(summary, config.data_dir)
            normal_wait = args.interval_minutes * 60
            rate_wait = (summary.wait_minutes + 1) * 60 if summary.wait_minutes else 0
            elapsed = time.monotonic() - cycle_started
            wait_seconds = max(normal_wait, rate_wait) - elapsed
            if wait_seconds > 0:
                print(f"Next collection in {wait_seconds / 60:.1f} minutes.")
                time.sleep(wait_seconds)
    except KeyboardInterrupt:
        print("Collection stopped by user.")
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
