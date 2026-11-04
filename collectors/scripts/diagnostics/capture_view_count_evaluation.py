"""Snapshot a selected dataset without navigating or changing collection data."""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path


def main(argv=None):
    collectors = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=collectors / "data/collector/.datasets/fashion",
                        help="Dataset containing .collector/reels_history_active.csv")
    parser.add_argument("--output", type=Path, help="New directory for the snapshot")
    args = parser.parse_args(argv)
    data = args.data_dir.expanduser().resolve()
    history = data / ".collector/reels_history_active.csv"
    if not history.is_file():
        parser.error(f"History CSV not found: {history}. Select a dataset with --data-dir.")
    content = history.read_bytes()
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline="")))
    captured_at = datetime.now(timezone.utc)
    evidence = (args.output.expanduser().resolve() if args.output else
                data / "diagnostics/view_count_evaluation" / captured_at.strftime("%Y%m%dT%H%M%S%fZ"))
    if evidence.exists():
        parser.error(f"Output directory already exists: {evidence}. Choose a new directory.")
    evidence.mkdir(parents=True, exist_ok=False)
    fields = ["url", "collected_at", "collection_number", "username", "view_count", "follower_count"]
    with (evidence / "history_snapshot.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fields} for row in rows)
    groups = collections.defaultdict(collections.Counter)
    exact = lambda value: str(value or "").strip().replace(",", "").isascii() and str(value or "").strip().replace(",", "").isdigit()
    for row in rows:
        group = groups[row.get("collection_number") or "unknown"]
        group["rows"] += 1
        follower = exact(row.get("follower_count"))
        view = exact(row.get("view_count"))
        group["follower_present"] += follower
        group["view_present"] += view
        group["follower_present_view_missing"] += follower and not view
    log = data / ".collector/python.log"
    if log.is_file():
        (evidence / "python_log_snapshot.log").write_bytes(log.read_bytes())
    result = {"schema_version": 2, "captured_at": captured_at.isoformat(), "data_source": str(data),
              "history_source_sha256": hashlib.sha256(content).hexdigest(), "rounds_at_capture": dict(groups),
              "limitations": ["This is a current snapshot, not reconstructed historical or pre-patch data.",
                              "Attempt success, unique snapshot outcomes, and CSV coverage use different denominators.",
                              "Changes over time are observational; no randomized or matched before/after experiment was run."],
              "live_attempts": str(data / ".collector/view_count_evaluation/attempts")}
    (evidence / "capture_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(evidence))


if __name__ == "__main__":
    main()
