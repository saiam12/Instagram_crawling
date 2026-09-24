"""Filesystem layout helpers for the detached Android metric queue."""

from __future__ import annotations

from pathlib import Path


REEL_HISTORY_DIRECTORY = ".collector"
ANDROID_METRIC_QUEUE_DIRECTORY = "android_metric_queue"
COLLECTION_STOP_FILENAME = "collection_stop.json"


def android_metric_queue_root(data_dir: Path | str) -> Path:
    return Path(data_dir).resolve() / REEL_HISTORY_DIRECTORY / ANDROID_METRIC_QUEUE_DIRECTORY


def collection_stop_path(data_dir: Path | str) -> Path:
    """Path for the durable stop signal shared by browser and Android workers."""
    return Path(data_dir).resolve() / REEL_HISTORY_DIRECTORY / COLLECTION_STOP_FILENAME


def clear_collection_stop_request(data_dir: Path | str) -> None:
    """A new user-started collector run is allowed to begin fresh."""
    collection_stop_path(data_dir).unlink(missing_ok=True)


def _android_metric_queue_paths(data_dir: Path | str) -> dict[str, Path]:
    root = android_metric_queue_root(data_dir)
    return {
        "root": root,
        "pending": root / "pending",
        "working": root / "working",
        "completed": root / "completed",
        "hashtag_completed": root / "hashtag_completed",
        "worker_starting": root / "worker.starting.json",
        "worker_lock": root / "worker.lock.json",
        "history_lock": root / "history.lock.json",
        "status": root / "status.json",
    }


def android_metric_queue_counts(data_dir: Path | str) -> dict[str, int]:
    paths = _android_metric_queue_paths(data_dir)
    return {
        name: len(list(paths[name].glob("*.json"))) if paths[name].exists() else 0
        for name in ("pending", "working", "completed")
    }
