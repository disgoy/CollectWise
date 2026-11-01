// The evidence page (M6.7): fetch /evidence/data and draw it. The numbers and
// chart shapes come from evidence.mjs; this file only builds the DOM.

import { headline, horizontalBars, liveStages, seconds, testLine, turnColumns } from "./evidence.mjs";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";

function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  return element;
}

function svgFrame(width, height, summary) {
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": summary });
  const title = svgElement("title");
  title.textContent = summary;
  svg.append(title);
  return svg;
}

function empty(container, message) {
  const p = document.createElement("p");
  p.className = "empty";
  p.textContent = message;
  container.replaceChildren(p);
}

function table(columns, rows) {
  const element = document.createElement("table");
  const head = element.createTHead().insertRow();
  for (const [title] of columns) {
    const th = document.createElement("th");
    th.textContent = title;
    head.append(th);
  }
  const body = element.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const [, cell] of columns) tr.insertCell().textContent = cell(row);
  }
  return element;
}

function drawBars(container, chart, summary) {
  const svg = svgFrame(chart.width, chart.height, summary);
  for (const bar of chart.bars) {
    const label = svgElement("text", { x: 0, y: bar.y + bar.h - 4 });
    label.textContent = bar.label;
    const rect = svgElement("rect", { x: bar.x, y: bar.y, width: bar.w, height: bar.h, rx: 3, class: "bar" });
    const value = svgElement("text", { x: bar.x + bar.w + 6, y: bar.y + bar.h - 4 });
    value.textContent = bar.text;
    svg.append(label, rect, value);
  }
  container.replaceChildren(svg);
}

const pct = (value) => (value === null || value === undefined ? "–" : `${value}%`);

function render(payload) {
  const { tables, sources } = payload;
  for (const span of document.querySelectorAll("[data-source]")) {
    span.textContent = `from ${sources[span.dataset.source]}`;
  }

  $("headline").replaceChildren(...headline(tables).map((tile) => {
    const div = document.createElement("div");
    div.className = "tile";
    for (const [cls, text] of [["label", tile.label], ["value", tile.value], ["note", tile.note]]) {
      const part = document.createElement("div");
      part.className = cls;
      part.textContent = text;
      div.append(part);
    }
    return div;
  }));

  const versions = tables.eval_versions;
  if (versions.length) {
    drawBars($("quality-chart"), horizontalBars(versions, {
      label: (v) => v.version, value: (v) => ({ amount: v.all_pass, text: pct(v.all_pass) }), max: 100, labelWidth: 90,
    }), "Conversations passing every check, by agent version");
    $("quality-table").replaceChildren(table(
      [["Version", (v) => v.version], ["Change", (v) => v.change], ["Outcome", (v) => pct(v.outcome)],
        ["Compliance", (v) => pct(v.compliance)], ["Reply quality", (v) => pct(v.quality)], ["All pass", (v) => pct(v.all_pass)]],
      versions));
  } else {
    empty($("quality-chart"), "No eval results.");
  }

  const conditions = tables.voice_conditions;
  if (conditions.length) {
    drawBars($("hearing-chart"), horizontalBars(conditions, {
      label: (c) => c.condition,
      value: (c) => ({ amount: (100 * c.correct) / c.total, text: `${c.correct}/${c.total}` }),
      max: 100, labelWidth: 230,
    }), "Test phrases heard correctly, by voice and noise");
    $("hearing-table").replaceChildren(table(
      [["Condition", (c) => c.condition], ["Heard correctly", (c) => String(c.correct)], ["Clips", (c) => String(c.total)]],
      conditions));
  } else {
    empty($("hearing-chart"), "No voice checks.");
  }

  const turns = tables.live_turns;
  if (turns.length) {
    const chart = turnColumns(turns);
    const svg = svgFrame(chart.width, chart.height, "Each live turn's time to first audio, against the 2-second target");
    for (const column of chart.columns) {
      const rect = svgElement("rect", { x: column.x, y: column.y, width: column.w, height: column.h, class: column.kind });
      const tip = svgElement("title");
      tip.textContent = column.label;
      rect.append(tip);
      svg.append(rect);
    }
    svg.append(svgElement("line", { x1: 0, x2: chart.width, y1: chart.targetY, y2: chart.targetY, class: "target" }));
    $("speed-chart").replaceChildren(svg);
    $("speed-table").replaceChildren(table(
      [["Step", (s) => s.stage], ["Median", (s) => seconds(s.p50)], ["p95", (s) => seconds(s.p95)], ["Turns", (s) => String(s.n)]],
      liveStages(turns)));
  } else {
    empty($("speed-chart"), "No live calls on this server yet. Make one on the Live call page, then refresh.");
    $("speed-table").replaceChildren();
  }

  const tests = tables.tests_over_time;
  const line = testLine(tests);
  const svg = svgFrame(line.width, line.height, "Automated tests over the project's steps");
  svg.append(svgElement("polyline", { points: line.points, class: "line" }));
  $("tests-chart").replaceChildren(svg);
  $("mutation-table").replaceChildren(table(
    [["Step", (s) => s.step], ["Bugs planted", (s) => String(s.bugs)], ["Caught first time", (s) => String(s.first_time)],
      ["Exposed a missing test", (s) => String(s.test_gaps)]],
    tables.mutation_steps));
}

async function load() {
  $("status").textContent = "Loading…";
  $("status").classList.remove("error");
  try {
    const response = await fetch("/evidence/data", { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    const payload = await response.json();
    render(payload);
    $("status").textContent = `Built ${new Date(payload.generated_at).toLocaleString()}.`;
  } catch (error) {
    $("status").textContent = `Couldn't load the numbers (${error.message}).`;
    $("status").classList.add("error");
  }
}

$("refresh").addEventListener("click", load);
load();
