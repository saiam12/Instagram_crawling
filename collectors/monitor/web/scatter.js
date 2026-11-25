"use strict";
// Shared numeric chart for analysis and uploaded collection records.
(() => {
let selectedReel = null;
const reelKey = row => row.reel_id || /\/reels?\/([A-Za-z0-9_-]+)/.exec(row.url || "")?.[1] || "";
function selectReel(key) {
  selectedReel = selectedReel === key ? null : key;
  for (const dot of document.querySelectorAll(".visual-dot[data-reel-id]")) {
    const selected = dot.getAttribute("data-reel-id") === selectedReel;
    dot.classList.toggle("is-selected", selected);
    dot.setAttribute("aria-pressed", String(selected));
    dot.setAttribute("r", selected ? 6 : dot.classList.contains("is-user") ? 5 : 3.5);
    if (selected) dot.parentNode.appendChild(dot);
  }
}
window.CollectorScatter = (host, rows, xKey, yKey, xLabel, yLabel, identity, highlightKorean = false, userQuery = "", chartHeight = null) => {
  const query = userQuery.trim().replace(/^@/, "").toLowerCase();
  const ns = "http://www.w3.org/2000/svg";
  const number = new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 1});
  const compact = new Intl.NumberFormat("ko-KR", {notation: "compact", maximumFractionDigits: 1});
  const make = (tag, attrs = {}, text) => {
    const element = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const minX = xKey === "view_count" ? 10 : 0, minY = yKey === "view_count" ? 10 : 0;
  const points = rows.filter(row => Number.isFinite(row[xKey]) && row[xKey] >= minX && Number.isFinite(row[yKey]) && row[yKey] >= minY);
  host.replaceChildren();
  const note = document.createElement("p");
  note.className = "scatter-count";
  note.textContent = `${number.format(points.length)}개 점`;
  host.append(note);
  if (!points.length) return;
  const width = Math.max(chartHeight === null ? 320 : 220, host.clientWidth || 680), left = 75, top = 30;
  const plotWidth = width - left - 35;
  const height = chartHeight === null ? top + plotWidth * .7 + 65 : Math.max(180, chartHeight - 26);
  const plotHeight = height - top - 65;
  let maxX = Math.max(1, minX), maxY = Math.max(1, minY);
  for (const row of points) { maxX = Math.max(maxX, row[xKey]); maxY = Math.max(maxY, row[yKey]); }
  const timeAxes = ["duration", "video_duration_seconds", "first_product_exposure_seconds", "first_appeal_seconds"];
  const linearX = timeAxes.includes(xKey);
  const linearY = timeAxes.includes(yKey);
  const transformX = value => linearX ? value : Math.log10(1 + value);
  const transformY = value => linearY ? value : Math.log10(1 + value);
  maxX = transformX(maxX) * 1.05; maxY = transformY(maxY) * 1.05;
  const startX = transformX(minX), startY = transformY(minY);
  const xTicks = [];
  if (linearX) {
    const rawStep = maxX / Math.max(2, Math.min(5, Math.floor(plotWidth / 58)));
    const unit = 10 ** Math.floor(Math.log10(rawStep));
    const step = [1,2,5,10].find(value => value * unit >= rawStep) * unit;
    maxX = Math.ceil(maxX / step) * step;
    for (let value = 0; value <= maxX + step / 100; value += step)
      xTicks.push({value, position:value / maxX * plotWidth, major:true, label:true});
  }
  const yTicks = [];
  if (linearY) {
    const rawStep = maxY / Math.max(2, Math.min(5, Math.floor(plotHeight / 27)));
    const unit = 10 ** Math.floor(Math.log10(rawStep));
    const step = [1,2,5,10].find(value => value * unit >= rawStep) * unit;
    maxY = Math.ceil(maxY / step) * step;
    for (let value = 0; value <= maxY + step / 100; value += step)
      yTicks.push({value, position:value / maxY * plotHeight, major:true, label:true});
  }
  const scaleLabel = linearX || linearY ? `X축 ${linearX ? "선형" : "로그"} · Y축 ${linearY ? "선형" : "로그"}` : "양축 로그";
  const svg = make("svg", {viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": `X축 ${xLabel}, Y축 ${yLabel}, ${points.length}개 기록, ${scaleLabel} 산점도`});
  svg.append(make("title", {}, `${xLabel}와 ${yLabel} · 전체 기록`));
  // Natural log ticks, with intermediate labels only where they fit.
  function ticks(minimum, maximum, pixels, spacing) {
    const start = Math.log10(1 + minimum);
    const values = minimum === 0 ? [{value: 0, position: 0, major: true}] : [];
    for (let power = 0; power <= Math.ceil(maximum); power++) {
      for (let multiple = 1; multiple <= 9; multiple++) {
        const value = multiple * 10 ** power, position = (Math.log10(1 + value) - start) / (maximum - start) * pixels;
        if (value >= minimum && position <= pixels) values.push({value, position, major: multiple === 1});
      }
    }
    const labels = [];
    for (const tick of [...values.filter(tick => tick.major), ...values.filter(tick => !tick.major)]) {
      tick.label = labels.every(position => Math.abs(position - tick.position) >= spacing);
      if (tick.label) labels.push(tick.position);
    }
    return values;
  }
  for (const tick of (linearX ? xTicks : ticks(minX, maxX, plotWidth, 58))) {
    const x = left + tick.position;
    if (tick.major) svg.append(make("line", {x1:x, y1:top, x2:x, y2:top + plotHeight, class:"visual-gridline"}));
    svg.append(make("line", {x1:x, y1:top + plotHeight, x2:x, y2:top + plotHeight + (tick.major ? 7 : 4), class:"visual-axis-tick"}));
    if (tick.label) svg.append(make("text", {x, y:top + plotHeight + 24, "text-anchor":"middle", class:"visual-axis"}, linearX ? number.format(tick.value) : compact.format(tick.value)));
  }
  for (const tick of (linearY ? yTicks : ticks(minY, maxY, plotHeight, 27))) {
    const y = top + plotHeight - tick.position;
    if (tick.major) svg.append(make("line", {x1:left, y1:y, x2:left + plotWidth, y2:y, class:"visual-gridline"}));
    svg.append(make("line", {x1:left - (tick.major ? 7 : 4), y1:y, x2:left, y2:y, class:"visual-axis-tick"}));
    if (tick.label) svg.append(make("text", {x:left - 12, y:y + 4, "text-anchor":"end", class:"visual-axis"}, linearY ? number.format(tick.value) : compact.format(tick.value)));
  }
  svg.append(make("text", {x: left, y: 16, class: "visual-axis"}, yLabel));
  svg.append(make("text", {x: left + plotWidth / 2, y: height - 12, "text-anchor": "middle", class: "visual-axis"}, xLabel));
  const highlighted = [], koreanPoints = [], userPoints = [];
  for (const row of points) {
    const korean = highlightKorean && window.CollectorKorean.value(row) === true;
    const user = Boolean(query && (row.username || "").trim().replace(/^@/, "").toLowerCase().includes(query));
    const dotClass = "visual-dot" + (korean ? " is-korean" : "") + (user ? " is-user" : "");
    const text = `${identity(row)}${korean ? " · 한국" : ""} · ${xLabel}: ${number.format(row[xKey])} · ${yLabel}: ${number.format(row[yKey])}`;
    const dot = make("circle", {cx: left + (transformX(row[xKey]) - startX) / (maxX - startX) * plotWidth, cy: top + plotHeight * (1 - (transformY(row[yKey]) - startY) / (maxY - startY)), r: user ? 5 : 3.5, class: dotClass});
    dot.append(make("title", {}, text));
    const key = reelKey(row), selected = Boolean(key && key === selectedReel);
    if (key) {
      dot.setAttribute("data-reel-id", key);
      dot.setAttribute("role", "button");
      dot.setAttribute("tabindex", "0");
      dot.setAttribute("aria-label", text);
      dot.setAttribute("aria-pressed", String(selected));
      dot.addEventListener("click", () => selectReel(key));
      dot.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectReel(key); }
        if (event.key === "Escape" && selectedReel) { event.preventDefault(); selectReel(selectedReel); }
      });
    }
    if (selected) {
      dot.setAttribute("class", dotClass + " is-selected");
      dot.setAttribute("r", "6");
      highlighted.push(dot);
    } else if (user) userPoints.push(dot);
    else if (korean) koreanPoints.push(dot);
    else svg.append(dot);
  }
  svg.append(...koreanPoints, ...userPoints, ...highlighted);
  host.append(svg);
};

})();
