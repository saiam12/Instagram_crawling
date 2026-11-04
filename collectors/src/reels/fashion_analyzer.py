"""Run the Reel analyzer for unusually high-performing Fashion Reels."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .korean_classification import record_is_korean, apply_korean_overrides
from .reel_records import normalize_reel_url


# ``reaction_rate`` is stored as a ratio (90.0 means 9000%).
REACTION_RATE_THRESHOLD = 90.0
DEFAULT_ANALYZER_CONCURRENCY = 4
ANALYZER_MAX_RETRIES = 3
ANALYZER_RETRY_DELAY_SECONDS = 2.0


def _history_path(data_root: Path) -> Path:
    return data_root / ".collector" / "reels_history_active.csv"


def _state_path(data_root: Path) -> Path:
    return data_root / ".collector" / "fashion_analyzer_state.json"


def _analysis_output_path(data_root: Path) -> Path:
    if data_root.parent.name == ".datasets":
        return data_root.parent.parent / "reel_analysis.json"
    return data_root / "reel_analysis.json"


async def _run_reel_insights(data_root: Path, repo_root: Path) -> None:
    analysis_path = _analysis_output_path(data_root)
    script = repo_root / "analyzers" / "reel_insights.py"
    if not analysis_path.is_file() or not script.is_file():
        return
    public_root = analysis_path.parent
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            _analyzer_python(repo_root),
            str(script),
            "--input", str(analysis_path),
            "--output", str(public_root / "reel_insights.json"),
            "--report", str(public_root / "reel_insights.html"),
            "--state", str(data_root / ".collector" / "reel_insights_state.json"),
            cwd=str(script.parent),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.terminate()
            await process.communicate()
        raise
    except OSError as error:
        print(f"[ANALYZER] Reel insights could not start: {error}", file=sys.stderr)
        return
    if process.returncode:
        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        print(f"[ANALYZER] Reel insights failed: {detail[-2_000:]}", file=sys.stderr)


def _read_rows(data_root: Path) -> list[dict[str, str]]:
    path = _history_path(data_root)
    if not path.is_file():
        return []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            return apply_korean_overrides(csv.DictReader(handle), data_root)
    except (OSError, UnicodeError, csv.Error):
        return []


def parse_reaction_rate(value: Any) -> float | None:
    """Parse the stored ratio, accepting a displayed percent as a convenience."""
    text = str(value or "").strip().replace(",", "")
    displayed_percent = text.endswith("%")
    if displayed_percent:
        text = text[:-1].strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number / 100 if displayed_percent else number


def qualifying_reels(
    rows: list[dict[str, Any]],
    threshold: float = REACTION_RATE_THRESHOLD,
) -> list[dict[str, Any]]:
    """Return one latest qualifying snapshot per Reel URL."""
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        url = str(row.get("url") or "").strip()
        rate = parse_reaction_rate(row.get("reaction_rate"))
        if not url or rate is None or rate < threshold:
            continue
        previous = selected.get(url)
        if previous is None or str(row.get("collected_at") or "") >= str(previous.get("collected_at") or ""):
            selected[url] = {**row, "reaction_rate": rate}
    return sorted(selected.values(), key=lambda row: str(row.get("url") or ""))


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return default


def _write_json_atomic(path: Path, value: Any) -> None:
    lock = None
    if path.name == "reel_analysis.json":
        from .process_lock import AtomicProcessLock
        lock = AtomicProcessLock(path.with_suffix(".classification.lock"), wait_seconds=30)
        if not lock.acquire():
            raise RuntimeError("Analysis storage is busy")
    try:
        if path.name == "reel_analysis.json" and value is None:
            value = _load_json(path, [])
        if path.name == "reel_analysis.json" and isinstance(value, list):
            data_root = path.parent / ".datasets" / "fashion"
            if not data_root.is_dir():
                data_root = path.parent
            sources = {}
            for source in _read_rows(data_root):
                normalized = normalize_reel_url(source.get("url"))
                if normalized:
                    sources[normalized["shortcode"]] = source
            for item in value:
                if not isinstance(item, dict):
                    continue
                source = sources.get(str(item.get("reel_id") or ""))
                if source is not None:
                    for field in ("korean", "korean_source", "account_country"):
                        item[field] = source.get(field)
                updated = apply_korean_overrides([item], data_root)[0]
                item.update(updated)
                item["korean"] = record_is_korean(item)
                if isinstance(item.get("analysis"), dict):
                    item["analysis"]["korean"] = item["korean"]
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        if lock is not None:
            lock.release()


def _analyzer_python(repo_root: Path) -> str:
    configured = os.getenv("FASHION_ANALYZER_PYTHON", "").strip()
    if configured:
        return configured
    candidate = repo_root / "analyzers" / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


async def _run_analyzer(
    *,
    url: str,
    output_file: Path,
    analyzer_script: Path,
    analyzer_python: str,
    korean: bool | None = None,
) -> tuple[str, Path, int, str]:
    environment = os.environ.copy()
    environment["GEMINI_OUTPUT_FILE"] = str(output_file)
    environment["PYTHONUTF8"] = "1"
    environment["COLLECTOR_REEL_KOREAN"] = "null" if korean is None else str(korean).lower()
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            analyzer_python,
            str(analyzer_script),
            "--url",
            url,
            cwd=str(analyzer_script.parent),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.terminate()
            await process.communicate()
        raise
    except OSError as error:
        return url, output_file, 1, str(error)
    detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
    return url, output_file, int(process.returncode or 0), detail[-2_000:]


async def analyze_qualifying_fashion_reels(
    data_root: Path | str,
    *,
    concurrency: int = DEFAULT_ANALYZER_CONCURRENCY,
    repo_root: Path | None = None,
) -> dict[str, int]:
    """Analyze new qualifying Fashion URLs concurrently and persist a manifest."""
    root = Path(data_root).resolve()
    repository = (repo_root or Path(__file__).resolve().parents[3]).resolve()
    script = repository / "analyzers" / "reel_analyzer.py"
    candidates = qualifying_reels(_read_rows(root))
    state_path = _state_path(root)
    state = _load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    records = state.get("records")
    if not isinstance(records, dict):
        records = {}
    records = {url: value for url, value in records.items() if isinstance(value, dict)}
    output_path = _analysis_output_path(root)
    existing = _load_json(output_path, [])
    analyses = existing if isinstance(existing, list) else []
    saved_urls = {item.get("reel_url") for item in analyses if isinstance(item, dict) and isinstance(item.get("reel_url"), str)}
    pending = [row for row in candidates if (
        records.get(str(row["url"]), {}).get("status") != "succeeded" or str(row["url"]) not in saved_urls
    )]
    if not pending:
        await _run_reel_insights(root, repository)
        return {"qualifying": len(candidates), "queued": 0, "succeeded": 0, "failed": 0}
    if not script.is_file():
        return {"qualifying": len(candidates), "queued": len(pending), "succeeded": 0, "failed": len(pending)}

    limit = max(1, min(8, int(concurrency or 1)))
    analyzer_python = _analyzer_python(repository)
    semaphore = asyncio.Semaphore(limit)

    async def run_attempt(row: dict[str, Any], directory: Path) -> tuple[dict[str, Any], bool, str]:
        async with semaphore:
            digest = hashlib.sha1(str(row["url"]).encode("utf-8")).hexdigest()
            result_file = directory / f"{digest}.json"
            result_file.unlink(missing_ok=True)
            url, path, code, detail = await _run_analyzer(
                url=str(row["url"]),
                output_file=result_file,
                analyzer_script=script,
                analyzer_python=analyzer_python,
                korean=record_is_korean(row),
            )
            if code != 0:
                return row, False, detail or f"analyzer exited with code {code}"
            payload = _load_json(path, None)
            if not isinstance(payload, list) or not payload:
                return row, False, "analyzer did not produce a JSON result"
            if not isinstance(payload[-1], dict) or not payload[-1]:
                return row, False, "analyzer did not produce a JSON record"
            record = payload[-1]
            record = {
                **record,
                "reel_url": url,
                "korean": record_is_korean(row),
                "reaction_rate": row.get("reaction_rate"),
                "fashion_analyzed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            if isinstance(record.get("analysis"), dict):
                record["analysis"]["korean"] = record["korean"]
            return row, True, json.dumps(record, ensure_ascii=False)

    async def run_one(row: dict[str, Any], directory: Path, index: int) -> tuple[dict[str, Any], bool, str, int]:
        for attempt in range(1, ANALYZER_MAX_RETRIES + 2):
            result_row, ok, detail = await run_attempt(row, directory)
            if ok or attempt == ANALYZER_MAX_RETRIES + 1:
                return result_row, ok, detail, attempt
            delay = ANALYZER_RETRY_DELAY_SECONDS * attempt
            print(f"[ANALYZER {index}/{len(pending)}] {row['url']} (Try : {attempt}/{ANALYZER_MAX_RETRIES})")
            await asyncio.sleep(delay)

    succeeded = failed = 0
    with tempfile.TemporaryDirectory(prefix="fashion-analyzer-") as temporary:
        tasks = [asyncio.create_task(run_one(row, Path(temporary), index)) for index, row in enumerate(pending, start=1)]
        try:
            for result in asyncio.as_completed(tasks):
                row, ok, detail, attempt_count = await result
                url = str(row["url"])
                if ok:
                    succeeded += 1
                    record = json.loads(detail)
                    analyses = [item for item in analyses if not isinstance(item, dict) or item.get("reel_url") != url]
                    analyses.append(record)
                    records[url] = {
                        "status": "succeeded",
                        "attempt_count": attempt_count,
                        "reaction_rate": row.get("reaction_rate"),
                        "analyzed_at": record.get("fashion_analyzed_at", ""),
                    }
                else:
                    failed += 1
                    records[url] = {
                        "status": "failed",
                        "attempt_count": attempt_count,
                        "reaction_rate": row.get("reaction_rate"),
                        "last_error": detail,
                        "last_attempt_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                state.update({"threshold_percent": REACTION_RATE_THRESHOLD * 100, "records": records})
                if ok:
                    _write_json_atomic(output_path, analyses)
                # Persist output before marking this URL succeeded.
                _write_json_atomic(state_path, state)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    await _run_reel_insights(root, repository)
    return {"qualifying": len(candidates), "queued": len(pending), "succeeded": succeeded, "failed": failed}


__all__ = [
    "DEFAULT_ANALYZER_CONCURRENCY",
    "REACTION_RATE_THRESHOLD",
    "analyze_qualifying_fashion_reels",
    "parse_reaction_rate",
    "qualifying_reels",
]
