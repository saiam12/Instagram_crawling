"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const source = "collectors/data/collector/reel_analysis.json";
  const fmt = new Intl.NumberFormat("ko-KR", {maximumFractionDigits:1});
  const date = new Intl.DateTimeFormat("ko-KR", {timeZone:"Asia/Seoul", dateStyle:"short", timeStyle:"short"});
  const labels = {unknown:"미상", all:"전체 타겟", female:"여성", male:"남성", mixed:"혼합", other:"기타", "10s":"10대", "20s":"20대", "30s":"30대", "20s_30s":"20~30대", "40s_plus":"40대 이상", strong:"강함", medium:"보통", weak:"약함", lookbook:"룩북", product_demo:"제품 시연", fashion_lookbook:"패션 룩북", styling_inspiration:"스타일링 제안", product_feature:"제품 특징", price_value:"가격·가성비", convenience:"편의성", product_variety:"제품 다양성", novelty:"새로움", brand_identity:"브랜드 정체성", transformation:"변화·전후 비교", comedy_skit:"상황극", store_tour:"매장 소개", try_on_haul:"착용·구매 후기", talking_head:"토크", daily_vlog:"일상 브이로그", slow:"느림", fast:"빠름"};
  const label = value => labels[value] || value;
  Object.assign(labels, {daily_lookbook:"데일리 룩북", fashion_recommendation:"패션 추천", fashion_review:"패션 후기", fashion_tips:"패션 팁", life_hack_tutorial:"생활 팁·사용법", meme:"밈", mirror_selfie_fashion_review:"거울 셀피·패션 후기", outfit_lookbook:"착장 룩북", parenting_item_review:"육아용품 후기", quote_aesthetic:"문구·감성 영상", reaction:"리액션", short_form_transition:"전환 연출", sketch_comedy:"코미디 상황극", street_fashion_portrait:"스트리트 패션 인물", streetwear_lookbook:"스트리트웨어 룩북"});
  const facetLabel = (field,value) => value === "all" ? (field === "ages" ? "전 연령" : field === "genders" ? "성별 무관" : label(value)) : label(value);
  const chartSpecs = [{id:"content", field:"content_type", title:"콘텐츠 유형"}, {id:"ages", field:"ages", title:"추천 연령"}, {id:"genders", field:"genders", title:"추천 성별"}, {id:"hook", field:"hook_strength", title:"Hook 강도"}, {id:"appeals", field:"appeals", title:"소구 유형"}, {id:"duration", field:"duration", title:"장면 기준 길이"}];
  const ranges = ["0–15초 미만", "15–30초 미만", "30–60초 미만", "60–120초 미만", "120초 이상", "미상"];
  const initialFilter = () => ({q:"", type:"*", gender:"*", korean:"all", dimension:"", value:""});
  let snapshot = null, filter = initialFilter(), page = 1, busy = false, revision = 0, searchTimer, shortcodeTimer;
  let shortcodeSearch="";
  const cache = (operation, value) => window.CollectorWorkspaceCache.action("collector-monitor-analysis", operation, value);
  const node = (tag, text, cls="") => { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; item.className = cls; return item; };
  const when = value => Number.isFinite(Date.parse(value)) ? date.format(new Date(value)) : "시각 미상";
  const durationKey = row => row.duration === null ? "5" : row.duration < 15 ? "0" : row.duration < 30 ? "1" : row.duration < 60 ? "2" : row.duration < 120 ? "3" : "4";
  function values(row, field) { return field === "duration" ? [durationKey(row)] : Array.isArray(row[field]) ? row[field] : [row[field]]; }
  const metrics={view_count:"조회수",like_count:"좋아요",comment_count:"댓글",share_count:"공유",repost_count:"재게시",saved_count:"저장",follower_count:"팔로워"};
  const metricLabels={...metrics,video_duration_seconds:"영상 길이(초)",scene_count:"분석 장면 수"};
  const boardState={columns:3,chartWidth:null,chartHeight:420,charts:[
    ...Object.keys(metrics).filter(key=>key!=="follower_count").map(key=>({id:"followers-"+key,x:"follower_count",y:key})),
    ...Object.keys(metrics).filter(key=>key!=="view_count").map(key=>({id:"views-"+key,x:"view_count",y:key})),
    {id:"duration",x:"video_duration_seconds",y:"view_count"},{id:"duration-likes",x:"video_duration_seconds",y:"like_count"}]};
  const metricBoard=window.CollectorChartBoard({state:boardState,chartLabels:metricLabels,render,saveWorkspace:persist,
    draw:(host,rows,x,y,height)=>window.CollectorScatter(host,rows,x,y,metricLabels[x],metricLabels[y],row=>row.reel_id,false,"",height)});
  function validSnapshot(value) {
    const strings = ["reel_id", "summary", "analyzed_at", "content_type", "hook_strength", "hook", "body", "cut_speed"];
    const objects = (items, keys, max) => Array.isArray(items) && items.length <= max && items.every(item => item && keys.every(key => typeof item[key] === "string"));
    return value?.version === 1 && value.source === source && Number.isFinite(Date.parse(value.loaded_at)) && Array.isArray(value.rows) && value.rows.length <= 10000
      && Array.isArray(value.warnings) && value.warnings.every(item => typeof item === "string")
      && value.rows.every(row => row && strings.every(key => typeof row[key] === "string") && /^[A-Za-z0-9_-]{5,80}$/.test(row.reel_id)
        && (row.korean === undefined || row.korean === null || typeof row.korean === "boolean")
        && Object.keys(metrics).every(key=>row[key]===undefined || row[key]===null || Number.isFinite(row[key]) && row[key]>=0)
        && (row.video_duration_seconds===undefined || row.video_duration_seconds===null || Number.isFinite(row.video_duration_seconds) && row.video_duration_seconds>0)
        && ["ages","genders","appeals"].every(key => Array.isArray(row[key]) && row[key].length <= 30 && row[key].every(item => typeof item === "string"))
        && (row.duration === null || (Number.isFinite(row.duration) && row.duration > 0))
        && objects(row.audience, ["age","gender","evidence"], 20) && objects(row.points, ["point","type","evidence"], 30)
        && objects(row.scenes, ["description"], 100) && Number.isSafeInteger(row.scene_count) && row.scene_count >= row.scenes.length
        && row.scenes.every(scene => [scene.start, scene.end].every(value => value === null || (Number.isFinite(value) && value >= 0))));
  }
  async function persist() {
    if (!snapshot) return;
    const current = ++revision;
    try {
      await cache("put", {version:1, snapshot, filter:{...filter}, page, shortcodeSearch, metricBoard:metricBoard.snapshot(), savedAt:new Date().toISOString()});
      if (current === revision) $("analysis-cache").textContent = "분석 스냅샷과 필터를 자동 저장했습니다. 다음 방문 시 복원됩니다.";
    } catch {
      if (current === revision) $("analysis-cache").textContent = "브라우저에 저장하지 못했습니다. 현재 분석 화면은 유지됩니다. 저장 공간을 확인하세요.";
    }
  }
  function options(select, field, title) {
    select.replaceChildren();
    for (const [value, text] of [["*",title], ...[...new Set(snapshot.rows.flatMap(row => values(row,field)))].sort().map(value => [value,facetLabel(field,value)])]) {
      const option = node("option", text); option.value = value; select.append(option);
    }
  }
  function setSnapshot(data, cached=false) {
    snapshot = cached ? {...data, rows:window.CollectorKorean.restore(data.rows)} : data;
    // Old browser snapshots predate server-side canonicalization.
    const canonicalType = value => {
      const key = value.trim().toLowerCase().replace(/[\s_-]+/g, "_");
      return ["lookbook", "fashion_lookbook", "daily_lookbook", "outfit_lookbook", "streetwear_lookbook",
        "룩북", "패션_룩북", "패션룩북", "데일리_룩북", "데일리룩북", "착장_룩북", "착장룩북",
        "스트리트웨어_룩북", "스트리트웨어룩북"].includes(key) ? "lookbook" : value;
    };
    snapshot = {...snapshot, rows:snapshot.rows.map(row => ({...row,
      content_type_raw:row.content_type_raw || row.content_type,
      content_type:canonicalType(row.content_type)
    }))};
    filter.type = canonicalType(filter.type);
    if (filter.dimension === "content_type") filter.value = canonicalType(filter.value);
    options($("analysis-type"), "content_type", "전체 유형"); options($("analysis-gender"), "genders", "전체 성별");
    for (const [id,key] of [["analysis-type","type"],["analysis-gender","gender"]]) {
      if (![...$(id).options].some(item => item.value === filter[key])) filter[key] = "*";
      $(id).value = filter[key];
    }
    $("analysis-search").value = filter.q;
    $("analysis-shortcode").value = shortcodeSearch;
    if (!["all","true","false","unknown"].includes(filter.korean)) filter.korean = "all";
    $("analysis-korean").value = filter.korean;
    const hasUnclassified = data.rows.some(row => window.CollectorKorean.value(row) === null);
    $("analysis-korean-note").textContent = `미확인은 -로 표시합니다.${hasUnclassified ? " · 저장된 korean 값으로 한국·외국을 구분합니다. 최신 분석 불러오기로 갱신하세요." : ""}`;
    $("source-status").textContent = `${cached ? "캐시 스냅샷" : "최신 파일에서 불러옴"} · ${when(data.loaded_at)} · ${fmt.format(data.rows.length)}개 분석 릴스${data.warnings.length ? " · " + data.warnings.join(" · ") : ""}`;
    $("analysis-empty").hidden = true; $("analysis-results").hidden = false; render();
  }
  function selectedRows() {
    const q = filter.q.trim().toLocaleLowerCase();
    return snapshot.rows.filter(row => (filter.type === "*" || row.content_type === filter.type)
      && (filter.gender === "*" || row.genders.includes(filter.gender))
      && window.CollectorKorean.matches(row,filter.korean)
      && (!filter.dimension || values(row,filter.dimension).includes(filter.value))
      && (!q || [row.reel_id,row.summary,row.hook,row.body,...row.points.map(point => point.point)].join(" ").toLocaleLowerCase().includes(q)));
  }
  function chart(spec, rows) {
    if (spec.field === "duration") {

      return;
    }
    const target = $("chart-"+spec.id), counts = new Map();
    for (const row of rows) for (const value of new Set(values(row,spec.field))) counts.set(value,(counts.get(value)||0)+1);
    const entries = [...counts].sort((a,b) => spec.field === "duration" ? Number(a[0])-Number(b[0]) : b[1]-a[1] || a[0].localeCompare(b[0])).slice(0,10);
    target.replaceChildren();
    if (!rows.length) { target.append(node("p", "조건에 맞는 분석이 없습니다.", "chart-empty")); return; }
    const list = node("div",undefined,"analysis-bars");
    for (const [value,count] of entries) {
      const name = spec.field === "duration" ? ranges[Number(value)] : facetLabel(spec.field,value), percent = count/rows.length*100;
      const button = node("button",undefined,"analysis-bar"); button.type="button";
      button.setAttribute("aria-label", `${spec.title} ${name}, ${count}건, ${fmt.format(percent)}퍼센트, 해당 릴스 보기`);
      button.setAttribute("aria-pressed", String(filter.dimension === spec.field && filter.value === value));
      const heading = node("span",undefined,"analysis-bar-label"); heading.append(node("span",name),node("strong",`${fmt.format(count)}건 · ${fmt.format(percent)}%`));
      const track = node("span",undefined,"analysis-track"), fill = node("span",undefined,"analysis-fill"); fill.style.width=percent+"%"; track.setAttribute("aria-hidden","true"); track.append(fill); button.append(heading,track);
      button.dataset.value=value;
      button.addEventListener("click", () => { const same = filter.dimension === spec.field && filter.value === value; filter.dimension = same ? "" : spec.field; filter.value = same ? "" : value; changed(); [...target.querySelectorAll("button")].find(item=>item.dataset.value===value)?.focus(); }); list.append(button);
    }
    target.append(list);
  }
  function reelLink(row) { const link = node("a",row.reel_id,"reel-link"); link.href=`https://www.instagram.com/reel/${row.reel_id}/`; link.target="_blank"; link.rel="noopener noreferrer"; return link; }
  function detail(row) {
    $("detail-title").textContent = row.reel_id + " · 분석 근거";
    const target = $("analysis-detail"); target.replaceChildren(reelLink(row), node("p",`릴스 구분 ${window.CollectorKorean.label(row)} · 분석 시각 ${when(row.analyzed_at)} · AI 분석의 원문이며 검증된 성과·시청자 통계가 아닙니다.`,"analysis-meta"));
    const section = (title, text) => { const part = node("section"); part.append(node("h3",title),node("p",text||"분석 내용 없음")); target.append(part); return part; };
    section("분석 요약",row.summary); section(`Hook · ${label(row.hook_strength)}`,row.hook); section("영상 전개",row.body);
    section("추천 타겟과 근거",row.audience.map(item => `${facetLabel("ages",item.age)} · ${facetLabel("genders",item.gender)}\n${item.evidence}`).join("\n\n"));
    section("핵심 소구점과 근거",row.points.map(item => `${label(item.type)} · ${item.point}\n${item.evidence}`).join("\n\n"));
    const scenes = node("section"); scenes.append(node("h3",`장면 구성 · ${row.scene_count}개 · 편집 속도 ${label(row.cut_speed)}`));
    if (row.scene_count > row.scenes.length) scenes.append(node("p", `상세 내용은 앞 ${row.scenes.length}개 장면을 표시합니다. 건수와 근사 길이는 전체 장면 기준입니다.`));
    for (const scene of row.scenes) { const item = node("div",undefined,"analysis-scene"); item.append(node("strong",`${scene.start === null ? "—" : fmt.format(scene.start)}–${scene.end === null ? "—" : fmt.format(scene.end)}초`),node("p",scene.description)); scenes.append(item); }
    if (!row.scenes.length) scenes.append(node("p","장면 분석 없음")); target.append(scenes);
    $("analysis-dialog").showModal(); $("detail-close").focus();
  }
  function render() {
    if (!snapshot) return;
    const rows = selectedRows(), durations = rows.map(row=>row.duration).filter(value=>value!==null).sort((a,b)=>a-b), middle=Math.floor(durations.length/2);
    $("analysis-count").textContent=fmt.format(rows.length); $("analysis-total").textContent=`전체 ${fmt.format(snapshot.rows.length)}개 중 · 릴스 ID 중복 제외`;
    $("scene-count").textContent=fmt.format(rows.reduce((total,row)=>total+row.scene_count,0));
    $("analysis-duration").textContent=durations.length ? fmt.format(durations.length%2 ? durations[middle] : (durations[middle-1]+durations[middle])/2)+"초" : "—";
    $("duration-coverage").textContent=`${fmt.format(durations.length)}/${fmt.format(rows.length)}개 확인 · 장면 종료 기준 근사값`;
    $("appeal-count").textContent=fmt.format(rows.filter(row=>row.points.length).length);
    const spec = chartSpecs.find(item=>item.field===filter.dimension);
    $("analysis-selection").textContent=`${fmt.format(rows.length)}개 릴스 선택 · 릴스 구분 ${$("analysis-korean").selectedOptions[0].textContent}${spec ? ` · ${spec.title}: ${filter.dimension==="duration" ? ranges[Number(filter.value)] : facetLabel(filter.dimension,filter.value)}` : " · 전체 분류"}`;
    $("clear-drill").hidden=!filter.dimension;
    for (const spec of chartSpecs) chart(spec,rows);
    metricBoard.render(rows);
    renderTable(rows);
  }
  function renderTable(rows=selectedRows()) {
    const query=shortcodeSearch.trim().toLowerCase();
    const tableRows=rows.filter(row=>!query || row.reel_id.toLowerCase().includes(query));
    const pages=Math.max(1,Math.ceil(tableRows.length/20)); page=Math.max(1,Math.min(page,pages)); const start=(page-1)*20;
    $("analysis-rows").replaceChildren(...tableRows.slice(start,start+20).map(row=>{
      const tr=node("tr"), first=node("td"); first.append(reelLink(row),node("span",row.summary,"analysis-summary-text"));
      const target=node("td"); target.append(node("span",`${window.CollectorKorean.label(row)} · ${label(row.content_type)}`),node("p",row.ages.map(value=>facetLabel("ages",value)).join(" · ")+" / "+row.genders.map(value=>facetLabel("genders",value)).join(" · "),"analysis-meta"));
      const length=node("td",`${row.scene_count}개 장면`); length.append(node("p",row.duration===null ? "길이 미상" : `${fmt.format(row.duration)}초 (근사값)`,"analysis-meta"));
      const action=node("td"), button=node("button","근거 보기","button secondary compact"); button.type="button"; button.setAttribute("aria-label",row.reel_id+" 분석 근거 보기"); button.addEventListener("click",()=>detail(row)); action.append(button); tr.append(first,target,length,action); return tr;
    }));
    if (!tableRows.length) { const tr=node("tr"), td=node("td","조건에 맞는 릴스가 없습니다. 필터를 넓혀 보세요."); td.colSpan=4; tr.append(td); $("analysis-rows").append(tr); }
    $("analysis-range").textContent=tableRows.length ? `${start+1}–${Math.min(start+20,tableRows.length)} / ${fmt.format(tableRows.length)}건` : "0건";
    $("analysis-page").textContent=`${page} / ${pages}`; $("analysis-prev").disabled=page===1; $("analysis-next").disabled=page===pages;
  }
  function changed() { page=1; render(); persist(); }
  async function reload() {
    if (busy) return;
    busy=true; $("reload-analysis").disabled=true; $("analysis-error").hidden=true; $("source-status").textContent="최신 분석 파일을 읽고 있습니다. 이전 화면은 계속 사용할 수 있습니다.";
    const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),25000);
    try {
      const response=await fetch("/api/analysis",{cache:"no-store",signal:controller.signal}), data=await response.json();
      if (!response.ok) throw new Error(data.error||"분석 파일을 불러오지 못했습니다.");
      if (!validSnapshot(data)) throw new Error("분석 응답 형식을 확인하지 못했습니다. 모니터를 새로고침하세요.");
      setSnapshot(data); await persist();
    } catch(error) {
      $("analysis-error").textContent=(error.name==="AbortError" ? "분석 파일 읽기 시간이 초과됐습니다." : error.message)+" 이전 스냅샷은 유지됩니다."; $("analysis-error").hidden=false;
      $("source-status").textContent=snapshot ? `${when(snapshot.loaded_at)}에 불러온 이전 스냅샷입니다. 최신 파일 갱신에 실패했습니다.` : "분석 파일을 확인한 뒤 최신 분석 불러오기를 눌러 주세요.";
      if (!snapshot) { $("analysis-empty").querySelector("h2").textContent="분석 파일을 불러오지 못했습니다"; }
    } finally { clearTimeout(timeout); busy=false; $("reload-analysis").disabled=false; }
  }
  function shortcodeChanged() {
    shortcodeSearch=$("analysis-shortcode").value.slice(0,80); page=1; renderTable(); persist();
  }
  $("analysis-shortcode").addEventListener("input",event=>{if(event.isComposing)return;clearTimeout(shortcodeTimer);shortcodeTimer=setTimeout(shortcodeChanged,300);});
  $("analysis-shortcode").addEventListener("compositionend",()=>{clearTimeout(shortcodeTimer);shortcodeTimer=setTimeout(shortcodeChanged,300);});
  $("reload-analysis").addEventListener("click",reload);
  $("analysis-search").addEventListener("input",event=>{ if(event.isComposing)return; clearTimeout(searchTimer); searchTimer=setTimeout(()=>{filter.q=$("analysis-search").value.slice(0,200);changed();},300); });
  $("analysis-search").addEventListener("compositionend",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{filter.q=$("analysis-search").value.slice(0,200);changed();},300);});
  $("analysis-type").addEventListener("change",()=>{filter.type=$("analysis-type").value;changed();}); $("analysis-gender").addEventListener("change",()=>{filter.gender=$("analysis-gender").value;changed();});
  $("analysis-korean").addEventListener("change",()=>{filter.korean=$("analysis-korean").value;changed();});
  $("reset-analysis").addEventListener("click",()=>{clearTimeout(searchTimer);clearTimeout(shortcodeTimer);shortcodeSearch="";$("analysis-shortcode").value="";filter=initialFilter();$("analysis-search").value="";$("analysis-type").value=$("analysis-gender").value="*";$("analysis-korean").value="all";changed();});
  $("clear-drill").addEventListener("click",()=>{filter.dimension=filter.value="";changed();});
  $("analysis-prev").addEventListener("click",()=>{page--;renderTable();persist();}); $("analysis-next").addEventListener("click",()=>{page++;renderTable();persist();});
  $("detail-close").addEventListener("click",()=>$("analysis-dialog").close());
  async function initialize() {
    try {
      const saved=await cache("get");
      if(saved?.version===1 && validSnapshot(saved.snapshot)) {
        if(saved.filter && ["q","type","gender","dimension","value"].every(key=>typeof saved.filter[key]==="string" && saved.filter[key].length<=200)) filter={...saved.filter,korean:["all","true","false","unknown"].includes(saved.filter.korean)?saved.filter.korean:"all"};
        if(!chartSpecs.some(spec=>spec.field===filter.dimension) || (filter.dimension==="duration" && !/^[0-5]$/.test(filter.value))) filter.dimension=filter.value="";
        shortcodeSearch=typeof saved.shortcodeSearch==="string"?saved.shortcodeSearch.slice(0,80):"";
        metricBoard.restore(saved.metricBoard || {});
        page=Number.isSafeInteger(saved.page)?saved.page:1; setSnapshot(saved.snapshot,true);
        $("analysis-cache").textContent="이전 분석과 필터를 캐시에서 복원했습니다. 최신 내용은 불러오기 버튼으로 갱신하세요.";
      }
    } catch { $("analysis-cache").textContent="이전 캐시를 읽지 못했습니다. 원본 분석 파일을 불러옵니다."; }
    $("reload-analysis").disabled=false;
    if(!snapshot || snapshot.rows.some(row => [...Object.keys(metrics),"video_duration_seconds"].some(key=>row[key]===undefined))) await reload();
  }
  let resizeTimer; window.addEventListener("resize",()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(render,150);});
  initialize();
})();
