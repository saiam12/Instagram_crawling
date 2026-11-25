import json
import base64
from pathlib import Path

ROOT = Path(r'C:\Instagram-crawling')
guides_dir = ROOT / 'analyzers/output/guides'

# Read context and references
context = json.loads((ROOT / 'graphify-out/guide-video-live-input.json').read_text(encoding='utf-8'))
brief = context.get('brief', {})
plan = context.get('plan', {})
references = context.get('references', [])

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

def generate_cached_panel_html():
    out = '''
      <div id="rec-cached-panel">
        <div class="rec-cache" style="margin-bottom:18px;">
          <p style="color:var(--muted); font-size:12px;">2026. 11. 10. 오전 1:57 자동 저장됨 · 로컬 collector 웹 캐시 6/6개 완료 (3D 카메라 모션 포함)</p>
          <div style="display: flex; gap: 8px;">
            <button id="copy-cached-scripts-btn" class="button secondary compact" type="button">전체 대본 복사</button>
            <button id="download-cached-json-btn" class="button secondary compact" type="button">기획안 JSON 저장</button>
          </div>
        </div>

        <div class="rec-layout">
          <!-- Left Column: Input Form & 6 References -->
          <div class="rec-input-column">
            <section class="panel rec-brief" aria-labelledby="cached-brief-heading">
              <div class="rec-heading">
                <div><h2 id="cached-brief-heading">어떤 영상을 만들까요?</h2></div>
                <span class="badge info">캐시된 입력 조건</span>
              </div>
              <form novalidate onsubmit="return false;">
                <div class="rec-field">
                  <label>참고할 데이터셋</label>
                  <input value="reel_analysis.json (283건 분석 코퍼스)" readonly style="background:var(--background); color:var(--ink); border:1px solid var(--line); padding:10px 12px; border-radius:6px; width:100%;">
                  <small class="rec-help">collectors/data/collector/reel_analysis.json</small>
                </div>
                <div class="rec-field">
                  <label>참고 사례 구분</label>
                  <input value="한국 (201건 매칭 풀)" readonly style="background:var(--background); color:var(--ink); border:1px solid var(--line); padding:10px 12px; border-radius:6px; width:100%;">
                </div>
                <div class="rec-field">
                  <label>내 계정 팔로워 수</label>
                  <input value="1,000" readonly style="background:var(--background); color:var(--ink); border:1px solid var(--line); padding:10px 12px; border-radius:6px; width:100%;">
                  <small class="rec-help">입력하면 내용이 유사한 후보 중 계정 규모가 가까운 사례를 우선합니다.</small>
                </div>
                <div class="rec-field">
                  <label>제품</label>
                  <input value="''' + brief.get('product', '아우터') + '''" readonly style="background:var(--background); color:var(--ink); border:1px solid var(--line); padding:10px 12px; border-radius:6px; width:100%;">
                </div>
                <div class="rec-field">
                  <label>타겟</label>
                  <input value="''' + brief.get('target', '20대 남성') + '''" readonly style="background:var(--background); color:var(--ink); border:1px solid var(--line); padding:10px 12px; border-radius:6px; width:100%;">
                </div>
                <div class="rec-field">
                  <label>목적</label>
                  <input value="''' + brief.get('purpose', '구매 유도') + '''" readonly style="background:var(--background); color:var(--ink); border:1px solid var(--line); padding:10px 12px; border-radius:6px; width:100%;">
                </div>
                <div class="rec-field">
                  <label>핵심 소구점</label>
                  <input value="''' + brief.get('selling_point', '끈적임 없이 가벼운 착용감, 미니멀 실루엣') + '''" readonly style="background:var(--background); color:var(--ink); border:1px solid var(--line); padding:10px 12px; border-radius:6px; width:100%;">
                </div>
                <div class="rec-field">
                  <label>제작 조건</label>
                  <input value="''' + brief.get('production_constraints', '얼굴 노출 없이, 전신 샷') + '''" readonly style="background:var(--background); color:var(--ink); border:1px solid var(--line); padding:10px 12px; border-radius:6px; width:100%;">
                </div>
              </form>
              <p class="rec-feedback">한국 201개 분석 릴스 · Embedding 2 의미 검색 + BM25 수집 세트 연합 130/201 · 6개 참고 사례를 찾았습니다.</p>
            </section>

            <!-- Reference Reels -->
            <section class="panel rec-references" aria-labelledby="cached-reference-heading">
              <div class="rec-heading">
                <h2 id="cached-reference-heading">참고 사례</h2>
                <span class="badge info">''' + str(len(references)) + '''개 사례 매칭</span>
              </div>
              <p class="rec-help">지정된 분석 JSON의 벤치마크 고성과 레코드입니다.</p>
              <div style="max-height:500px; overflow-y:auto; padding-right:8px;">
    '''
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

        out += f'''
                <article class="rec-reference" style="border-top:1px solid var(--line); padding:14px 0;">
                  <div class="rec-reference-top" style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
                    <span class="rec-ref-id" style="color:var(--primary); font-weight:700;">{rid}</span>
                    <a href="https://www.instagram.com/reel/{reel_id}/" target="_blank" rel="noopener noreferrer" style="font-weight:600; text-decoration:none;">
                      {reel_id} ↗
                    </a>
                  </div>
                  <p style="font-size:12px; line-height:1.6; color:var(--ink); margin:4px 0;">{summary}</p>
                  {f'<p style="font-size:11px; color:var(--muted); margin:4px 0;"><strong>착장:</strong> {product_desc}</p>' if product_desc else ''}
                  {f'<p style="font-size:11px; color:var(--primary); margin:4px 0;"><strong>Hook:</strong> {hook_desc}</p>' if hook_desc else ''}
                </article>
        '''

    out += '''
              </div>
            </section>
          </div>

          <!-- Right Column: 6 Guide Videos & Scripts -->
          <section class="panel rec-output" aria-labelledby="cached-plan-heading">
            <div class="rec-heading">
              <div><h2 id="cached-plan-heading">가이드 영상 · 대본</h2></div>
              <span class="badge good">생성 가이드 6/6개 완료</span>
            </div>
            
            <div class="rec-provider">
              Antigravity에 가이드 영상 6개와 영상별 장면 구성·대본을 직접 병렬 요청 완료한 캐시본입니다.
            </div>
            
            <div class="rec-brief-summary" style="margin-top:14px; padding:10px 14px; background:var(--soft-teal); border-left:3px solid var(--primary); font-size:12px;">
              제품: ''' + brief.get('product', '아우터') + ''' · 타겟: ''' + brief.get('target', '20대 남성') + ''' · 목적: ''' + brief.get('purpose', '구매 유도') + ''' · 제작 조건: ''' + brief.get('production_constraints', '얼굴 노출 없이') + ''' · 내 계정 팔로워: 1,000
            </div>

            <!-- Concept Summary Section -->
            <div style="background:var(--background); border:1px solid var(--line); border-radius:8px; padding:16px 18px; margin:16px 0;">
              <h3 style="margin:0 0 8px; font-size:18px; color:var(--ink);">
                ''' + plan.get('concept', '20대 남성을 위한 핏별 아우터 전신 스타일링 룩북') + '''
              </h3>
              <p style="font-size:12px; line-height:1.6; color:var(--muted); margin-bottom:10px;">
                ''' + plan.get('duration_reason', '') + '''
              </p>
              <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:10px; font-size:12px;">
                <div><strong style="color:var(--primary);">Hook 강도:</strong> <span class="pill-badge pill-format">''' + plan.get('hook', '') + '''</span></div>
                <div><strong style="color:var(--primary);">BGM:</strong> <span>''' + plan.get('bgm', '') + '''</span></div>
              </div>
            </div>

            <!-- Navigation Pills for 6 Videos -->
            <h3 style="font-size:14px; font-weight:700; margin:20px 0 8px;">영상별 가이드 · 장면 구성 · 대본</h3>
            <p class="rec-help" style="margin-bottom:10px;">생성 가이드 6개(캐시본 6/6개 완료 · 참고 형식 3개 / 특징 혼합 3개)</p>
            <div style="display:flex; flex-wrap:wrap; gap:8px; margin-bottom:18px;">
    '''

    for v in assembled_videos:
        out += f'''              <button class="button secondary compact" type="button" onclick="scrollCachedVideo({v['index']})" style="font-size:11px;">
                {v['index']}. {v['title']} ({v['duration_seconds']}초)
              </button>\n'''

    out += '''            </div>

            <!-- 6 Guide Video Cards -->
            <div class="rec-video-grid" style="display:grid; gap:20px;">
    '''

    for v in assembled_videos:
        mode_badge = '<span class="pill-badge pill-format">참고 형식</span>' if v['mode'] == 'format' else '<span class="pill-badge pill-blend">특징 혼합</span>'
        out += f'''
              <details class="rec-video-card" id="dash-guide-video-{v['index']}" open style="border:1px solid var(--line); border-radius:8px; padding:18px; background:var(--surface);">
                <summary class="rec-video-header" style="display:flex; align-items:center; gap:10px; cursor:pointer;">
                  {mode_badge}
                  <h4 style="margin:0; font-size:15px; font-weight:700; flex:1;">{v['index']}. {v['title']}</h4>
                  <span class="badge info">{v['duration_seconds']}초 · 장면 {len(v['scenes'])}개</span>
                </summary>
                
                <div class="rec-video-body" style="display:grid; grid-template-columns:340px minmax(0, 1fr); gap:24px; align-items:start; margin-top:16px;">
                  <!-- Left: Video Player & Download -->
                  <div class="rec-video-media" style="width:340px;">
                    <p class="rec-help" style="margin-bottom:6px; font-size:11px;">위: 영상 가이드 (3D 카메라 모션) · 아래: 영상 다운로드</p>
                    <video controls playsinline preload="metadata" src="{v['video_src']}" aria-label="{v['title']}" style="width:100%; border-radius:6px; border:1px solid var(--line); background:#000; display:block;"></video>
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:8px;">
                      <small style="color:var(--muted); font-size:11px;">{v['duration_seconds']}초 · {' · '.join(v['reference_ids'])}</small>
                      <a href="{v['video_src']}" download="촬영-가이드-{v['index']}.mp4" class="button secondary compact" style="font-size:11px; padding:4px 8px;">영상 저장 (.mp4)</a>
                    </div>
                    {f'<ul style="margin-top:10px; font-size:11px; color:var(--muted); padding-left:16px;"><li>' + '</li><li>'.join(v['features']) + '</li></ul>' if v['features'] else ''}
                  </div>

                  <!-- Right: Scene Scripts -->
                  <div class="rec-video-content" style="min-width:0;">
                    <details open>
                      <summary style="font-weight:700; cursor:pointer; color:var(--primary); font-size:13px; margin-bottom:12px;">
                        장면 구성 / 대본 (총 {len(v['scenes'])}장면)
                      </summary>
                      <p class="rec-help" style="margin-bottom:12px; font-size:11px;">촬영용 대본 · 영상에는 음성이 포함되지 않습니다.</p>
                      <ol class="rec-scenes" style="list-style:none; padding:0; margin:0;">
        '''
        for s in v['scenes']:
            ref_id = s.get('framing', {}).get('reference_id', 'R1')
            source_scene = s.get('framing', {}).get('source_scene', 1)
            matched_reel = next((r.get('reel_id') for r in references if r.get('reference_id') == ref_id), 'DeCqQTBJgSC')
            out += f'''
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
            '''
        out += '''
                      </ol>
                    </details>
                  </div>
                </div>
              </details>
        '''

    out += '''
            </div>
          </section>
        </div>
      </div>
    '''
    return out, assembled_videos

if __name__ == '__main__':
    panel_html, vids = generate_cached_panel_html()
    print("Generated cached panel HTML length:", len(panel_html))
    print("Videos count:", len(vids))
