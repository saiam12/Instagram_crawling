"""Create a duration-versus-play-count Reel scatter plot."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def read_reel_points(csv_path: Path) -> list[dict[str, float]]:
    points: list[dict[str, float]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                duration = float(row.get("video_duration_seconds", ""))
                play_count = float(row.get("view_count", ""))
            except (TypeError, ValueError):
                continue
            if 0 < duration <= 400 and play_count > 0:
                points.append({"duration": duration, "playCount": play_count})
    return points


def write_duration_vs_play_count_visualization(points: list[dict[str, float]], output_path: Path) -> None:
    if not points:
        raise ValueError("No Reel rows have both video_duration_seconds and view_count.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(points, ensure_ascii=False, separators=(",", ":"))
    output_path.write_text(
        f'''<style>
#duration-play-count {{
  color-scheme: light dark;
  --viz-series-1: light-dark(#1d4ed8, #60a5fa);
}}
</style>
<div id="duration-play-count">
  <h3>영상 길이와 재생 수</h3>
  <svg role="img" aria-label="400초 이하 영상의 길이와 재생 수 산점도"></svg>
</div>
<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>
<script>
(() => {{
  const root = document.getElementById("duration-play-count");
  const points = {data};
  const svg = d3.select(root).select("svg");
  const xExtent = d3.extent(points, d => d.duration);
  const yExtent = d3.extent(points, d => d.playCount);

  function draw() {{
    const width = Math.max(320, root.clientWidth || 736);
    const height = Math.max(360, Math.round(width * 0.6));
    const margin = {{ top: 28, right: 24, bottom: 64, left: 82 }};
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const xPadding = Math.max(1, (xExtent[1] - xExtent[0]) * 0.03);
    const x = d3.scaleLinear()
      .domain([Math.max(0, xExtent[0] - xPadding), xExtent[1] + xPadding])
      .range([margin.left, width - margin.right]);
    const y = d3.scaleLog()
      .domain([Math.max(1, yExtent[0] / 1.5), yExtent[1] * 1.5])
      .range([height - margin.bottom, margin.top]);

    svg.attr("viewBox", `0 0 ${{width}} ${{height}}`)
      .attr("width", width)
      .attr("height", height);
    svg.selectAll("*").remove();
    svg.append("title").text("영상 길이와 재생 수");
    svg.append("desc").text(`수집된 ${{points.length.toLocaleString()}}개, 400초 이하 Reel의 영상 길이와 재생 수입니다. 수직 축은 로그 스케일입니다.`);
    svg.append("rect")
      .attr("data-chart-frame", "")
      .attr("x", margin.left)
      .attr("y", margin.top)
      .attr("width", plotWidth)
      .attr("height", plotHeight)
      .attr("fill", "none")
      .attr("stroke", "var(--border)");
    svg.append("g")
      .attr("transform", `translate(0,${{height - margin.bottom}})`)
      .call(d3.axisBottom(x).ticks(width < 500 ? 4 : 7));
    svg.append("g")
      .attr("transform", `translate(${{margin.left}},0)`)
      .call(d3.axisLeft(y).ticks(6, "~s"));
    svg.append("g")
      .selectAll("circle")
      .data(points)
      .join("circle")
      .attr("cx", d => x(d.duration))
      .attr("cy", d => y(d.playCount))
      .attr("r", 2.5)
      .attr("fill", "var(--viz-series-1)")
      .attr("opacity", 0.5)
      .attr("aria-label", d => `영상 길이 ${{d.duration.toFixed(1)}}초, 재생 수 ${{Math.round(d.playCount).toLocaleString()}}`);
    svg.append("text")
      .attr("class", "axis-title")
      .attr("data-axis", "x")
      .attr("x", margin.left + plotWidth / 2)
      .attr("y", height - 16)
      .attr("text-anchor", "middle")
      .attr("fill", "var(--foreground)")
      .text("video_duration (seconds)");
    svg.append("text")
      .attr("class", "axis-title")
      .attr("data-axis", "y")
      .attr("transform", `translate(20,${{margin.top + plotHeight / 2}}) rotate(-90)`)
      .attr("text-anchor", "middle")
      .attr("fill", "var(--foreground)")
      .text("play_count (log scale)");
  }}

  new ResizeObserver(draw).observe(root);
  draw();
}})();
</script>
''',
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=BASE_DIR.parent / "outputs" / "duration-vs-play-count" / "duration-vs-play-count.html",
    )
    arguments = parser.parse_args()
    points = [
        *read_reel_points(BASE_DIR / "data_web" / "fashion_reels.csv"),
        *read_reel_points(BASE_DIR / "data_web" / "beauty_reels.csv"),
    ]
    write_duration_vs_play_count_visualization(points, arguments.output)
    print(f"Visualization saved: {arguments.output} ({len(points):,} Reel rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
