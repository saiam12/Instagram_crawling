"""
Standalone Monitoring Dashboard HTML Builder
Bundles:
1. Collection Visualization (3,811 reels, 15,244 snapshots, filters, SVG scatter charts, trends)
2. Analysis Visualization (283 detailed AI analyzed reels, audience, hooks, appeals, scenes)
3. Video Planning Recommendations (Interactive client-side BM25 similarity search + 30 production video plans)
4. Reel Insights Summary (Hook, Scene, Movement patterns)
"""
from __future__ import annotations

import sys
import json
import csv
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, r'C:\Instagram-crawling\collectors')
sys.path.insert(0, r'C:\Instagram-crawling\collectors\monitor\backend')

from analysis_data import read_analysis
from generate_cached_recommendation_panel import generate_cached_panel_html

def build():
    print("1. Extracting collection history...")
    csv_path = Path(r'C:\Instagram-crawling\collectors\data\collector\reels.csv')
    reels_rows = []
    
    def num(val):
        if val is None or val == '':
            return None
        if str(val).strip().upper() == 'X':
            return 'X'
        try:
            v = float(val)
            return int(v) if v.is_integer() else round(v, 2)
        except (ValueError, TypeError):
            return None

    with open(csv_path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            clean = {k.strip('\"'): v for k, v in row.items() if k}
            url = clean.get('url', '')
            shortcode = ''
            if '/reels/' in url or '/reel/' in url:
                parts = [p for p in url.split('?')[0].split('/') if p]
                if len(parts) >= 2:
                    shortcode = parts[-1]
            if not shortcode:
                shortcode = url

            k_raw = clean.get('korean', '').strip().lower()
            k_val = True if k_raw == 'true' else (False if k_raw == 'false' else None)

            reels_rows.append([
                int(clean.get('collection_number') or 1),
                clean.get('collected_at', '')[:16],
                shortcode,
                clean.get('username', ''),
                k_val,
                num(clean.get('view_count')),
                num(clean.get('like_count')),
                num(clean.get('comment_count')),
                num(clean.get('share_count')),
                num(clean.get('repost_count')),
                num(clean.get('saved_count')),
                num(clean.get('follower_count')),
                num(clean.get('video_duration_seconds')),
            ])
    print(f"Total collection rows: {len(reels_rows)}")

    print("2. Extracting analysis data...")
    p_analysis = Path(r'C:\Instagram-crawling\collectors\data\collector\reel_analysis.json')
    analysis_bundle = read_analysis(p_analysis)
    print(f"Total analysis rows: {len(analysis_bundle['rows'])}")

    print("3. Extracting video guide plans...")
    guides_dir = Path(r'C:\Instagram-crawling\analyzers\output\guides')
    plans = []
    for p in guides_dir.glob('*.video.json'):
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            meta = data.get('metadata')
            if meta and meta.get('title'):
                plans.append(meta)
        except Exception:
            pass
    print(f"Total video plans: {len(plans)}")

    print("4. Extracting insights summary...")
    p_insights = Path(r'C:\Instagram-crawling\collectors\data\collector\reel_insights.json')
    insights_data = {}
    if p_insights.exists():
        try:
            insights_data = json.loads(p_insights.read_text(encoding='utf-8'))
        except Exception:
            pass

    # Read existing CSS styles and JS modules
    web_dir = Path(r'C:\Instagram-crawling\collectors\monitor\web')
    styles_css = (web_dir / 'styles.css').read_text(encoding='utf-8', errors='ignore')
    visualize_css = (web_dir / 'visualize.css').read_text(encoding='utf-8', errors='ignore')
    analysis_css = (web_dir / 'analysis.css').read_text(encoding='utf-8', errors='ignore')
    recommend_css = (web_dir / 'recommend.css').read_text(encoding='utf-8', errors='ignore')
    korean_filter_js = (web_dir / 'korean-filter.js').read_text(encoding='utf-8', errors='ignore')
    scatter_js = (web_dir / 'scatter.js').read_text(encoding='utf-8', errors='ignore')
    chart_board_js = (web_dir / 'chart-board.js').read_text(encoding='utf-8', errors='ignore')

    print("4b. Generating cached recommendation panel...")
    cached_panel_html, cached_vids = generate_cached_panel_html()
    light_vids = [{k: v for k, v in item.items() if k != 'video_src'} for item in cached_vids]
    cached_vids_json_str = json.dumps(light_vids, ensure_ascii=False)

    # Bundle json strings
    reels_json_str = json.dumps(reels_rows, separators=(',', ':'), ensure_ascii=False)
    analysis_json_str = json.dumps(analysis_bundle['rows'], separators=(',', ':'), ensure_ascii=False)
    plans_json_str = json.dumps(plans, separators=(',', ':'), ensure_ascii=False)
    insights_json_str = json.dumps(insights_data, separators=(',', ':'), ensure_ascii=False)

    template_path = Path(r'C:\Instagram-crawling\collectors\scripts\standalone_template.html')
    template = template_path.read_text(encoding='utf-8')

    html = template.replace('/*__STYLES_CSS__*/', styles_css)
    html = html.replace('/*__VISUALIZE_CSS__*/', visualize_css)
    html = html.replace('/*__ANALYSIS_CSS__*/', analysis_css)
    html = html.replace('/*__RECOMMEND_CSS__*/', recommend_css)
    html = html.replace('/*__KOREAN_FILTER_JS__*/', korean_filter_js)
    html = html.replace('/*__SCATTER_JS__*/', scatter_js)
    html = html.replace('/*__CHART_BOARD_JS__*/', chart_board_js)
    html = html.replace('<!--__CACHED_RECOMMENDATION_PANEL__-->', cached_panel_html)
    html = html.replace('"__CACHED_VIDEOS__"', cached_vids_json_str)
    html = html.replace('"__REELS_DATA__"', reels_json_str)
    html = html.replace('"__ANALYSIS_DATA__"', analysis_json_str)
    html = html.replace('"__PLANS_DATA__"', plans_json_str)
    html = html.replace('"__INSIGHTS_DATA__"', insights_json_str)
    html = html.replace('__UPDATED_AT__', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))

    output_path = Path(r'C:\Instagram-crawling\instagram_monitoring_dashboard.html')
    output_path.write_text(html, encoding='utf-8')
    print(f"5. Generated standalone HTML: {output_path} (size: {output_path.stat().st_size / 1024 / 1024:.2f} MB)")

if __name__ == '__main__':
    build()
