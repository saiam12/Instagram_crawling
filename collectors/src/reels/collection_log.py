"""Collection-source logs and terminal projections."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from .android_metric_queue import REEL_HISTORY_DIRECTORY
from .collection_diagnostics import sanitize_log_value
from .collection_settings import ANDROID_COLLECTION_LOG_FILENAME, PYTHON_COLLECTION_LOG_FILENAME
from .reel_records import isoformat_kst


def collection_log_path(data_dir: Path | str, source: str) -> Path:
    """Return the live log dedicated to one collection source."""
    filenames = {
        "python": PYTHON_COLLECTION_LOG_FILENAME,
        "android": ANDROID_COLLECTION_LOG_FILENAME,
    }
    try:
        filename = filenames[source]
    except KeyError as error:
        raise ValueError(f"Unknown collection log source: {source}") from error
    return Path(data_dir).resolve() / REEL_HISTORY_DIRECTORY / filename


def _collection_log_value(key: str, value: Any) -> str:
    value = sanitize_log_value(key, value)
    if value in (None, ""):
        return ""
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        text = str(value)
    return " ".join(text.split())


def append_collection_log(
    data_dir: Path | str, source: str, event: str, *, show_in_terminal: bool = False, **values: Any
) -> Path:
    """Append one human-readable, source-specific collection event.

    The browser process and the detached Android worker intentionally use
    different files so their live output never interleaves.  Logging must not
    interrupt a collection if a user has the file open in another program.
    """
    path = collection_log_path(data_dir, source)
    detail_parts: list[str] = []
    for key, value in values.items():
        # Queue identifiers are useful in durable JSON state, but add noise to
        # operator-facing logs.  Keep Reel URLs readable without a redundant
        # ``url=`` label.
        if key.casefold().endswith("job_id"):
            continue
        rendered = _collection_log_value(key, value)
        if not rendered:
            continue
        detail_parts.append(rendered if key.casefold() == "url" else f"{key}={rendered}")
    details = " ".join(detail_parts)
    prefix = f"{isoformat_kst()} [{source.upper()}]"
    line = f"{prefix} {event}"
    if details:
        line = f"{line} | {details}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(f"{line}\n")
            file.flush()
    except OSError:
        # Collection data is written through its own atomic history path;
        # a transient log-file lock must not make the data capture fail.
        pass
    if show_in_terminal:
        terminal_line = collection_log_terminal_line(source, line)
        if terminal_line is not None:
            print(terminal_line, flush=True)
    return path


def print_new_collection_log_lines(path: Path, source: str, position: int) -> int:
    try:
        if not path.exists():
            return position
        size = path.stat().st_size
        if size < position:
            position = 0
        if size == position:
            return position
        with path.open("r", encoding="utf-8", errors="replace") as file:
            file.seek(position)
            lines = file.readlines()
            position = file.tell()
        for line in lines:
            terminal_line = collection_log_terminal_line(source, line)
            if terminal_line is not None:
                print(terminal_line, flush=True)
    except OSError:
        pass
    return position


def collection_log_terminal_line(source: str, line: str) -> str | None:
    """Show Android Reel summaries while retaining state events only in the log."""
    rendered = line.rstrip()
    if source != "android":
        return rendered
    if " [ANDROID] reel_metric_started" in rendered:
        return None
    marker = " terminal_line="
    if marker not in rendered:
        return None
    return rendered.split(marker, 1)[1]

