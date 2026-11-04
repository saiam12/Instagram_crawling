"""Shared collection defaults; mutable run state stays with its owner."""
from __future__ import annotations
from pathlib import Path
from .android_reel_metrics import ANDROID_COLLECTION_FIELDS
from .reel_records import CSV_FIELDS, REEL_CHANGE_FIELDS

PYTHON_VERSION_ROOT = Path(__file__).resolve().parents[2]
ROW_COLLECTION_FIELDS = [
    "collection_number",
    "days_since_previous",
    *CSV_FIELDS,
    "reaction_rate",
    *REEL_CHANGE_FIELDS,
    "android_mismatch",
]
REEL_HISTORY_FILENAME = "reels_history_active.csv"
PYTHON_COLLECTION_LOG_FILENAME = "python.log"
ANDROID_COLLECTION_LOG_FILENAME = "android.log"
LEGACY_REEL_HISTORY_FILENAMES = ("reels_history.csv",)
PUBLIC_REELS_STEM = "reels"
COUNT_HISTORY_XLSX_FILENAME = "count_history.xlsx"
COUNT_HISTORY_METRIC_FIELDS = (
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "repost_count",
    "saved_count",
    "follower_count",
)
LEGACY_REEL_DROPPED_FIELDS = {
    "collection_label",
    "follower_count_collected_at",
    "follower_lookup_status",
}
ANDROID_METRIC_QUEUE_IDLE_SECONDS = 12.0
ANDROID_METRIC_QUEUE_EXPORT_BATCH_SIZE = 20
ANDROID_METRIC_MAX_ATTEMPTS_PER_REEL = 3
ANDROID_UI_RETRY_DELAY_SECONDS = 30.0
ANDROID_UI_MAX_RETRY_DELAY_SECONDS = 30.0 * 60
ANDROID_UI_RECOVERY_COOLDOWN_SECONDS = 120.0
ANDROID_PROFILE_VIEW_MAX_ATTEMPTS = 3
ANDROID_PROFILE_VIEW_RETRY_DELAY_SECONDS = 10.0
ANDROID_DEVICE_RECONNECT_SECONDS = 30.0
ANDROID_METRIC_FAILURE_BACKOFF_SECONDS = 30.0
ANDROID_RECOVERY_RETRY_SECONDS = 60.0
COLLECTION_MAX_CONSECUTIVE_FAILURES = 5
COLLECTION_FAILURE_BACKOFF_SECONDS = 5.0
ANDROID_METRIC_FIELDS = (*ANDROID_COLLECTION_FIELDS, "comment_count")
PYTHON_ANDROID_COMPARISON_COUNT_FIELDS = (
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "repost_count",
    "saved_count",
)
ANDROID_TERMINAL_METRIC_FIELDS = (
    "like_count",
    "comment_count",
    "repost_count",
    "share_count",
    "saved_count",
    "view_count",
)
REEL_STORE_FLUSH_RECORD_COUNT = 100
RECOLLECT_COOLDOWN_SCHEDULE = [
    {"label": "6시간", "seconds": 6 * 60 * 60},
    {"label": "1일", "seconds": 24 * 60 * 60},
    {"label": "3일", "seconds": 3 * 24 * 60 * 60},
    {"label": "1주", "seconds": 7 * 24 * 60 * 60},
    {"label": "2주", "seconds": 14 * 24 * 60 * 60},
    {"label": "1달", "calendar_months": 1},
]
