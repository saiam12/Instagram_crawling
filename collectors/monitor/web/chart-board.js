"use strict";
// Shared metric board: the collection and analysis screens use the same interactions.
window.CollectorChartBoard = ({state, chartLabels, draw, render, saveWorkspace}) => {
  const $ = id => document.getElementById(id);
  const {defaultChartHeight,minChartHeight,maxChartHeight,minChartWidth,maxChartWidth}=window.CollectorChartBoard.limits;
  const grid=$("comparison-grid"), builder=grid.querySelector(".axis-chart-panel"), chartPanels=new Map();
  const el=(tag,className,text) => { const node=document.createElement(tag); node.className=className; if(text!==undefined)node.textContent=text; return node; };
  grid.replaceChildren(builder);
  let draggedChart=null,comparisonRows=[];
  function resizeCharts(height, redraw = true) {
    state.chartHeight = Math.round(Math.min(maxChartHeight, Math.max(minChartHeight, height)));
    grid.style.setProperty("--chart-height", `${state.chartHeight}px`);
    if (!redraw) return;
    for (const chart of state.charts) {
      const panel = chartPanels.get(chart.id);
      if (panel) renderChartPanel(chart, panel, comparisonRows);
    }
  }

  function applyChartWidth() {
    const custom = state.chartWidth !== null;
    grid.dataset.widthMode = custom ? "custom" : "preset";
    if (custom) grid.style.setProperty("--chart-width", `${state.chartWidth}px`);
    grid.style.setProperty("--chart-columns", state.columns);
    $("chart-columns").value = String(state.columns);
  }

  function resizeWidths(width, redraw = true) {
    state.chartWidth = Math.round(Math.min(maxChartWidth, Math.max(minChartWidth, width)));
    applyChartWidth();
    if (!redraw) return;
    for (const chart of state.charts) {
      const panel = chartPanels.get(chart.id);
      if (panel) renderChartPanel(chart, panel, comparisonRows);
    }
  }

  function sizeHandle(panel, axis) {
    const vertical = axis === "height", minimum = vertical ? minChartHeight : minChartWidth, maximum = vertical ? maxChartHeight : maxChartWidth;
    const resize = vertical ? resizeCharts : resizeWidths;
    const currentSize = () => vertical ? state.chartHeight : panel.getBoundingClientRect().width;
    const handle = el("div", `chart-${axis}-handle`);
    handle.tabIndex = 0;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", vertical ? "horizontal" : "vertical");
    handle.setAttribute("aria-valuemin", minimum);
    handle.setAttribute("aria-valuemax", maximum);
    let drag = null, frame = null;
    handle.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      event.preventDefault();
      drag = {position:vertical ? event.clientY : event.clientX, size:currentSize(), next:currentSize()};
      handle.setPointerCapture(event.pointerId);
      handle.focus({preventScroll:true}); panel.classList.add("is-resizing");
    });
    handle.addEventListener("pointermove", event => {
      if (!drag) return;
      drag.next = drag.size + (vertical ? event.clientY : event.clientX) - drag.position;
      // Resize the boxes live; rebuild thousands of SVG points once on release.
      if (frame === null) frame = requestAnimationFrame(() => { frame = null; if (drag) resize(drag.next, false); });
    });
    const finish = () => {
      if (!drag) return;
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null; resize(drag.next); drag = null;
      panel.classList.remove("is-resizing"); saveWorkspace();
    };
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
    handle.addEventListener("lostpointercapture", finish);
    handle.addEventListener("dblclick", () => {
      if (vertical) resizeCharts(defaultChartHeight);
      else { state.chartWidth = null; applyChartWidth(); resizeCharts(state.chartHeight); }
      saveWorkspace();
    });
    handle.addEventListener("keydown", event => {
      const decrease = vertical ? "ArrowUp" : "ArrowLeft", increase = vertical ? "ArrowDown" : "ArrowRight";
      if (![decrease, increase, "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      resize(event.key === "Home" ? minimum : event.key === "End" ? maximum : currentSize() + (event.key === decrease ? -20 : 20));
      saveWorkspace();
    });
    return handle;
  }

  function moveChart(id, target) {
    const from = state.charts.findIndex(chart => chart.id === id);
    if (from < 0 || target < 0 || target >= state.charts.length) return;
    const [chart] = state.charts.splice(from, 1);
    state.charts.splice(target, 0, chart);
    render();
    saveWorkspace();
    chartPanels.get(id)?.querySelector("button")?.focus();
  }

  function renderChartBoard(rows) {
    comparisonRows = rows;
    const active = new Set(state.charts.map(chart => chart.id));
    for (const [id, panel] of chartPanels) if (!active.has(id)) { panel.remove(); chartPanels.delete(id); }
    for (const chart of state.charts) {
      let panel = chartPanels.get(chart.id);
      if (!panel) {
        panel = el("section", "panel visual-chart-panel comparison-card");
        panel.dataset.chartId = chart.id;
        const title = `${chartLabels[chart.x]} · ${chartLabels[chart.y]}`;
        panel.setAttribute("aria-label", title);
        const heading = el("div", "chart-card-heading"), handle = el("button", "chart-drag-handle", "⠿");
        handle.type = "button";
        handle.draggable = true;
        handle.setAttribute("aria-label", `${title} 순서 이동`);
        handle.title = "끌어서 이동 · Alt+방향키로 이동";
        handle.addEventListener("dragstart", event => { draggedChart = chart.id; event.dataTransfer.setData("text/plain", chart.id); event.dataTransfer.effectAllowed = "move"; panel.classList.add("is-dragging"); });
        handle.addEventListener("dragend", () => { draggedChart = null; panel.classList.remove("is-dragging"); });
        handle.addEventListener("keydown", event => {
          if (!event.altKey || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
          event.preventDefault();
          const index = state.charts.indexOf(chart);
          moveChart(chart.id, index + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1));
        });
        const tools = el("div", "chart-card-tools");
        const swap = el("button", "chart-swap-button", "⇄"); swap.type = "button";
        swap.title = "X축·Y축 바꾸기";
        swap.addEventListener("click", () => {
          const previousMinHeight = panel.style.minHeight;
          panel.style.minHeight = `${panel.getBoundingClientRect().height}px`;
          [chart.x, chart.y] = [chart.y, chart.x];
          renderChartPanel(chart, panel, comparisonRows);
          panel.style.minHeight = previousMinHeight;
          saveWorkspace(); swap.focus({preventScroll: true});
        });
        tools.append(swap);
        for (const [label, delta] of [["←", -1], ["→", 1]]) {
          const button = el("button", "chart-order-button", label); button.type = "button";
          button.setAttribute("aria-label", `${title} ${delta < 0 ? "앞으로" : "뒤로"} 이동`);
          button.addEventListener("click", () => moveChart(chart.id, state.charts.indexOf(chart) + delta));
          tools.append(button);
        }
        const remove = el("button", "chart-remove-button", "−"); remove.type = "button";
        remove.setAttribute("aria-label", `${title} 그래프 삭제`);
        remove.addEventListener("click", () => {
          const index = state.charts.indexOf(chart);
          state.charts = state.charts.filter(item => item.id !== chart.id);
          render(); saveWorkspace();
          const next = state.charts[Math.min(index, state.charts.length - 1)];
          (chartPanels.get(next?.id)?.querySelector("button") || $("chart-x")).focus({preventScroll: true});
        });
        tools.append(remove);
        heading.append(handle, el("h2", "", title), tools);
        panel.append(heading, el("div", "visual-chart"), sizeHandle(panel, "height"), sizeHandle(panel, "width"));
        panel.addEventListener("dragover", event => { if (draggedChart) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; } });
        panel.addEventListener("drop", event => { if (!draggedChart) return; event.preventDefault(); moveChart(draggedChart, state.charts.indexOf(chart)); });
        chartPanels.set(chart.id, panel);
      }
      grid.append(panel);
      renderChartPanel(chart, panel, rows);
    }
    grid.append(builder);
  }
  function renderChartPanel(chart, panel, rows) {
    const title = `${chartLabels[chart.x]} · ${chartLabels[chart.y]}`;
    panel.setAttribute("aria-label", title);
    panel.querySelector("h2").textContent = title;
    panel.querySelector(".chart-drag-handle").setAttribute("aria-label", `${title} 순서 이동`);
    panel.querySelector(".chart-swap-button").setAttribute("aria-label", `${title} X축·Y축 바꾸기`);
    panel.querySelector(".chart-remove-button").setAttribute("aria-label", `${title} 그래프 삭제`);
    const handle = panel.querySelector(".chart-height-handle");
    handle.setAttribute("aria-label", `${title}에서 전체 그래프 높이 조절`);
    handle.setAttribute("aria-valuenow", state.chartHeight);
    handle.setAttribute("aria-valuetext", `전체 그래프 ${state.chartHeight}픽셀`);
    const widthHandle = panel.querySelector(".chart-width-handle"), width = Math.round(panel.getBoundingClientRect().width);
    widthHandle.setAttribute("aria-label", `${title}에서 전체 그래프 너비 조절`);
    widthHandle.setAttribute("aria-valuenow", width);
    widthHandle.setAttribute("aria-valuetext", `전체 그래프 ${width}픽셀`);
    const buttons = panel.querySelectorAll(".chart-order-button"), index = state.charts.indexOf(chart);
    buttons[0].setAttribute("aria-label", `${title} 앞으로 이동`);
    buttons[1].setAttribute("aria-label", `${title} 뒤로 이동`);
    buttons[0].disabled = index === 0; buttons[1].disabled = index === state.charts.length - 1;
    const host = panel.querySelector(".visual-chart");
    draw(host, rows, chart.x, chart.y, host.clientHeight);
  }
  for (const id of ["chart-x", "chart-y"]) {
    $(id).replaceChildren(...[["", "지표 선택"], ...Object.entries(chartLabels)].map(([value,label]) => { const option=el("option", "", label); option.value=value; return option; }));
    $(id).addEventListener("change", () => {
      const x = $("chart-x").value, y = $("chart-y").value;
      if (!x || !y) return;
      state.charts.push({id: "custom-" + crypto.randomUUID(), x, y});
      $("chart-x").value = $("chart-y").value = "";
      $("axis-chart").textContent = `${chartLabels[x]} · ${chartLabels[y]} 그래프를 추가했습니다.`;
      render(); saveWorkspace(); $("chart-x").focus();
    });
  }
  $("chart-columns").addEventListener("change", () => {
    state.columns = Number($("chart-columns").value);
    state.chartWidth = null;
    applyChartWidth();
    resizeCharts(state.chartHeight);
    saveWorkspace();
  });
  function restore(saved = {}) {
    state.chartHeight=Number.isFinite(saved.chartHeight)?Math.min(maxChartHeight,Math.max(minChartHeight,saved.chartHeight)):defaultChartHeight;
    state.chartWidth=Number.isFinite(saved.chartWidth)?Math.min(maxChartWidth,Math.max(minChartWidth,saved.chartWidth)):null;
    state.columns=[2,3,4].includes(saved.columns)?saved.columns:3;
    if(Array.isArray(saved.charts)) {
      const ids=new Set();
      state.charts=saved.charts.filter(c=>c && typeof c.id==="string" && c.id!=="builder" && !ids.has(c.id) && Object.hasOwn(chartLabels,c.x) && Object.hasOwn(chartLabels,c.y) && ids.add(c.id)).map(({id,x,y})=>({id,x,y}));
    }
    grid.style.setProperty("--chart-height",`${state.chartHeight}px`); applyChartWidth();
  }
  restore(state);
  return {render:renderChartBoard,applyWidth:applyChartWidth,resizeHeight:resizeCharts,restore,
    snapshot:()=>({charts:state.charts.map(c=>({...c})),columns:state.columns,chartHeight:state.chartHeight,chartWidth:state.chartWidth})};
};
window.CollectorChartBoard.limits={defaultChartHeight:420,minChartHeight:320,maxChartHeight:900,minChartWidth:240,maxChartWidth:1000};
