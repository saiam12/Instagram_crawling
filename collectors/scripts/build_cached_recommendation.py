"""
Builder script to generate `video_recommendation_cached.html`
Standalone, self-contained HTML that matches 127.0.0.1:8765/recommend.html
with full base64-embedded MP4 3D camera guide animations and complete scene scripts.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(r'C:\Instagram-crawling')
web_dir = ROOT / 'collectors/monitor/web'
guides_dir = ROOT / 'analyzers/output/guides'

# Read styles
styles_css = (web_dir / 'styles.css').read_text(encoding='utf-8', errors='ignore')
recommend_css = (web_dir / 'recommend.css').read_text(encoding='utf-8', errors='ignore')

# Read context and references
context = json.loads((ROOT / 'graphify-out/guide-video-live-input.json').read_text(encoding='utf-8'))
brief = context.get('brief', {})
plan = context.get('plan', {})
references = context.get('references', [])

# Assemble the 6 videos
video_files = [
    ('f0a409cc1495444e93742efb96d076bf', 1, 'format'),
    ('ccfc6aa8da4a4208a1eeeb501bcaeb82', 2, 'format'),
    ('ce3ec0af9dc94f0eb4430c2159ddb7c1', 3, 'format'),
    ('5313d8d2d6fa405da95fc049c6bc10dc', 4, 'blend'),
    ('c25cfd21ceb346a39ebbf56bf417a4a1', 5, 'blend'),
    ('16ddaa2296d445c694e3bdad202fbd1c', 6, 'blend'),
]

assembled_videos = []
for h, idx, mode in video_files:
    json_path = guides_dir / f"{h}.video.json"
    mp4_path = guides_dir / f"{h}.mp4"
    data = json.loads(json_path.read_text(encoding='utf-8'))
    meta = data.get('metadata', {})
    
    scenes = meta.get('scenes', [])
    if not scenes and 'motion' in data and 'scenes' in data['motion']:
        scenes = [{
            **{k: s[k] for k in ('start_second', 'end_second', 'shot', 'camera', 'action', 'caption') if k in s},
            'dialogue': s.get('caption', ''),
            'framing': {'reference_id': meta.get('reference_ids', ['R1'])[0], 'source_scene': i + 1}
        } for i, s in enumerate(data['motion']['scenes'])]
    
    b64_video = ""
    if mp4_path.exists():
        b64_video = "data:video/mp4;base64," + base64.b64encode(mp4_path.read_bytes()).decode('ascii')
    
    assembled_videos.append({
        'index': idx,
        'mode': mode,
        'title': meta.get('title'),
        'duration_seconds': meta.get('duration_seconds'),
        'filename': f"{h}.mp4",
        'reference_ids': meta.get('reference_ids', []),
        'features': meta.get('features', []),
        'scenes': scenes,
        'video_src': b64_video,
    })

print(f"Total videos assembled: {len(assembled_videos)}")

# Generate HTML
html = f"""<!doctype html>
<html lang="ko" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Collector Monitor · 영상 기획 추천 (캐시 완료본)</title>
  <style>
{styles_css}
{recommend_css}

/* Extra polishing for standalone cached viewer */
body.recommend-page {{
  min-height: 100vh;
  background: var(--background);
  color: var(--ink);
}}
.rec-video-media video {{
  width: 100%;
  max-width: 380px;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: #000;
  display: block;
  margin: 10px 0;
}}
.rec-video-body {{
  display: grid;
  grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}}
@media (max-width: 960px) {{
  .rec-video-body {{
    grid-template-columns: 1fr;
  }}
}}
.rec-video-nav {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 14px 0 20px;
}}
.rec-video-nav a {{
  padding: 8px 14px;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: var(--surface);
  color: var(--ink);
  font-size: 12px;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
  transition: all .15s ease;
}}
.rec-video-nav a:hover, .rec-video-nav a.active {{
  background: #2a455b;
  color: #fff;
  border-color: var(--primary);
}}
.pill-badge {{
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
}}
.pill-format {{ background: #1e3a5f; color: #60a5fa; }}
.pill-blend {{ background: #3b2d54; color: #c084fc; }}
.rec-video-card {{
  margin-bottom: 24px;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 20px;
  background: var(--surface);
}}
.rec-video-header {{
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  cursor: pointer;
}}
.rec-video-header h4 {{
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  flex: 1;
}}
.rec-copy-toast {{
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: #0f766e;
  color: #fff;
  padding: 12px 20px;
  border-radius: 8px;
  font-weight: 600;
  box-shadow: 0 4px 14px rgba(0,0,0,0.3);
  z-index: 9999;
  display: none;
}}
  </style>
</head>
<body class="recommend-page">
  <aside class="sidebar">
    <a class="brand" href="instagram_monitoring_dashboard.html#recommend" aria-label="통합 대시보드로 이동">
      <span class="brand-mark" aria-hidden="true">▥</span>
      <div>
        <span>COLLECTOR</span>
        <small>CACHED PLAN</small>
      </div>
    </a>
    <nav aria-label="주 메뉴">
      <div class="nav-group" role="group">
        <p class="nav-group-label">수집 및 분석</p>
        <a href="instagram_monitoring_dashboard.html#overview" class="nav-link">
          <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg></span>
          수집 현황
        </a>
        <a href="instagram_monitoring_dashboard.html#visualize" class="nav-link">
          <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 4v16h17M8 16v-5m5 5V7m5 9V4"/></svg></span>
          수집 시각화
        </a>
        <a href="instagram_monitoring_dashboard.html#analysis" class="nav-link">
          <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18M15 9v12M9 15h12"/></svg></span>
          분석 시각화
        </a>
        <a href="#recommend" class="nav-link" aria-current="page">
          <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m12 3 2.8 6.2L21 12l-6.2 2.8L12 21l-2.8-6.2L3 12l6.2-2.8Z"/></svg></span>
          영상 기획 추천 (캐시)
        </a>
      </div>
    </nav>
    <div class="sidebar-bottom">
      <span class="local-dot" aria-hidden="true"></span>로컬 캐시본
      <small>6개 가이드 영상 · 100% 완료</small>
    </div>
  </aside>

  <main id="main">
    <header class="page-header">
      <div>
        <h1>영상 기획 추천 (로컬 캐시 완료본)</h1>
        <p>로컬 collector 웹에 캐싱된 20대 남성 아우터 타겟 6종 프로덕션 가이드 영상 및 대본입니다.</p>
      </div>
      <div class="header-tools">
        <span class="badge good">캐시 완료 (6/6개)</span>
        <button id="theme-toggle" class="button secondary" type="button">라이트모드</button>
      </div>
    </header>

    <div class="rec-cache">
      <p id="cache-status">2026. 11. 10. 오전 1:57 자동 저장됨 · Antigravity 촬영 가이드 애니메이션 6종 포함</p>
      <div style="display: flex; gap: 8px;">
        <button id="copy-all-scripts" class="button secondary compact" type="button">전체 대본 복사</button>
        <button id="download-json" class="button secondary compact" type="button">기획안 JSON 저장</button>
      </div>
    </div>

    <div class="rec-layout">
      <!-- Left Column: Input Form & 6 References -->
      <div class="rec-input-column">
        <section class="panel rec-brief" aria-labelledby="brief-heading">
          <div class="rec-heading">
            <div><h2 id="brief-heading">어떤 영상을 만들까요?</h2></div>
            <span class="badge info">캐시된 조건</span>
          </div>
          <form id="brief-form" novalidate onsubmit="return false;">
            <div class="rec-field">
              <label for="dataset">참고할 데이터셋</label>
              <input id="dataset" value="reel_analysis.json (283건 분석 코퍼스)" readonly>
              <small id="dataset-path" class="rec-help">collectors/data/collector/reel_analysis.json</small>
            </div>
            <div class="rec-field">
              <label for="reference-country">참고 사례 구분</label>
              <input id="reference-country" value="한국 (201건 매칭 풀)" readonly>
            </div>
            <div class="rec-field">
              <label for="follower_count">내 계정 팔로워 수</label>
              <input id="follower_count" value="1,000" readonly>
              <small class="rec-help">유사 후보 중 계정 규모가 가까운 사례를 우선 반영했습니다.</small>
            </div>
            <div class="rec-field">
              <label for="product">제품</label>
              <input id="product" value="{brief.get('product', '아우터')}" readonly>
            </div>
            <div class="rec-field">
              <label for="target">타겟</label>
              <input id="target" value="{brief.get('target', '20대 남성')}" readonly>
            </div>
            <div class="rec-field">
              <label for="purpose">목적</label>
              <input id="purpose" value="{brief.get('purpose', '구매 유도')}" readonly>
            </div>
            <div class="rec-field">
              <label for="selling_point">핵심 소구점</label>
              <input id="selling_point" value="{brief.get('selling_point', '끈적임 없이 가벼운 착용감, 미니멀 실루엣')}" readonly>
            </div>
            <div class="rec-field">
              <label for="production_constraints">제작 조건</label>
              <input id="production_constraints" value="{brief.get('production_constraints', '얼굴 노출 없이, 전신 샷')}" readonly>
            </div>
          </form>
          <p class="rec-feedback">한국 201개 분석 릴스 · Embedding 2 의미 검색 + BM25 수집 세트 연합 130/201 · 6개 참고 사례를 찾았습니다.</p>
        </section>

        <!-- Reference Reels -->
        <section class="panel rec-references" aria-labelledby="reference-heading">
          <div class="rec-heading">
            <h2 id="reference-heading">참고 사례</h2>
            <span id="reference-count" class="badge info">{len(references)}개 사례 매칭</span>
          </div>
          <p id="source-note" class="rec-help">지정된 분석 JSON의 검증된 벤치마크 레코드입니다.</p>
          <div id="references">
"""

for ref in references:
    rid = ref.get('reference_id', '')
    reel_id = ref.get('reel_id', '')
    analysis = ref.get('analysis', {})
    summary = analysis.get('summary', '')
    product_desc = ""
    try:
        p_json = json.loads(analysis.get('product', '{}'))
        product_desc = p_json.get('description', '')
    except Exception:
        pass
    
    hook_desc = ""
    try:
        h_json = json.loads(analysis.get('hook', '{}'))
        hook_desc = h_json.get('description', '')
    except Exception:
        pass

    html += f"""
            <article class="rec-reference">
              <div class="rec-reference-top">
                <span class="rec-ref-id">{rid}</span>
                <a href="https://www.instagram.com/reel/{reel_id}/" target="_blank" rel="noopener noreferrer">
                  {reel_id} ↗
                </a>
              </div>
              <p style="font-weight:600; color:var(--ink);">{summary}</p>
              {f'<p style="font-size:11px; color:var(--muted); margin:4px 0;"><strong>착장/제품:</strong> {product_desc}</p>' if product_desc else ''}
              {f'<p style="font-size:11px; color:var(--primary); margin:4px 0;"><strong>Hook:</strong> {hook_desc}</p>' if hook_desc else ''}
            </article>
"""

html += f"""
          </div>
        </section>
      </div>

      <!-- Right Column: 6 Guide Videos & Scripts -->
      <section class="panel rec-output" aria-labelledby="plan-heading">
        <div class="rec-heading">
          <div><h2 id="plan-heading">가이드 영상 · 대본</h2></div>
          <span class="badge good">생성 가이드 6/6개 완료</span>
        </div>
        
        <div id="provider-status" class="rec-provider">
          Antigravity에 가이드 영상 6개와 영상별 장면 구성·대본을 직접 병렬 요청 완료한 캐시본입니다.
        </div>
        
        <div class="rec-brief-summary">
          제품: {brief.get('product', '아우터')} · 타겟: {brief.get('target', '20대 남성')} · 목적: {brief.get('purpose', '구매 유도')} · 제작 조건: {brief.get('production_constraints', '얼굴 노출 없이')} · 내 계정 팔로워: 1,000
        </div>

        <!-- Concept Summary Section -->
        <div style="background:var(--background); border:1px solid var(--line); border-radius:8px; padding:18px 20px; margin:20px 0;">
          <h3 class="rec-concept" style="margin:0 0 10px; font-size:20px; color:var(--ink);">
            {plan.get('concept', '20대 남성을 위한 핏별 아우터 전신 스타일링 룩북')}
          </h3>
          <p style="font-size:13px; line-height:1.7; color:var(--muted); margin-bottom:12px;">
            {plan.get('duration_reason', '')}
          </p>
          <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); gap:12px; font-size:12px;">
            <div><strong style="color:var(--primary);">Hook 강도:</strong> <span class="pill-badge pill-format">{plan.get('hook', '')}</span></div>
            <div><strong style="color:var(--primary);">BGM:</strong> <span>{plan.get('bgm', '')}</span></div>
          </div>
          <div style="margin-top:12px; font-size:12px; color:var(--muted);">
            <strong style="color:var(--primary);">캡션 예시:</strong>
            <p style="white-space:pre-wrap; margin-top:4px;">{plan.get('caption', '')}</p>
          </div>
        </div>

        <!-- Navigation Pills for 6 Videos -->
        <h3 style="font-size:15px; font-weight:700; margin:24px 0 10px;">영상별 가이드 · 장면 구성 · 대본</h3>
        <p class="rec-help">생성 가이드 6개(캐시본 6/6개 완료 · 참고 형식 3개 / 특징 혼합 3개)</p>
        <div class="rec-video-nav">
"""

for v in assembled_videos:
    mode_text = "참고 형식" if v['mode'] == 'format' else "특징 혼합"
    html += f"""          <a href="#guide-video-{v['index']}" onclick="openVideo({v['index']})">
            {v['index']}. {v['title']} ({v['duration_seconds']}초)
          </a>\n"""

html += """        </div>

        <!-- 6 Guide Video Cards -->
        <div class="rec-video-grid">
"""

for v in assembled_videos:
    mode_badge = '<span class="pill-badge pill-format">참고 형식</span>' if v['mode'] == 'format' else '<span class="pill-badge pill-blend">특징 혼합</span>'
    html += f"""
          <details class="rec-video-card" id="guide-video-{v['index']}" open>
            <summary class="rec-video-header">
              {mode_badge}
              <h4>{v['index']}. {v['title']}</h4>
              <span class="badge info">{v['duration_seconds']}초 · 장면 {len(v['scenes'])}개</span>
            </summary>
            
            <div class="rec-video-body">
              <!-- Left: Video Player & Download -->
              <div class="rec-video-media">
                <p class="rec-help" style="margin-bottom:6px;">위: 영상 가이드 (3D 카메라 모션) · 아래: 영상 다운로드</p>
                <video controls playsinline preload="metadata" src="{v['video_src']}" aria-label="{v['title']}"></video>
                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:8px;">
                  <small style="color:var(--muted);">{v['duration_seconds']}초 · {' · '.join(v['reference_ids'])}</small>
                  <a href="{v['video_src']}" download="촬영-가이드-{v['index']}.mp4" class="button secondary compact">영상 저장 (.mp4)</a>
                </div>
                {f'<ul style="margin-top:12px; font-size:11px; color:var(--muted); padding-left:18px;"><li>' + '</li><li>'.join(v['features']) + '</li></ul>' if v['features'] else ''}
              </div>

              <!-- Right: Scene Scripts -->
              <div class="rec-video-content">
                <details class="rec-video-script" open>
                  <summary style="font-weight:700; cursor:pointer; color:var(--primary); margin-bottom:12px;">
                    장면 구성 / 대본 (총 {len(v['scenes'])}장면)
                  </summary>
                  <p class="rec-help" style="margin-bottom:12px;">촬영용 대본 · 영상에는 음성이 포함되지 않습니다.</p>
                  <ol class="rec-scenes" aria-label="{v['index']}번 영상 장면 구성과 대본">
"""
    for s in v['scenes']:
        ref_id = s.get('framing', {}).get('reference_id', 'R1')
        source_scene = s.get('framing', {}).get('source_scene', 1)
        # find reel_id for ref_id
        matched_reel = next((r.get('reel_id') for r in references if r.get('reference_id') == ref_id), 'DeCqQTBJgSC')
        
        html += f"""
                    <li style="display:grid; grid-template-columns:32px minmax(0,1fr); gap:12px; padding:16px 0; border-bottom:1px solid var(--line); align-items:start;">
                      <div style="min-width:0; width:100%;">
                        <!-- Top row: Time badge & Camera/Shot info -->
                        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:6px;">
                          <span style="font-family:var(--mono, monospace); font-weight:700; font-size:12px; color:var(--primary); background:var(--soft-teal, rgba(15,118,110,0.12)); padding:2px 8px; border-radius:4px;">
                            {s.get('start_second', 0)}–{s.get('end_second', 1)}초
                          </span>
                          <span style="color:var(--muted); font-size:11px;">
                            화면 범위 · <strong style="color:var(--ink);">{s.get('shot', '전신')}</strong>  /  카메라 · <strong style="color:var(--ink);">{s.get('camera', '고정 카메라')}</strong>
                          </span>
                        </div>

                        <!-- Action description -->
                        <p style="margin:6px 0; font-size:13px; line-height:1.6; color:var(--ink);">
                          {s.get('action', '')}
                        </p>

                        <!-- Spoken Dialogue / Script Box -->
                        <div style="background:rgba(245,158,11,0.08); border-left:3px solid var(--warning, #f59e0b); border-radius:4px; padding:8px 12px; margin:8px 0; font-size:12px; display:flex; align-items:baseline; gap:8px;">
                          <span style="font-weight:700; color:var(--warning, #f59e0b); font-size:11px; flex-shrink:0;">대본</span>
                          <span style="font-weight:600; color:var(--ink); line-height:1.5;">"{s.get('dialogue', '대사 없음')}"</span>
                        </div>

                        <!-- Bottom meta: Caption and Reference Source Link -->
                        <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-top:6px; font-size:11px; color:var(--muted);">
                          <span>화면 자막 · <strong style="color:var(--ink);">{s.get('caption', '')}</strong></span>
                          <span>참고 영상 · <a href="https://www.instagram.com/reel/{matched_reel}/" target="_blank" rel="noopener noreferrer" style="color:var(--primary); font-weight:600; text-decoration:none;">{ref_id} · {matched_reel} ↗</a> · 원본 장면 {source_scene}</span>
                        </div>
                      </div>
                    </li>
"""
    html += """
                  </ol>
                </details>
              </div>
            </div>
          </details>
"""

html += f"""
        </div>
      </section>
    </div>
  </main>

  <div id="toast" class="rec-copy-toast">클립보드에 복사되었습니다!</div>

  <script>
    // Theme toggle
    const themeBtn = document.getElementById('theme-toggle');
    themeBtn.addEventListener('click', () => {{
      const cur = document.documentElement.dataset.theme;
      const next = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      themeBtn.textContent = next === 'dark' ? '라이트모드' : '다크모드';
    }});

    function openVideo(idx) {{
      const card = document.getElementById('guide-video-' + idx);
      if (card) {{
        card.open = true;
        card.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }}
    }}

    function showToast(msg) {{
      const toast = document.getElementById('toast');
      toast.textContent = msg;
      toast.style.display = 'block';
      setTimeout(() => toast.style.display = 'none', 2500);
    }}

    // Copy all scripts
    const videosData = {json.dumps(assembled_videos, ensure_ascii=False)};
    document.getElementById('copy-all-scripts').addEventListener('click', () => {{
      let fullText = "=== [Collector Monitor] 20대 남성 아우터 영상 기획 대본집 ===\\n\\n";
      videosData.forEach(v => {{
        fullText += `[영상 ${{v.index}}] ${{v.title}} (${{v.duration_seconds}}초)\\n`;
        v.scenes.forEach((s, i) => {{
          fullText += `  #${{i+1}} (${{s.start_second}}~${{s.end_second}}초) [${{s.shot}} / ${{s.camera}}]\\n`;
          fullText += `    - 연출: ${{s.action}}\\n`;
          fullText += `    - 대본: ${{s.dialogue}}\\n`;
          fullText += `    - 자막: ${{s.caption}}\\n`;
        }});
        fullText += "\\n";
      }});
      navigator.clipboard.writeText(fullText).then(() => showToast("전체 6종 영상 대본이 클립보드에 복사되었습니다!"));
    }});

    // Download JSON
    document.getElementById('download-json').addEventListener('click', () => {{
      const blob = new Blob([JSON.stringify(videosData, null, 2)], {{ type: 'application/json' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'cached_video_recommendations.json';
      a.click();
      URL.revokeObjectURL(url);
    }});
  </script>
</body>
</html>
"""

output_path = ROOT / 'video_recommendation_cached.html'
output_path.write_text(html, encoding='utf-8')
print(f"Generated standalone cached HTML: {output_path} (size: {output_path.stat().st_size / 1024 / 1024:.2f} MB)")
