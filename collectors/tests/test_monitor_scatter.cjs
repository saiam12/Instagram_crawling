const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; }
  setAttribute(key, value) { this.attrs[key] = value; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
}
const context = {window: {}, document: {
  createElement: tag => new Element(tag),
  createElementNS: (_, tag) => new Element(tag)
}, Intl, Number, Math};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../monitor/web/scatter.js"), "utf8"), context);
for (const width of [360, 1200]) {
  const host = new Element("div"); host.clientWidth = width;
  context.window.CollectorScatter(host, [{x:0,y:0}, {x:100,y:100}, {x:1e7,y:1e6}, {x:null,y:1}], "x", "y", "X", "Y", () => "reel");
  const svg = host.children.find(item => item.tag === "svg");
  assert.equal(host.children[0].textContent, "3개 점");
  assert.equal(host.children[0].className, "scatter-count");
  assert.ok(!host.children.some(item => item.tag === "details"));
  const grid = svg.children.filter(item => item.attrs.class === "visual-gridline");
  assert.ok(Math.abs((Math.max(...grid.map(line => line.attrs.x2)) - 75) * .7 - (Math.max(...grid.map(line => line.attrs.y2)) - 30)) < 1e-8);
  const dots = svg.children.filter(item => item.tag === "circle");
  assert.equal(dots.length, 3);
  assert.equal(dots[0].attrs.cx, 75);
  assert.equal(dots[0].attrs.cy, 30 + (width - 110) * .7);
  assert.ok(dots.every(dot => Number.isFinite(dot.attrs.cx) && Number.isFinite(dot.attrs.cy)));
  const labels = svg.children.filter(item => item.tag === "text" && item.attrs["text-anchor"] === "middle" && item.attrs.y === 54 + (width - 110) * .7);
  assert.equal(labels[0].textContent, "0");
  for (let index = 1; index < labels.length; index++) assert.ok(labels[index].attrs.x - labels[index-1].attrs.x >= 58);
  if (width === 1200) assert.ok(labels.some(item => item.textContent === "100"));
  assert.ok(svg.children.filter(item => item.attrs.class === "visual-axis-tick").length > labels.length);
}
for (const [xKey, yKey] of [["view_count", "like_count"], ["like_count", "view_count"]]) {
  const host = new Element("div"); host.clientWidth = 1200;
  context.window.CollectorScatter(host, [0, 9, 10, 100].map(view_count => ({view_count, like_count:0})), xKey, yKey, "X", "Y", () => "reel");
  assert.equal(host.children[0].textContent, "2개 점");
  const svg = host.children.find(item => item.tag === "svg"), dots = svg.children.filter(item => item.tag === "circle");
  assert.equal(dots.length, 2);
  assert.equal(dots[0].attrs.cx, 75);
  assert.equal(dots[0].attrs.cy, 793);
  const axisLabels = svg.children.filter(item => item.tag === "text" && (xKey === "view_count" ? item.attrs.y === 817 : item.attrs["text-anchor"] === "end"));
  assert.equal(axisLabels[0].textContent, "10");
  assert.ok(!axisLabels.some(item => item.textContent === "0"));
}
context.window.CollectorKorean = {value: row => row.korean};
for (const enabled of [false, true]) {
  const host = new Element("div"); host.clientWidth = 1200;
  context.window.CollectorScatter(host, [true, false, null].map(korean => ({x:100,y:50,korean})), "x", "y", "X", "Y", () => "reel", enabled);
  assert.equal(host.children[0].textContent, "3개 점");
  const dots = host.children.find(item => item.tag === "svg").children.filter(item => item.tag === "circle");
  assert.equal(dots.length, 3);
  assert.equal(dots.filter(dot => dot.attrs.class.includes("is-korean")).length, enabled ? 1 : 0);
}
for (const [query, expected] of [["@CREATOR", 2], ["missing", 0], ["", 0]]) {
  const host = new Element("div"); host.clientWidth = 1200;
  context.window.CollectorScatter(host, ["creator", "CREATOR", "other"].map(username => ({x:100,y:50,username})), "x", "y", "X", "Y", () => "reel", false, query);
  const dots = host.children.find(item => item.tag === "svg").children.filter(item => item.tag === "circle");
  assert.equal(dots.length, 3);
  assert.equal(host.children[0].textContent, "3개 점");
  assert.equal(dots.filter(dot => dot.attrs.class.includes("is-user")).length, expected);
}
{
  const host = new Element("div"); host.clientWidth = 680;
  context.window.CollectorScatter(host, [10,20,30].map(duration => ({duration,view_count:100})), "duration", "view_count", "초", "조회수", () => "reel");
  const svg = host.children.find(item => item.tag === "svg"), dots = svg.children.filter(item => item.tag === "circle");
  assert.ok(Math.abs((dots[1].attrs.cx-dots[0].attrs.cx)-(dots[2].attrs.cx-dots[1].attrs.cx)) < 1e-8);
  assert.ok(svg.attrs["aria-label"].includes("X축 선형"));
  assert.ok(svg.attrs["aria-label"].includes("Y축 로그"));
}
{
  const host = new Element("div"); host.clientWidth = 680;
  context.window.CollectorScatter(host, [10,20,30].map(duration => ({duration,view_count:100})), "view_count", "duration", "조회수", "초", () => "reel");
  const svg = host.children.find(item => item.tag === "svg"), dots = svg.children.filter(item => item.tag === "circle");
  assert.ok(Math.abs((dots[0].attrs.cy-dots[1].attrs.cy)-(dots[1].attrs.cy-dots[2].attrs.cy)) < 1e-8);
  assert.ok(svg.attrs["aria-label"].includes("Y축 선형"));
}

console.log("PASS: log counts, linear duration on either axis, Korean/user highlighting");
