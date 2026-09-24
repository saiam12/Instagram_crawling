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


# ``reaction_rate`` is stored as a ratio (90.0 means 9000%).
REACTION_RATE_THRESHOLD = 90.0
DEFAULT_ANALYZER_CONCURRENCY = 4


def _history_path(data_root: Path) -> Path:
    return data_root / ".collector" / "reels_history_active.csv"


def _state_path(data_root: Path) -> Path:
    return data_root / ".collector" / "fashion_analyzer_state.json"


def _analysis_output_path(data_root: Path) -> Path:
    if data_root.parent.name == ".datasets":
        return data_root.parent.parent / "fashion_reel_analyses.json"
    return data_root / "fashion_reel_analyses.json"


async def _run_reel_insights(data_root: Path, repo_root: Path) -> None:
    analysis_path = _analysis_output_path(data_root)
    script = repo_root / "analyzers" / "gemini" / "reel_insights.py"
    if not analysis_path.is_file() or not script.is_file():
        return
    public_root = analysis_path.parent
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    try:
        process = await asyncio.create_subprocess_exec(
            _analyzer_python(repo_root),
            str(script),
            "--input", str(analysis_path),
            "--output", str(public_root / "fashion_reel_insights.json"),
            "--report", str(public_root / "fashion_reel_insights.html"),
            "--state", str(data_root / ".collector" / "fashion_reel_insights_state.json"),
            cwd=str(script.parent),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
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
            return [dict(row) for row in csv.DictReader(handle)]
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
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _analyzer_python(repo_root: Path) -> str:
    configured = os.getenv("FASHION_ANALYZER_PYTHON", "").strip()
    if configured:
        return configured
    candidate = repo_root / "analyzers" / "gemini" / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


async def _run_analyzer(
    *,
    url: str,
    output_file: Path,
    analyzer_script: Path,
    analyzer_python: str,
) -> tuple[str, Path, int, str]:
    environment = os.environ.copy()
    environment["GEMINI_OUTPUT_FILE"] = str(output_file)
    environment["PYTHONUTF8"] = "1"
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
    script = repository / "analyzers" / "gemini" / "reel_analyzer.py"
    candidates = qualifying_reels(_read_rows(root))
    state_path = _state_path(root)
    state = _load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    records = state.get("records")
    if not isinstance(records, dict):
        records = {}
    pending = [row for row in candidates if records.get(str(row["url"]), {}).get("status") != "succeeded"]
    if not pending:
        await _run_reel_insights(root, repository)
        return {"qualifying": len(candidates), "queued": 0, "succeeded": 0, "failed": 0}
    if not script.is_file():
        return {"qualifying": len(candidates), "queued": len(pending), "succeeded": 0, "failed": len(pending)}

    limit = max(1, min(8, int(concurrency or 1)))
    analyzer_python = _analyzer_python(repository)
    output_path = _analysis_output_path(root)
    existing = _load_json(output_path, [])
    analyses = existing if isinstance(existing, list) else []
    semaphore = asyncio.Semaphore(limit)

    async def run_one(row: dict[str, Any], directory: Path) -> tuple[dict[str, Any], bool, str]:
        async with semaphore:
            digest = hashlib.sha1(str(row["url"]).encode("utf-8")).hexdigest()
            result_file = directory / f"{digest}.json"
            url, path, code, detail = await _run_analyzer(
                url=str(row["url"]),
                output_file=result_file,
                analyzer_script=script,
                analyzer_python=analyzer_python,
            )
            if code != 0:
                return row, False, detail or f"analyzer exited with code {code}"
            payload = _load_json(path, None)
            if not isinstance(payload, list) or not payload:
                return row, False, "analyzer did not produce a JSON result"
            record = payload[-1] if isinstance(payload[-1], dict) else {}
            record = {
                **record,
                "reel_url": url,
                "reaction_rate": row.get("reaction_rate"),
                "fashion_analyzed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            return row, True, json.dumps(record, ensure_ascii=False)

    with tempfile.TemporaryDirectory(prefix="fashion-analyzer-") as temporary:
        results = await asyncio.gather(*(run_one(row, Path(temporary)) for row in pending))

    succeeded = failed = 0
    for row, ok, detail in results:
        url = str(row["url"])
        if ok:
            succeeded += 1
            record = json.loads(detail)
            analyses = [item for item in analyses if not isinstance(item, dict) or item.get("reel_url") != url]
            analyses.append(record)
            records[url] = {
                "status": "succeeded",
                "reaction_rate": row.get("reaction_rate"),
                "analyzed_at": record.get("fashion_analyzed_at", ""),
            }
        else:
            failed += 1
            records[url] = {
                "status": "failed",
                "reaction_rate": row.get("reaction_rate"),
                "last_error": detail,
                "last_attempt_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
    state.update({"threshold_percent": REACTION_RATE_THRESHOLD * 100, "records": records})
    _write_json_atomic(state_path, state)
    if succeeded:
        _write_json_atomic(output_path, analyses)
    await _run_reel_insights(root, repository)
    return {"qualifying": len(candidates), "queued": len(pending), "succeeded": succeeded, "failed": failed}


__all__ = [
    "DEFAULT_ANALYZER_CONCURRENCY",
    "REACTION_RATE_THRESHOLD",
    "analyze_qualifying_fashion_reels",
    "parse_reaction_rate",
    "qualifying_reels",
]
