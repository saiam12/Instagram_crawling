"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const number = new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 1});
  const compact = new Intl.NumberFormat("ko-KR", {notation: "compact", maximumFractionDigits: 1});
  const dates = new Intl.DateTimeFormat("ko-KR", {timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false});
  const shortDates = new Intl.DateTimeFormat("ko-KR", {timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false});
  const metrics = {view_count: "조회수", like_count: "좋아요", comment_count: "댓글", share_count: "공유", repost_count: "재게시", saved_count: "저장", follower_count: "팔로워"};
  const token = document.querySelector('meta[name="monitor-token"]').content;
  const state = {datasets: [], filename: "", warnings: [], sequence: 0, controller: null, cacheRevision: 0, hasCache: false, publisherQuery: ""};
  const initialCharts = () => [
    ...Object.keys(metrics).filter(key => key !== "follower_count").map(key => ({id: "followers-" + key, x: "follower_count", y: key})),
    ...Object.keys(metrics).filter(key => key !== "view_count").map(key => ({id: "views-" + key, x: "view_count", y: key})),
    {id: "duration", x: "video_duration_seconds", y: "view_count"}
  ];
  state.charts = initialCharts();
  const {defaultChartHeight,minChartHeight,maxChartHeight,minChartWidth,maxChartWidth} = window.CollectorChartBoard.limits;
  state.chartHeight = defaultChartHeight;
  state.chartWidth = null;
  state.columns = 3;
  const chartLabels = {...metrics, video_duration_seconds: "영상 길이(초)"};
  const grid = $("comparison-grid");

  const board = window.CollectorChartBoard({state,chartLabels,render,saveWorkspace,
    draw:(host,rows,x,y,height)=>window.CollectorScatter(host,rows,x,y,chartLabels[x],chartLabels[y],reelLabel,$("highlight-korean").checked,state.publisherQuery,height)});
  const renderChartBoard=board.render, applyChartWidth=board.applyWidth;
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const fmt = value => value === null || value === undefined ? "—" : number.format(value);
  const time = row => Date.parse(row.collected_at) || 0;
  const date = value => value && Number.isFinite(Date.parse(value)) ? dates.format(new Date(value)) : "시각 없음";
  const cacheAction = (action, value) => window.CollectorWorkspaceCache.action("collector-monitor-visualization", action, value);

  function cacheNotice(message, failed = false) {
    $("cache-status").textContent = message;
    $("cache-status").classList.toggle("cache-error", failed);
  }

  async function saveWorkspace() {
    if (!state.datasets.length) return;
    const revision = ++state.cacheRevision;
    const workspace = {
      version: 1, datasets: state.datasets, filename: state.filename, warnings: state.warnings, charts: state.charts, columns:state.columns, chartHeight:state.chartHeight, chartWidth:state.chartWidth,
      savedAt: new Date().toISOString(), publisherQuery: state.publisherQuery, highlightKorean: $("highlight-korean").checked,
      selection: Object.fromEntries(["sheet-select", "visual-round", "visual-korean", "visual-scope", "time-bucket"].map(id => [id, $(id).value]))
    };
    cacheNotice("현재 파일과 필터를 이 브라우저에 저장하고 있습니다…");
    try {
      await cacheAction("put", workspace);
      if (revision !== state.cacheRevision) return;
      state.hasCache = true;
      cacheNotice(`${workspace.filename} · ${date(workspace.savedAt)} 자동 저장 · 다음 방문 시 복원됩니다.`);
    } catch {
      if (revision !== state.cacheRevision) return;
      cacheNotice("브라우저에 작업을 저장하지 못했습니다. 저장 공간·브라우저 설정을 확인하세요. 현재 그래프는 계속 사용할 수 있습니다.", true);
    } finally {
      $("delete-cache").disabled = !state.hasCache;
    }
  }

  function validWorkspace(value) {
    if (value?.version !== 1 || typeof value.filename !== "string" || !Array.isArray(value.datasets) || !value.datasets.length || value.datasets.length > 20) return false;
    let count = 0;
    for (const sheet of value.datasets) {
      if (!sheet || typeof sheet.name !== "string" || !Array.isArray(sheet.rows)) return false;
      count += sheet.rows.length;
      if (count > 100000) return false;
      if (!sheet.rows.every(row => row && typeof row.url === "string" && typeof row.collected_at === "string"
          && (row.collection_number === null || Number.isSafeInteger(row.collection_number))
          && (row.korean === undefined || row.korean === null || typeof row.korean === "boolean")
          && Object.keys(metrics).every(key => row[key] === null || (Number.isFinite(row[key]) && row[key] >= 0)))) return false;
    }
    return count > 0;
  }

  async function restoreWorkspace() {
    const sequence = state.sequence;
    cacheNotice("이전에 저장한 작업을 확인하고 있습니다…");
    try {
      const saved = await cacheAction("get");
      if (sequence !== state.sequence || state.datasets.length) return;
      if (!saved) {cacheNotice("파일과 시트·회차·지표 설정을 이 브라우저에 자동 저장합니다."); return;}
      state.hasCache = true;
      $("delete-cache").disabled = false;
      if (!validWorkspace(saved)) throw new Error("cache format");
      state.datasets = saved.datasets.map(sheet => ({...sheet, rows:window.CollectorKorean.restore(sheet.rows)}));
      state.filename = saved.filename;
      state.chartHeight = Number.isFinite(saved.chartHeight) ? Math.min(maxChartHeight, Math.max(minChartHeight, saved.chartHeight)) : defaultChartHeight;
      grid.style.setProperty("--chart-height", `${state.chartHeight}px`);
      state.chartWidth = Number.isFinite(saved.chartWidth) ? Math.min(maxChartWidth, Math.max(minChartWidth, saved.chartWidth)) : null;
      state.columns = [2,3,4].includes(saved.columns) ? saved.columns : 3;
      applyChartWidth();
      if (Array.isArray(saved.charts)) {
        const ids = new Set();
        const charts = saved.charts.filter(chart => chart && typeof chart.id === "string" && !ids.has(chart.id) && chart.id !== "builder" && Object.hasOwn(chartLabels, chart.x) && Object.hasOwn(chartLabels, chart.y) && ids.add(chart.id));
        state.charts = charts.map(({id,x,y}) => ({id,x,y}));
      }
      state.warnings = Array.isArray(saved.warnings) ? saved.warnings : [];
      const selection = saved.selection || {};
      state.publisherQuery = typeof saved.publisherQuery === "string" ? saved.publisherQuery.slice(0,100) : "";
      $("publisher-search").value = state.publisherQuery;
      $("highlight-korean").checked = saved.highlightKorean === true;
      options($("sheet-select"), state.datasets.map((item, i) => [String(i), `${item.name} · ${fmt(item.rows.length)}건`]), selection["sheet-select"]);
      sheetChanged(selection);
      for (const id of ["visual-korean", "visual-scope", "time-bucket"]) {
        if ([...$(id).options].some(option => option.value === selection[id])) $(id).value = selection[id];
      }
      render();
      $("visual-empty").hidden = true;
      $("visual-results").hidden = false;
      render();
      $("clear-file").disabled = false;
      $("file-status").textContent = `${saved.filename} · 이전 작업을 캐시에서 복원했습니다. 최신 파일을 읽으려면 다시 선택하세요.`;
      cacheNotice(`${date(saved.savedAt)} 저장된 작업 복원 · 파일과 필터 설정을 유지했습니다.`);
      if (state.warnings.length) error(`이전에 제외된 시트: ${state.warnings.join(" / ")}`);
    } catch {
      if (sequence === state.sequence) cacheNotice("이전 작업을 복원하지 못했습니다. 파일을 다시 선택하거나 저장된 작업을 삭제하세요.", true);
    }
  }

  function options(select, entries, chosen) {
    select.replaceChildren(...entries.map(([value, label]) => {
      const node = el("option", "", label);
      node.value = value;
      return node;
    }));
    select.value = entries.some(([value]) => value === chosen) ? chosen : entries[0]?.[0] || "";
  }

  function error(message) {
    $("file-error").textContent = message;
    $("file-error").hidden = !message;
  }

  function setLoading(active) {
    $("file-zone").setAttribute("aria-busy", String(active));
    $("data-file").disabled = active;
    $("cancel-read").hidden = !active;
    $("clear-file").disabled = !state.datasets.length && !active;
  }

  async function readFile(file) {
    if (!file) return;
    if (!/\.(csv|json|xlsx)$/i.test(file.name) || file.size === 0 || file.size > 20 * 1024 * 1024) {
      error("CSV·JSON·XLSX 중 비어 있지 않은 20 MiB 이하 파일을 선택하세요.");
      $("data-file").value = "";
      return;
    }
    state.controller?.abort();
    const sequence = ++state.sequence;
    const controller = new AbortController();
    state.controller = controller;
    const timeout = setTimeout(() => controller.abort(), 120000);
    setLoading(true);
    error("");
    $("file-status").textContent = `${file.name} · ${fmt(file.size / 1024 / 1024)} MiB · 파일을 읽고 있습니다…`;
    try {
      const response = await fetch("/api/visualize", {
        method: "POST", body: file, signal: controller.signal, cache: "no-store",
        headers: {"Content-Type": "application/octet-stream", "X-Monitor-Token": token, "X-File-Name": encodeURIComponent(file.name)}
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "파일을 읽지 못했습니다.");
      if (sequence !== state.sequence) return;
      state.datasets = data.datasets;
      state.filename = file.name;
      state.warnings = data.warnings;
      options($("sheet-select"), state.datasets.map((item, i) => [String(i), `${item.name} · ${fmt(item.rows.length)}건`]), "0");
      sheetChanged();
      $("visual-empty").hidden = true;
      $("visual-results").hidden = false;
      render();
      $("file-status").textContent = `${file.name} · ${fmt(file.size / 1024 / 1024)} MiB · ${fmt(state.datasets.length)}개 데이터 시트를 읽었습니다.`;
      if (data.warnings.length) error(`일부 시트는 제외했습니다. ${data.warnings.join(" / ")}`);
      saveWorkspace();
    } catch (failure) {
      if (sequence !== state.sequence) return;
      $("data-file").value = "";
      error(failure.name === "AbortError" ? "읽기를 취소했거나 대기 시간이 초과되었습니다. 파일을 다시 선택할 수 있습니다." : failure.message);
      $("file-status").textContent = state.datasets.length ? `${state.filename} · 이전 파일의 결과를 유지하고 있습니다.` : "파일을 다시 선택하세요.";
    } finally {
      clearTimeout(timeout);
      if (sequence === state.sequence) {
        state.controller = null;
        setLoading(false);
      }
    }
  }

  function sheetChanged(selection = {}) {
    const rows = state.datasets[Number($("sheet-select").value)]?.rows || [];
    const rounds = [...new Set(rows.map(row => row.collection_number).filter(value => value !== null))].sort((a, b) => a - b);
    const entries = [["all", "전체 회차"], ...rounds.map(value => [String(value), `${value}차 수집`])];
    if (rows.some(row => row.collection_number === null)) entries.push(["unknown", "회차 미지정"]);
    options($("visual-round"), entries, selection["visual-round"] || "all");
    render();
  }

  function svgNode(tag, attrs = {}, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function barChart(id, entries, title, axisLabel = "건") {
    const host = $(id);
    host.replaceChildren();
    if (!entries.length) {
      host.append(el("p", "chart-empty", "표시할 값이 없습니다. 파일의 컬럼 또는 선택 조건을 확인하세요."));
      return;
    }
    if (entries.length > 96) {
      const rest = entries.slice(95);
      entries = [...entries.slice(0, 95), {label: "기타", detail: `나머지 ${rest.length}개 범주 합계`, value: rest.reduce((sum, item) => sum + item.value, 0)}];
    }
    const width = 680, height = 245, left = 58, right = 16, top = 23, bottom = 48;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const peak = Math.max(1, ...entries.map(item => item.value));
    const tickUnit = 10 ** Math.max(0, Math.floor(Math.log10(peak / 4)));
    const max = Math.ceil(peak / 4 / tickUnit) * tickUnit * 4;
    const svg = svgNode("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${title}. 세로축 ${axisLabel}. 아래 수치 보기에서 값을 확인할 수 있습니다.`});
    svg.append(svgNode("title", {}, title));
    for (let i = 0; i <= 4; i++) {
      const y = top + plotHeight * i / 4;
      svg.append(svgNode("line", {x1: left, y1: y, x2: width - right, y2: y, class: "visual-gridline"}));
      svg.append(svgNode("text", {x: left - 8, y: y + 4, "text-anchor": "end", class: "visual-axis"}, compact.format(max * (1 - i / 4))));
    }
    svg.append(svgNode("text", {x: left, y: 12, class: "visual-axis"}, axisLabel));
    const step = plotWidth / entries.length;
    entries.forEach((item, index) => {
      const barHeight = item.value / max * plotHeight;
      const x = left + index * step;
      const bar = svgNode("rect", {x: x + step * .13, y: top + plotHeight - barHeight, width: Math.max(1, step * .74), height: barHeight, rx: 2, class: "visual-bar"});
      bar.append(svgNode("title", {}, `${item.detail || item.label}: ${fmt(item.value)}${axisLabel}`));
      svg.append(bar);
      if (entries.length <= 12) svg.append(svgNode("text", {x: x + step / 2, y: Math.max(16, top + plotHeight - barHeight - 6), "text-anchor": "middle", class: "visual-value"}, fmt(item.value)));
      const stride = Math.max(1, Math.ceil(entries.length / 6));
      if (index % stride === 0 || index === entries.length - 1) svg.append(svgNode("text", {x: x + step / 2, y: height - 24, "text-anchor": "middle", class: "visual-axis"}, item.label));
    });
    host.append(svg);
    const details = el("details", "chart-values");
    details.append(el("summary", "", "수치 보기"));
    const list = el("ul");
    for (const item of entries) list.append(el("li", "", `${item.detail || item.label}: ${fmt(item.value)}${axisLabel}`));
    details.append(list);
    host.append(details);
  }

  function timeline(rows) {
    const times = rows.map(time).filter(value => value > 0);
    if (!times.length) {
      $("timeline-note").textContent = "읽을 수 있는 수집 시각이 없습니다. collected_at 컬럼을 확인하세요.";
      return [];
    }
    let min = times[0], max = min;
    for (const value of times) {min = Math.min(min, value); max = Math.max(max, value);}
    const daily = $("time-bucket").value === "day";
    const unit = daily ? 86400000 : 3600000;
    const multiplier = Math.max(1, Math.ceil((max - min) / unit / 72));
    const size = unit * multiplier, kst = 9 * 3600000;
    const start = Math.floor((min + kst) / size) * size - kst;
    const entries = Array.from({length: Math.floor((max - start) / size) + 1}, (_, index) => {
      const from = start + index * size;
      return {label: daily ? shortDates.format(new Date(from)).split(" ").slice(0, 2).join(" ") : shortDates.format(new Date(from)), detail: `${dates.format(new Date(from))} ~ ${dates.format(new Date(from + size))} 미만`, value: 0};
    });
    for (const value of times) entries[Math.floor((value - start) / size)].value++;
    $("timeline-note").textContent = `한국 시간 · ${multiplier}${daily ? "일" : "시간"} 구간${multiplier > 1 ? " (전체 기간에 맞춰 자동 조정)" : ""} · 시각 누락 ${fmt(rows.length - times.length)}건 제외`;
    return entries;
  }

  function reelLabel(row) {
    return row.username ? `@${row.username}` : row.title || row.url.split("/").filter(Boolean).at(-1) || "주소 없는 기록";
  }

  function safeLink(row, text) {
    try {
      const url = new URL(row.url);
      if (url.protocol === "https:" && /(^|\.)instagram\.com$/i.test(url.hostname)) {
        const link = el("a", "reel-link", text);
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = row.title || row.url;
        return link;
      }
    } catch {}
    return el("span", "", text);
  }

  function preview(rows) {
    const host = $("visual-rows");
    host.replaceChildren();
    for (const row of [...rows].sort((a, b) => time(b) - time(a)).slice(0, 20)) {
      const tr = el("tr"), identity = el("td");
      identity.append(safeLink(row, row.title || reelLabel(row)), el("small", "preview-author", `${reelLabel(row)} · ${window.CollectorKorean.label(row)}`));
      tr.append(identity, el("td", "", date(row.collected_at)), el("td", "", row.collection_number ? `${row.collection_number}차` : "미지정"), ...Object.keys(metrics).map(key => el("td", "numeric", fmt(row[key]))));
      host.append(tr);
    }
    if (!rows.length) {const tr = el("tr"), td = el("td", "empty", "선택 조건에 해당하는 기록이 없습니다."); td.colSpan = 10; tr.append(td); host.append(tr);}
  }

  function render() {
    const sheet = state.datasets[Number($("sheet-select").value)];
    if (!sheet) return;
    const round = $("visual-round").value;
    const korean = $("visual-korean").value;
    const roundRows = sheet.rows.filter(row => round === "all" || (round === "unknown" ? row.collection_number === null : String(row.collection_number) === round));
    $("clear-publisher-search").hidden = !$("publisher-search").value;
    const filtered = roundRows.filter(row => window.CollectorKorean.matches(row, korean));
    let selected = roundRows;
    if ($("visual-scope").value === "latest") {
      const latest = new Map();
      roundRows.forEach((row, index) => {
        const key = row.url || `missing-url-${index}`;
        const previous = latest.get(key);
        if (!previous || time(row) >= time(previous)) latest.set(key, row);
      });
      selected = [...latest.values()];
    }
    selected = selected.filter(row => window.CollectorKorean.matches(row, korean));
    $("visual-count").textContent = fmt(selected.length);
    $("visual-unique").textContent = `URL 기준 릴스 ${fmt(new Set(selected.filter(row => row.url).map(row => row.url)).size)}개`;
    renderChartBoard(selected);
    $("selection-note").textContent = `${state.filename} / ${sheet.name} · 릴스 구분 ${$("visual-korean").selectedOptions[0].textContent} · 회차·시간 그래프 ${fmt(filtered.length)}건 · 지표 그래프 ${fmt(selected.length)}건 (${$("visual-scope").selectedOptions[0].textContent}) · 0은 실제 값으로 포함`;
    const hasUnclassified = sheet.rows.some(row => window.CollectorKorean.value(row) === null);
    $("korean-note").textContent = `미확인은 -로 표시합니다.${hasUnclassified ? " · 저장된 korean 값으로 한국·외국을 구분합니다. 최신 분류는 원본을 다시 선택하세요." : ""}`;
    const counts = new Map();
    for (const row of filtered) {const key = row.collection_number || 0; counts.set(key, (counts.get(key) || 0) + 1);}
    const rounds = [...counts].sort((a, b) => (a[0] || Infinity) - (b[0] || Infinity)).map(([key, value]) => ({label: key ? `${key}차` : "미지정", value}));
    barChart("round-chart", rounds, "회차별 수집 기록");
    barChart("time-chart", timeline(filtered), "시간대별 수집량");
    preview(selected);
  }

  let publisherTimer;
  function searchPublisher() {
    clearTimeout(publisherTimer);
    state.publisherQuery = $("publisher-search").value.slice(0,100);
    render();
    saveWorkspace();
  }
  $("publisher-search").addEventListener("input", event => {
    $("clear-publisher-search").hidden = !event.target.value;
    clearTimeout(publisherTimer);
    if (!event.isComposing) publisherTimer = setTimeout(searchPublisher, 300);
  });
  $("publisher-search").addEventListener("compositionstart", () => clearTimeout(publisherTimer));
  $("publisher-search").addEventListener("compositionend", () => { clearTimeout(publisherTimer); publisherTimer = setTimeout(searchPublisher,300); });
  $("publisher-search").addEventListener("keydown", event => { if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); searchPublisher(); } });
  $("clear-publisher-search").addEventListener("click", () => { $("publisher-search").value = ""; searchPublisher(); $("publisher-search").focus(); });

  function selectView(name) {
    $("selection-count-card").hidden = name !== "metrics";
    for (const view of ["metrics", "trends"]) {
      const selected = view === name;
      $(view + "-tab").setAttribute("aria-selected", String(selected));
      $(view + "-tab").tabIndex = selected ? 0 : -1;
      $(view + "-panel").hidden = !selected;
    }
    render();
  }
  for (const [index, name] of ["metrics", "trends"].entries()) {
    $(name + "-tab").addEventListener("click", () => selectView(name));
    $(name + "-tab").addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? "metrics" : event.key === "End" ? "trends" : ["metrics", "trends"][1-index];
      selectView(next);
      $(next + "-tab").focus();
    });
  }
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 150); });


  $("data-file").addEventListener("change", event => readFile(event.target.files[0]));
  $("data-file").addEventListener("click", event => {event.target.value = "";});
  $("cancel-read").addEventListener("click", () => state.controller?.abort());
  function closeWorkspace() {
    state.sequence++;
    state.controller?.abort();
    state.controller = null;
    state.datasets = [];
    state.filename = "";
    state.warnings = [];
    $("data-file").value = "";
    $("visual-results").hidden = true;
    $("visual-empty").hidden = false;
    $("file-status").textContent = "파일을 닫았습니다. 저장된 이전 작업은 다음 방문 시 복원됩니다.";
    error("");
    setLoading(false);
    $("data-file").focus();
  }
  $("clear-file").addEventListener("click", () => {saveWorkspace(); closeWorkspace();});
  $("delete-cache").addEventListener("click", async () => {
    const revision = ++state.cacheRevision;
    const sequence = state.sequence;
    $("delete-cache").disabled = true;
    cacheNotice("저장된 작업을 삭제하고 있습니다…");
    try {
      await cacheAction("delete");
      if (revision !== state.cacheRevision) return;
      state.hasCache = false;
      if (sequence === state.sequence) closeWorkspace();
      $("file-status").textContent = "파일을 선택하면 새로운 작업으로 시작합니다.";
      cacheNotice("이 브라우저의 저장된 시각화 작업을 삭제했습니다.");
    } catch {
      if (revision === state.cacheRevision) cacheNotice("저장된 작업을 삭제하지 못했습니다. 브라우저 저장소 설정을 확인하세요.", true);
    } finally {
      $("delete-cache").disabled = !state.hasCache;
    }
  });
  $("sheet-select").addEventListener("change", () => {sheetChanged(); saveWorkspace();});
  for (const id of ["visual-round", "visual-korean", "visual-scope", "time-bucket", "highlight-korean"]) $(id).addEventListener("change", () => {render(); saveWorkspace();});
  $("file-zone").addEventListener("dragover", event => {event.preventDefault(); $("file-zone").classList.add("drag-over");});
  $("file-zone").addEventListener("dragleave", event => {if (!$("file-zone").contains(event.relatedTarget)) $("file-zone").classList.remove("drag-over");});
  $("file-zone").addEventListener("drop", event => {
    event.preventDefault();
    $("file-zone").classList.remove("drag-over");
    if (event.dataTransfer.files.length !== 1) {error("파일은 한 번에 하나씩 선택하세요."); return;}
    readFile(event.dataTransfer.files[0]);
  });
  restoreWorkspace();
})();
