"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const requiredFields = ["product", "target", "purpose"];
  const optionalFields = ["selling_point", "production_constraints"];
  const fields = [...requiredFields, ...optionalFields];
  const countries = {all:"전체",true:"한국",false:"외국",unknown:"미확인"};
  const metricLabels = {view_count:"조회",like_count:"좋아요",comment_count:"댓글",share_count:"공유",repost_count:"재게시",saved_count:"저장",follower_count:"팔로워"};
  const validFollower = value => value === undefined || value === "" || (typeof value === "number" ? Number.isSafeInteger(value) && value >= 0 : typeof value === "string" && /^\d+$/.test(value.replaceAll(",", "").trim()) && Number.isSafeInteger(Number(value.replaceAll(",", "").trim())));
  const labels = {product:"제품", target:"타겟", purpose:"목적", selling_point:"핵심 소구점", production_constraints:"제작 조건", mood:"이전 분위기"};
  const briefSummary = brief => [...Object.keys(labels).filter(id => typeof brief[id] === "string" && brief[id].trim()).map(id => `${labels[id]}: ${brief[id]}`),...(Number.isSafeInteger(brief.follower_count) ? [`내 계정 팔로워: ${brief.follower_count.toLocaleString("ko-KR")}`] : [])].join(" · ");
  const token = document.querySelector('meta[name="monitor-token"]').content;
  let search = null, ready = false, searching = false, generating = false, sequence = 0;
  let requestId = "", jobId = "", timer;
  let lastResult = null, searchFresh = false, restoring = true, hasCache = false, deleting = false, cacheRevision = 0;
  const number = new Intl.NumberFormat("ko-KR");
  const seconds = new Intl.NumberFormat("ko-KR", {maximumFractionDigits:1});
  const date = new Intl.DateTimeFormat("ko-KR", {timeZone:"Asia/Seoul", dateStyle:"short", timeStyle:"short"});
  const cache = (operation, value) => window.CollectorWorkspaceCache.action("collector-monitor-recommendation", operation, value);
  const node = (tag, text, cls = "") => { const value = document.createElement(tag); value.textContent = text; value.className = cls; return value; };
  function feedback(id, message, error = false) { $(id).textContent = message; $(id).classList.toggle("error", error); }
  async function api(path, payload, timeoutMs = 25000) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {method: payload ? "POST" : "GET", cache: "no-store", signal: controller.signal,
        headers: payload ? {"Content-Type": "application/json", "X-Monitor-Token": token} : {}, body: payload ? JSON.stringify(payload) : undefined});
      const data = await response.json();
      if (!response.ok) { const error = new Error(data.error || "요청을 처리하지 못했습니다."); error.status = response.status; throw error; }
      return data;
    } finally { clearTimeout(timeout); }
  }
  function formValues() { return {dataset: $("dataset").value, country:$("reference-country").value, follower_count:$("follower_count").value.trim(), ...Object.fromEntries(fields.map(id => [id, $(id).value]))}; }
  async function persist() {
    if (restoring || deleting) return false;
    const revision = ++cacheRevision;
    const workspace = {version:1, savedAt:new Date().toISOString(), form:formValues(), search, result:lastResult, jobId};
    feedback("cache-status", "현재 입력·참고 사례·기획안을 이 브라우저에 저장하고 있습니다…");
    try {
      await cache("put", workspace);
      if (revision !== cacheRevision) return true;
      hasCache = true; feedback("cache-status", `${date.format(new Date(workspace.savedAt))} 자동 저장 · 다음 방문 시 복원됩니다.`);
      return true;
    } catch {
      if (revision === cacheRevision) feedback("cache-status", "브라우저에 저장하지 못했습니다. 저장 공간·브라우저 설정을 확인하세요. 현재 결과는 유지됩니다.", true);
      return false;
    } finally { buttons(); }
  }
  function validBrief(value, editing = false) {
    return value && requiredFields.every(id => typeof value[id] === "string" && value[id].length <= 200)
      && optionalFields.every(id => value[id] === undefined || (typeof value[id] === "string" && value[id].length <= 200))
      && (editing && typeof value.follower_count === "string" ? value.follower_count.length <= 24 : validFollower(value.follower_count));
  }
  function validRetrieval(value) {
    return value && typeof value.id === "string" && typeof value.dataset === "string" && validBrief(value.brief)
      && (value.country === undefined || Object.hasOwn(countries,value.country))
      && Number.isFinite(value.corpus_count) && Number.isFinite(value.analyzed_count) && Array.isArray(value.references) && value.references.length <= 6
      && value.references.every(ref => ref && /^R[1-6]$/.test(ref.reference_id) && /^[A-Za-z0-9_-]{5,80}$/.test(ref.reel_id) && typeof ref.title === "string" && ref.analysis && !Array.isArray(ref.analysis)
        && Object.values(ref.analysis).every(item => typeof item === "string") && ref.metrics && !Array.isArray(ref.metrics) && Object.entries(ref.metrics).every(([key,value]) => Object.hasOwn(metricLabels,key) && (value === null || Number.isFinite(value) && value >= 0)) && Number.isFinite(ref.score));
  }
  function validResult(value) {
    if (!value || !validRetrieval(value.retrieval) || !validBrief(value.brief)) return false;
    const plan = value.result?.plan;
    if (!plan && value.result?.guide?.kind === "videos") {
      const guide = value.result.guide;
      return ["completed","partial","failed"].includes(guide.state) && Array.isArray(guide.videos) && guide.videos.length <= 6
        && guide.videos.every(video => {
          if (!video || !Number.isInteger(video.index) || video.index < 1 || video.index > 6 || !["format","blend"].includes(video.mode)) return false;
          if (video.state === "failed") return typeof video.message === "string";
          if (video.state !== "completed" || !/^[a-f0-9]{32}\.mp4$/.test(video.filename) || typeof video.title !== "string"
              || !Number.isInteger(video.duration_seconds) || video.duration_seconds < 5 || video.duration_seconds > 30
              || !Array.isArray(video.reference_ids) || !video.reference_ids.every(id => value.retrieval.references.some(ref => ref.reference_id === id))
              || !Array.isArray(video.features) || !video.features.every(s => typeof s === "string")
              || !Array.isArray(video.scenes) || video.scenes.length < 3 || video.scenes.length > 8) return false;
          let cursor = 0;
          for (const scene of video.scenes) {
            if (!scene || scene.start_second !== cursor || !Number.isInteger(scene.end_second) || scene.end_second <= cursor
                || !["shot","camera","action","dialogue","caption"].every(key => typeof scene[key] === "string" && scene[key].length <= 1000)
                || !scene.framing || !video.reference_ids.includes(scene.framing.reference_id) || !Number.isInteger(scene.framing.source_scene) || scene.framing.source_scene < 1) return false;
            cursor = scene.end_second;
          }
          return cursor === video.duration_seconds;
        });
    }
    if (plan?.duration_seconds !== undefined) {
      let cursor = 0;
      if (!Array.isArray(plan.scenes)) return false;
      for (const scene of plan.scenes) {
        if (!scene || !Number.isInteger(scene.start_second) || !Number.isInteger(scene.end_second) || scene.start_second !== cursor || scene.end_second <= cursor) return false;
        cursor = scene.end_second;
      }
      if (cursor !== plan.duration_seconds) return false;
    }
    return plan && ["concept","hook","bgm","caption","rationale"].every(key => typeof plan[key] === "string")
      && ["hashtags","reference_ids","limitations"].every(key => Array.isArray(plan[key]) && plan[key].length <= 20 && plan[key].every(item => typeof item === "string"))
      && plan.reference_ids.every(id => value.retrieval.references.some(ref => ref.reference_id === id))
      && Array.isArray(plan.scenes) && plan.scenes.length >= 3 && plan.scenes.length <= 6 && plan.scenes.every(scene => scene && typeof scene.description === "string" && typeof scene.dialogue === "string")
      && (plan.duration_seconds === undefined || (Number.isInteger(plan.duration_seconds) && plan.duration_seconds >= 5 && plan.duration_seconds <= 180 && typeof plan.duration_reason === "string"));
  }
  async function restoreWorkspace() {
    try {
      const saved = await cache("get");
      if (!saved) { feedback("cache-status", "마지막 입력·참고 사례·기획안을 이 브라우저에 자동 저장합니다."); return null; }
      hasCache = true;
      if (JSON.stringify(saved).length > 16*1024*1024 || saved.version !== 1 || !Number.isFinite(Date.parse(saved.savedAt)) || !validBrief(saved.form,true)
          || (saved.search && !validRetrieval(saved.search)) || (saved.result && !validResult(saved.result)) || (saved.jobId && !/^[a-zA-Z0-9_-]{16,80}$/.test(saved.jobId))) throw new Error("cache format");
      for (const id of fields) $(id).value = saved.form[id] || "";
      search = saved.search; lastResult = saved.result; jobId = saved.jobId || "";
      if (search) renderReferences(search);
      else if (lastResult) renderReferences(lastResult.retrieval);
      if (lastResult) renderPlan(lastResult);
      $("plan-state").textContent = "저장된 작업";
      feedback("cache-status", `${date.format(new Date(saved.savedAt))} 작업을 캐시에서 복원했습니다. 새 생성은 사례를 다시 검색한 뒤 진행하세요.`);
      feedback("generation-status", "저장된 결과를 복원했습니다. 최신 사례는 다시 검색하세요. Gemini 요청은 자동으로 보내지 않습니다.");
      return saved.form;
    } catch {
      feedback("cache-status", "저장된 작업을 복원하지 못했습니다. 새로 검색하거나 저장된 작업을 삭제하세요.", true); return null;
    }
  }
  function buttons() {
    $("search").disabled = !$("dataset").value || searching || generating || restoring;
    $("example").disabled = generating || searching || restoring;
    $("consent").disabled = !searchFresh || !search?.references.length || generating;
    $("generate").disabled = !ready || !searchFresh || !search?.references.length || !$("consent").checked || searching || generating;
    $("generate").textContent = generating ? "가이드 영상·대본 생성 중…" : "가이드 영상·대본 생성";
    $("dataset").disabled = searching || generating || restoring || !$("dataset").value;
    for (const id of fields) $(id).disabled = generating || restoring;
    $("reference-country").disabled = generating || restoring;
    $("follower_count").disabled = generating || restoring;
    $("delete-cache").disabled = !hasCache || searching || generating || restoring || deleting;
  }
  function invalidate() {
    ++sequence; search = null; searchFresh = false; requestId = ""; $("consent").checked = false;
    $("duration-evidence").hidden = true;
    $("reference-count").textContent = "다시 검색";
    $("references").replaceChildren(node("p", "조건이 바뀌었습니다. 사례를 다시 검색하세요.", "rec-empty-small"));
    $("source-note").textContent = "새 조건으로 검색한 사례를 기획안 생성에 사용합니다.";
    $("plan-state").textContent = "조건 변경 · 검색 필요";
    feedback("generation-status", "기존 기획안이 있다면 이전 조건의 결과입니다."); buttons(); persist();
  }
  function validate() {
    let first;
    for (const id of fields) {
      const length = $(id).value.trim().length;
      const valid = length <= 200 && (!requiredFields.includes(id) || length > 0);
      $(id).setAttribute("aria-invalid", String(!valid)); $(id+"-error").hidden = valid;
      $(id+"-error").textContent = requiredFields.includes(id) ? "1~200자로 입력하세요." : "200자 이내로 입력하거나 비워 두세요.";
      if (!valid) { $(id).setAttribute("aria-describedby", id+"-error"); first ||= $(id); }
      else $(id).removeAttribute("aria-describedby");
    }
    const follower=$("follower_count"), followerValid=validFollower(follower.value.trim());
    follower.setAttribute("aria-invalid",String(!followerValid)); $("follower_count-error").hidden=followerValid;
    $("follower_count-error").textContent="0 이상의 정수로 입력하세요.";
    if (!followerValid) { follower.setAttribute("aria-describedby","follower_count-error"); first ||= follower; }
    else follower.removeAttribute("aria-describedby");
    first?.focus(); return !first;
  }
  function renderReferences(data) {
    $("reference-count").textContent = `${data.references.length}건`;
    $("source-note").textContent = `${countries[data.country || "all"]} · ${number.format(data.corpus_count)}개 분석 릴스 · ${data.search_method === "hybrid" ? "Embedding 2 의미 검색 + BM25" : "BM25 단어 검색"}${data.metric_coverage ? ` · 수집 지표 연결 ${data.metric_coverage.collected}/${data.metric_coverage.total}` : ""} · ${data.sources?.[0] || data.dataset}`;
    $("references").replaceChildren(...data.references.map((ref, index) => {
      const article = node("article", "", "rec-reference"); article.id = "ref-"+ref.reference_id;
      const top = node("div", "", "rec-reference-top"); top.append(node("span", ref.reference_id, "rec-ref-id"));
      const link = node("a", ref.reel_id); link.href = `https://www.instagram.com/reel/${ref.reel_id}/`; link.target = "_blank"; link.rel = "noopener noreferrer"; top.append(link, node("span", "분석 자료", "badge info"),node("span", ref.korean === true ? "한국" : ref.korean === false ? "외국" : "미확인", "badge info"));
      const summary = ref.analysis.summary || ref.title || "본문 없음";
      article.append(top, node("p", summary.length > 220 ? summary.slice(0, 220) + "…" : summary));
      const score = data.search_method === "hybrid" ? `통합 순위 ${index+1} · 코사인 유사도 ${ref.cosine_similarity} · 단어 점수 ${ref.bm25_score}` : `단어 점수 ${ref.score}`;
      const metrics=Object.entries(metricLabels).map(([key,label]) => `${label} ${Number.isFinite(ref.metrics[key]) ? number.format(ref.metrics[key]) : ref.private_metrics?.includes(key) ? "비공개" : "미수집"}`).join(" · ");
      article.append(node("small",metrics),node("small",score + (data.brief.follower_count !== undefined ? ` · 계정 규모 점수 ${Number.isFinite(ref.follower_similarity) ? ref.follower_similarity.toFixed(2) : "미수집"}` : "")));
      if (Number.isFinite(Date.parse(ref.collected_at))) article.append(node("small","지표 수집 · "+date.format(new Date(ref.collected_at))));
      if (ref.follower_collected_at !== ref.collected_at && Number.isFinite(Date.parse(ref.follower_collected_at))) article.append(node("small","팔로워 수집 · "+date.format(new Date(ref.follower_collected_at))));
      const duration = ref.video_duration_seconds;
      article.append(node("small", Number.isFinite(duration) && duration > 0 ? `영상 길이 ${seconds.format(duration)}초${ref.duration_source === "analysis_scene_end" ? " · 분석 장면 기준 근사값" : " · 분석 파일 값"}` : "영상 길이 미확인"));
      const details = node("details", ""); details.append(node("summary", "전송할 텍스트·지표 보기"), node("pre", JSON.stringify(ref, null, 2))); article.append(details); return article;
    }));
    if (!data.references.length) $("references").append(node("p", "유사한 사례가 없습니다. 제품이나 핵심 소구점을 더 넓은 표현으로 바꿔 보세요.", "rec-empty-small"));
    $("brief-summary").hidden = false; $("brief-summary").textContent = briefSummary(data.brief);
    const durations = data.references.map(ref => ref.video_duration_seconds).filter(value => Number.isFinite(value) && value > 0).sort((a,b) => a-b);
    $("duration-evidence").hidden = false;
    const middle = Math.floor(durations.length / 2), median = durations.length % 2 ? durations[middle] : (durations[middle-1] + durations[middle]) / 2;
    $("duration-evidence").textContent = durations.length ? `참고 영상 길이 중앙값 ${seconds.format(median)}초 · ${durations.length}/${data.references.length}개 확인 · 길이 추천의 참고값입니다.` : "참고 영상 길이 미확인 · 길이 추천 시 입력 조건에 따른 추정임을 표시합니다.";
  }
  $("brief-form").addEventListener("submit", async event => {
    event.preventDefault(); if (!validate() || searching || generating) return;
    const current = ++sequence; searching = true; searchFresh = false; $("consent").checked = false; buttons();
    feedback("search-status", "의미와 단어를 함께 비교하고 있습니다. 첫 검색은 분석문 임베딩 준비에 시간이 걸릴 수 있습니다.");
    try {
      const brief=Object.fromEntries(fields.map(id => [id, $(id).value.trim()]).filter(([id, value]) => requiredFields.includes(id) || value));
      if ($("follower_count").value.trim()) brief.follower_count=Number($("follower_count").value.replaceAll(",", "").trim());
      const data = await api("/api/recommendation/search", {dataset: $("dataset").value, country:$("reference-country").value, brief}, 190000);
      if (current !== sequence) return;
      search = data; searchFresh = true; requestId = ""; renderReferences(data); persist();
      feedback("search-status", data.warnings.length ? data.warnings.join(" ") : `${data.references.length}개 참고 사례를 찾았습니다.`, !!data.warnings.length);
      $("plan-state").textContent = lastResult ? "이전 기획안 · 새 검색" : data.references.length ? "생성 준비" : "참고 사례 없음";
      feedback("generation-status", lastResult ? "아래 결과는 이전 검색의 결과입니다. 새 사례로 생성하려면 전송 내용을 확인하세요." : "검색 결과의 전송 내용을 확인한 뒤 가이드 영상·대본을 생성하세요.");
    } catch (error) { if (current === sequence) feedback("search-status", error.message || "사례 검색에 실패했습니다.", true); }
    finally { searching = false; buttons(); }
  });
  function renderPlan(data) {
    const plan = data.result.plan; $("plan").replaceChildren(node("h3", plan?.concept || "영상별 가이드 · 장면 구성 · 대본", "rec-concept"));
    $("plan").append(node("p", "생성 조건 · " + briefSummary(data.brief), "rec-help"));
    const section = (heading, content, cls = "") => { const part = node("section", "", "rec-plan-section"); part.append(node("h3", heading), node("p", content, cls)); $("plan").append(part); };
    if (plan) {
    if (Number.isInteger(plan.duration_seconds)) {
      section("추천 영상 길이", `${plan.duration_seconds}초`, "rec-duration");
      section("이 길이를 추천한 이유", plan.duration_reason);
    } else section("추천 영상 길이", "이전 형식의 기획안에는 길이 추천이 없습니다. 새로 생성하면 초 단위로 제안합니다.");
    section("Hook · 첫 3초", plan.hook, "rec-hook");
    const scenes = node("section", "", "rec-plan-section"), list = node("ol", "", "rec-scenes");
    scenes.append(node("h3", "장면 구성 / 대본"));
    for (const scene of plan.scenes) { const item = node("li", ""), content = node("div", ""); if (Number.isFinite(scene.start_second) && Number.isFinite(scene.end_second)) content.append(node("strong", `${scene.start_second}–${scene.end_second}초 · ${scene.end_second-scene.start_second}초`, "rec-scene-time")); content.append(node("p", scene.description), node("small", scene.dialogue)); item.append(content); list.append(item); }
    scenes.append(list); $("plan").append(scenes);
    }
    const guide = data.result.guide;
    if (guide?.kind === "videos" && Array.isArray(guide.videos)) {
      const disclosure = node("details", "", "rec-guide-disclosure"), summary = node("summary", "가이드 영상 6개 펼치기");
      disclosure.append(summary, node("p", guide.message || "Antigravity 촬영 가이드 애니메이션", "rec-guide-warning"));
      disclosure.addEventListener("toggle", () => { summary.textContent = disclosure.open ? "가이드 영상 접기" : "가이드 영상 6개 펼치기"; });
      {
        const group = node("section", "", "rec-video-group");
        const navigation = node("nav", "", "rec-video-nav"); navigation.setAttribute("aria-label", "가이드 영상 바로가기");
        const cards = node("div", "", "rec-video-grid");
        for (const item of guide.videos.slice(0,6)) {
          const card = node("details", "", "rec-video-card"); card.id = `guide-video-${item.index}`; card.tabIndex = -1; card.open = true;
          const title = `${item.index}. ${item.title || "가이드 영상"}`;
          const jump = node("a", title); jump.href = `#${card.id}`; jump.addEventListener("click", () => { card.open = true; }); navigation.append(jump);
          const header = node("summary", "", "rec-video-header"), toggle = node("span", "접기", "rec-video-toggle");
          header.append(node("span", item.mode === "format" ? "참고 형식" : "특징 혼합", "rec-video-mode"), node("h4", title), toggle); card.append(header);
          card.addEventListener("toggle", () => { toggle.textContent = card.open ? "접기" : "펼치기"; if (!card.open) card.querySelector("video")?.pause(); });
          const body = node("div", "", "rec-video-body"), media = node("div", "", "rec-video-media"), contentPanel = node("div", "", "rec-video-content");
          const sources = node("details", "", "rec-video-sources");
          sources.append(node("summary", item.mode === "format" ? "참고한 원본 영상" : "혼합에 사용한 원본 영상"));
          for (const id of (item.reference_ids || []).slice(0,6)) {
            const source = data.retrieval.references.find(ref => ref.reference_id === id);
            if (!source || !/^[A-Za-z0-9_-]{5,80}$/.test(source.reel_id)) continue;
            const link = node("a", `${id} · ${source.reel_id} ↗`);
            link.href = `https://www.instagram.com/reel/${source.reel_id}/`; link.target = "_blank"; link.rel = "noopener noreferrer";
            const description = source.title || source.analysis.summary || "";
            sources.append(link);
            if (description) sources.append(node("p", description.length > 180 ? description.slice(0,180) + "…" : description));
          }
          if (sources.children.length === 1) sources.append(node("p", "저장된 참고 영상 정보가 없습니다."));
          contentPanel.append(sources); body.append(media,contentPanel); card.append(body);
          if (item.state === "completed" && /^[a-f0-9]{32}\.mp4$/.test(item.filename)) {
            const video = document.createElement("video"); video.controls = true; video.preload = "metadata"; video.playsInline = true;
            video.src = "/api/recommendation/video?filename=" + encodeURIComponent(item.filename) + "&v=camera-frustum-1";
            video.setAttribute("aria-label", item.title || "촬영 가이드 애니메이션");
            video.addEventListener("error", () => { if (!card.querySelector(".rec-video-error")) card.append(node("p", "저장된 영상을 불러오지 못했습니다. 서버의 가이드 파일을 확인하세요.", "rec-video-error")); });
            const download = node("a", "영상 저장", "button secondary"); download.href = video.src; download.download = `촬영-가이드-${item.index}.mp4`;
            media.append(node("p", "위: 영상 가이드 · 아래: 3D 카메라 가이드", "rec-help"), video, node("small", `${item.duration_seconds}초 · ${(item.reference_ids || []).join(" · ")}`), download);
            const features = node("ul", ""); for (const feature of (item.features || []).slice(0,8)) features.append(node("li", feature));
            sources.append(features);
            if (Array.isArray(item.scenes) && item.scenes.length) {
              const script = node("details", "", "rec-video-script"), scenes = node("ol", "", "rec-scenes");
              scenes.setAttribute("aria-label", `${item.index}번 영상 장면 구성과 대본`);
              script.open = true;
              script.append(node("summary", "장면 구성 / 대본"));
              for (const scene of item.scenes) {
                const row = node("li", ""), content = node("div", "");
                content.append(node("strong", `${scene.start_second}–${scene.end_second}초`, "rec-scene-time"));
                content.append(node("small", `화면 범위 · ${scene.shot}  /  카메라 · ${scene.camera}`, "rec-scene-camera"));
                content.append(node("p", scene.action));
                content.append(node("p", "대본 · " + (scene.dialogue || "대사 없음"), "rec-scene-dialogue"));
                content.append(node("small", "화면 자막 · " + scene.caption));
                if (scene.framing) {
                  const reference = data.retrieval.references.find(ref => ref.reference_id === scene.framing.reference_id);
                  const evidence = node("small", "참고 영상 · ", "rec-scene-source");
                  if (reference && /^[A-Za-z0-9_-]{5,80}$/.test(reference.reel_id)) {
                    const link = node("a", `${scene.framing.reference_id} · ${reference.reel_id} ↗`);
                    link.href = `https://www.instagram.com/reel/${reference.reel_id}/`; link.target = "_blank"; link.rel = "noopener noreferrer";
                    evidence.append(link, document.createTextNode(` · 원본 장면 ${scene.framing.source_scene}`));
                  } else evidence.append(document.createTextNode(`${scene.framing.reference_id} · 원본 장면 ${scene.framing.source_scene} (원본 링크 정보 없음)`));
                  content.append(evidence);
                }
                row.append(content); scenes.append(row);
              }
              script.append(node("p", "촬영용 대본 · 영상에는 음성이 포함되지 않습니다.", "rec-help"), scenes); contentPanel.prepend(script);
            } else contentPanel.prepend(node("p", "이전 저장 영상에는 영상별 장면 구성·대본이 없습니다. 새로 생성한 결과에서 확인할 수 있습니다.", "rec-help"));
            if (item.warning) card.append(node("p", item.warning, "rec-guide-warning"));
          } else media.append(node("p", item.message || "이 영상을 생성하지 못했습니다.", "rec-video-error"));
          cards.append(card);
        }
        group.append(navigation,cards); disclosure.append(group);
      }
      $("plan").append(disclosure);
    } else if (guide?.state === "completed" && typeof guide.image === "string" && /^data:image\/(?:png|svg\+xml);base64,[A-Za-z0-9+/=]+$/.test(guide.image)) {
      const figure = node("figure", "", "rec-guide"), image = document.createElement("img");
      image.src = guide.image; image.alt = "장면별 촬영 구도, 인물·제품 배치와 동작을 보여주는 가이드 도식";
      if (Number.isSafeInteger(guide.width) && Number.isSafeInteger(guide.height) && guide.width > 0 && guide.height > 0) {
        image.width = guide.width; image.height = guide.height;
      }
      const heading = node("figcaption", "장면별 촬영 가이드 · Antigravity"), download = node("a", "이미지 저장", "button secondary");
      download.href = guide.image; download.download = guide.image.startsWith("data:image/png;") ? "촬영-가이드.png" : "촬영-가이드.svg";
      heading.append(download); figure.append(heading, image);
      if (guide.warning) figure.append(node("p", guide.warning, "rec-guide-warning"));
      const disclosure = node("details", "", "rec-guide-disclosure"), summary = node("summary", "가이드 이미지 펼치기");
      disclosure.addEventListener("toggle", () => { summary.textContent = disclosure.open ? "가이드 이미지 접기" : "가이드 이미지 펼치기"; });
      disclosure.append(summary, figure); $("plan").append(disclosure);
    } else if (guide?.state === "failed") section("촬영 가이드", guide.message);
    if (plan) {
    section("BGM", plan.bgm); section("Caption · Hashtag", plan.caption + "\n" + plan.hashtags.join(" "));
    section("참고 사례에서 가져온 점", plan.rationale);
    const evidence = node("div", "", "rec-evidence");
    for (const id of plan.reference_ids) { const ref = data.retrieval.references.find(item => item.reference_id === id); if (!ref) continue; const link = node("a", id + " 참고 릴스 ↗"); link.href = `https://www.instagram.com/reel/${ref.reel_id}/`; link.target = "_blank"; link.rel = "noopener noreferrer"; evidence.append(link); } $("plan").append(evidence);
    section("적용 전 확인할 점", plan.limitations.join("\n"));
    }
    $("plan").hidden = false; $("plan-empty").hidden = true;
    const guideState = data.result.guide;
    $("plan-state").textContent = guideState?.state === "failed" ? "영상 생성 실패" : guideState?.state === "partial" ? "일부 생성 완료" : "생성 완료";
    $("brief-summary").hidden = false; $("brief-summary").textContent = briefSummary(data.brief);
    feedback("generation-status", guideState?.kind === "videos" ? `${data.result.model} · 가이드 영상 ${guideState.completed ?? guideState.videos.filter(v => v.state === "completed").length}/6개 · 영상별 장면 구성/대본` : `${data.result.model} · 저장된 기획안`, guideState?.state === "failed");
  }
  async function pollJob() {
    if (!jobId) return;
    try {
      const data = await api("/api/recommendation/job?id=" + encodeURIComponent(jobId));
      if (data.state === "running") { timer = setTimeout(pollJob, 2500); return; }
      generating = false; jobId = ""; try { sessionStorage.removeItem("collector-recommend-job"); } catch {}
      if (data.state === "completed") { lastResult = data; renderReferences(data.retrieval); renderPlan(data); requestId = ""; }
      else { $("plan-state").textContent = "생성 실패"; feedback("generation-status", data.error, true); requestId = ""; }
      buttons(); persist();
    } catch (error) {
      if (error.status === 400) {
        generating = false; jobId = ""; requestId = "";
        try { sessionStorage.removeItem("collector-recommend-job"); } catch {}
        $("plan-state").textContent = "작업 확인 필요";
        feedback("generation-status", error.message + " 이전 API 처리 여부가 불확실하면 한도를 확인한 뒤 새로 생성하세요.", true); buttons(); persist(); return;
      }
      // Retry reads only: a lost acknowledgement must not create another paid request.
      feedback("generation-status", "작업 상태를 확인하지 못했습니다. 새 생성 요청 없이 상태만 다시 확인합니다.", true);
      timer = setTimeout(pollJob, 7000);
    }
  }
  $("generate").addEventListener("click", async () => {
    if (!searchFresh || !search || !$("consent").checked || generating) return;
    requestId ||= crypto.randomUUID(); generating = true; buttons(); $("plan-state").textContent = "생성 중";
    jobId = requestId;
    try { sessionStorage.setItem("collector-recommend-job", jobId); } catch {}
    await persist(); // Commit recovery ID before a paid request may be accepted.
    feedback("generation-status", "Antigravity에 가이드 영상 6개와 영상별 장면 구성·대본을 직접 병렬 요청합니다. 화면을 이동해도 서버의 작업은 계속됩니다.");
    try {
      const data = await api("/api/recommendation/generate", {search_id: search.id, request_id: requestId});
      jobId = data.id; try { sessionStorage.setItem("collector-recommend-job", jobId); } catch {} pollJob();
    } catch (error) {
      feedback("generation-status", `${error.message || "연결 오류"} 새로 생성하지 않고 같은 요청 ID의 처리 여부를 확인합니다.`, true);
      pollJob();
    }
  });
  $("consent").addEventListener("change", buttons);
  for (const id of fields) $(id).addEventListener("input", invalidate);
  $("reference-country").addEventListener("change", invalidate);
  $("follower_count").addEventListener("input", invalidate);
  $("dataset").addEventListener("change", invalidate);
  $("example").addEventListener("click", () => { const sample = {product: "화장품 · 수분 크림", target: "20대 여성", purpose: "신제품 인지", selling_point: "끈적임 없이 가벼운 사용감", production_constraints: "얼굴 노출 없이 · 휴대폰으로 실내 촬영"}; for (const id of fields) $(id).value = sample[id]; invalidate(); $("product").focus(); });
  $("delete-cache").addEventListener("click", () => { $("delete-error").textContent = ""; $("delete-dialog").showModal(); $("delete-cancel").focus(); });
  $("delete-cancel").addEventListener("click", () => $("delete-dialog").close());
  $("delete-dialog").addEventListener("cancel", event => { if (deleting) event.preventDefault(); });
  $("delete-confirm").addEventListener("click", async () => {
    if (deleting || generating) return;
    deleting = true; ++cacheRevision; $("delete-confirm").disabled = $("delete-cancel").disabled = true;
    try {
      await cache("delete"); ++sequence; search = lastResult = null; searchFresh = hasCache = false; requestId = jobId = "";
      try { localStorage.removeItem("collector-recommend-brief"); sessionStorage.removeItem("collector-recommend-job"); } catch {}
      for (const id of fields) $(id).value = "";
      $("reference-country").value = "all";
      $("follower_count").value = "";
      $("plan").replaceChildren(); $("plan").hidden = true; $("plan-empty").hidden = false; $("brief-summary").hidden = $("duration-evidence").hidden = true;
      $("references").replaceChildren(node("p", "조건을 입력하면 유사한 릴스를 여기에 표시합니다.", "rec-empty-small"));
      $("source-note").textContent = "지정된 분석 JSON의 레코드만 참고합니다."; $("reference-count").textContent = "검색 전"; $("plan-state").textContent = "사례 검색 대기"; $("consent").checked = false;
      feedback("cache-status", "저장된 추천 작업을 삭제했습니다. 다음 작업부터 다시 자동 저장합니다."); feedback("search-status", "조건을 입력하고 사례를 찾아보세요."); feedback("generation-status", "먼저 유사 사례를 검색하세요."); $("delete-dialog").close();
    } catch { $("delete-error").textContent = "저장된 작업을 삭제하지 못했습니다. 현재 결과는 유지했습니다."; }
    finally { deleting = false; $("delete-confirm").disabled = $("delete-cancel").disabled = false; buttons(); }
  });
  async function initialize() {
    $("consent").checked = false;
    buttons();
    let saved = await restoreWorkspace();
    if (!saved) { try { saved = JSON.parse(localStorage.getItem("collector-recommend-brief") || "{}"); } catch {} }
    saved ||= {};
    let guideRestored = false;
    if (lastResult?.result?.guide?.kind === "videos") {
      const videos = lastResult.result.guide.videos;
      const restored = await Promise.allSettled(videos.map(async item => {
        if (item.state !== "completed" || !/^[a-f0-9]{32}\.mp4$/.test(item.filename)) return item;
        const cached = await api("/api/recommendation/video-plan?filename=" + encodeURIComponent(item.filename));
        return cached.video?.filename === item.filename && Array.isArray(cached.video.scenes) ? cached.video : item;
      }));
      const updated = {...lastResult, result:{...lastResult.result, guide:{...lastResult.result.guide,
        videos:restored.map((item,index) => item.status === "fulfilled" ? item.value : videos[index])}}};
      if (validResult(updated)) {
        lastResult = updated; renderPlan(lastResult); guideRestored = true;
        feedback("generation-status", "저장된 영상별 장면 구성과 대본을 복원했습니다. 생성 API는 호출하지 않았습니다.");
      }
    }
    if (lastResult?.result?.guide?.filename && lastResult.result.guide.cache_version !== 3) {
      try {
        const cached = await api("/api/recommendation/guide?filename=" + encodeURIComponent(lastResult.result.guide.filename));
        if (cached.guide?.state === "completed") {
          lastResult.result.guide = cached.guide; renderPlan(lastResult); guideRestored = true;
          feedback("generation-status", "저장된 이미지 캐시를 복원했습니다. 새 생성 요청은 보내지 않았습니다.");
        }
      } catch { feedback("generation-status", "브라우저에 저장된 이미지를 유지했습니다. 서버 이미지 캐시를 확인하지 못했습니다."); }
    }
    for (const id of fields) if (typeof saved[id] === "string") $(id).value = saved[id].slice(0, 200);
    $("reference-country").value = Object.hasOwn(countries,saved.country) ? saved.country : "all";
    $("follower_count").value = saved.follower_count === undefined ? "" : String(saved.follower_count);
    try {
      const data = await api("/api/recommendation/dataset");
      const option = node("option", data.label); option.value = data.available ? data.id : ""; $("dataset").replaceChildren(option); $("dataset-path").textContent = data.path;
      if ((search && search.dataset !== data.id) || (!search && lastResult && lastResult.retrieval.dataset !== data.id)) {
        invalidate(); $("plan-state").textContent = lastResult ? "이전 자료의 기획안" : "참고 자료 변경";
        feedback("cache-status", "이전 작업은 보존했습니다. 참고 파일이 변경되어 지정 분석 JSON에서 다시 검색해야 합니다.");
      }
      feedback("search-status", data.available ? "지정 분석 파일에서 유사 사례를 검색합니다." : "지정 분석 파일이 없습니다. 표시된 경로를 확인하세요.", !data.available);
    } catch { feedback("search-status", "데이터를 불러오지 못했습니다. 모니터 서버 실행 상태를 확인하고 새로고침하세요.", true); }
    try { jobId = sessionStorage.getItem("collector-recommend-job") || jobId; } catch {}
    if (lastResult?.id === jobId) { jobId = ""; try { sessionStorage.removeItem("collector-recommend-job"); } catch {} }
    if (jobId) { generating = true; $("plan-state").textContent = "이전 요청 확인 중"; }
    restoring = false; buttons();
    if (guideRestored) await persist();
    if (jobId) pollJob();
    try { const config = await api("/api/recommendation/status"); ready = config.ready; feedback("provider-status", config.message || "설정을 확인하세요.", !ready); } catch { feedback("provider-status", "Gemini 설정을 확인하지 못했습니다. 서버 재시작 및 Analyzer 설치 상태를 확인하세요.", true); }
    buttons();
  }
  initialize();
})();
