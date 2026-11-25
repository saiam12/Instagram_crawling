"""Read the temporary comparison dataset without changing its selection metrics."""
import json
from datetime import datetime, timezone
from statistics import median

from analysis_data import read_analysis
from visualization_data import metric_number


SOURCE = "analyzers/output/temp_cohort_comparison_20261119"
GROUPS = {
    "01_views_100x_followers": "① 팔로워 대비 조회수 100배 이상",
    "02_korean_top_views": "② 한국 릴스 조회수 상위",
    "03_followers_10000_ratio_5_random": "③ 팔로워 1만 이상·조회수 5배 무작위",
    "04_age_3d_low_views": "④ 업로드 3일 이상·조회수 하위",
}


def comparison_snapshot(root):
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("임시 그룹 선정 파일을 읽을 수 없습니다.") from None

    groups, all_ids, overlapping = [], set(), set()
    for index, (key, title) in enumerate(GROUPS.items()):
        if index == 0:
            source = root / (key + ".json")
            selected = json.loads(source.read_text(encoding="utf-8"))
            reused, fresh = read_analysis(source, frozen_metrics=True)["rows"], []
        else:
            selected = manifest["cohorts"][key]
            reused = read_analysis(root / key / "reused.json", frozen_metrics=True)["rows"]
            fresh = read_analysis(root / key / "new.json", frozen_metrics=True)["rows"]
        ids = {row["reel_id"] for row in selected}
        overlapping.update(all_ids & ids)
        all_ids.update(ids)
        rows = {row["reel_id"]: {**row, "analysis_origin": origin}
                for origin, entries in (("reused", reused), ("new", fresh)) for row in entries
                if row["reel_id"] in ids}
        reused_count = sum(row["analysis_origin"] == "reused" for row in rows.values())
        stats = {}
        for field in ("view_count", "follower_count", "view_follower_ratio", "days_since_upload"):
            numbers = [n for row in selected if (n := metric_number(row.get(field))) is not None]
            stats[field] = {"median": median(numbers) if numbers else None, "count": len(numbers)}
        groups.append({"id": key, "title": title, "total": len(ids), "reused": reused_count,
                       "new": len(rows) - reused_count, "pending": len(ids) - len(rows), "stats": stats,
                       "rows": [rows[row["reel_id"]] for row in selected if row["reel_id"] in rows]})
    return {"version": 1, "source": SOURCE, "loaded_at": datetime.now(timezone.utc).isoformat(),
            "overlap": len(overlapping), "groups": groups, "warnings": []}
