// The evidence page's numbers and charts (M6.7), as plain data: no DOM here,
// so it runs under `node --test`. evidence-app.mjs turns the shapes into SVG.
// Every number comes from /evidence/data; none is written into the page.

export const TARGET_S = 2; // the first-audio target of the live design (M5)

export function median(values) {
  const sorted = values.filter((v) => v !== null && v !== undefined).sort((a, b) => a - b);
  if (sorted.length === 0) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

// Nearest-rank percentile, like the server's reports.
export function percentile(values, fraction) {
  const sorted = values.filter((v) => v !== null && v !== undefined).sort((a, b) => a - b);
  if (sorted.length === 0) return null;
  const rank = Math.max(1, Math.round(fraction * sorted.length + 0.5 - 1e-9));
  return sorted[Math.min(rank, sorted.length) - 1];
}

export const seconds = (ms) => (ms === null ? "–" : `${(ms / 1000).toFixed(1)} s`);
const percent = (value) => (value === null || value === undefined ? "–" : `${Math.round(value * 10) / 10}%`);

// The headline tiles.
export function headline(tables) {
  const versions = tables.eval_versions;
  const latest = versions.at(-1);
  const voice = tables.voice_conditions;
  const correct = voice.reduce((sum, row) => sum + row.correct, 0);
  const clips = voice.reduce((sum, row) => sum + row.total, 0);
  const steps = tables.mutation_steps;
  const bugs = steps.reduce((sum, row) => sum + row.bugs, 0);
  const firstTime = steps.reduce((sum, row) => sum + row.first_time, 0);
  const tests = tables.tests_over_time;
  return [
    { label: "Agent pass rate, latest version", value: latest ? percent(latest.all_pass) : "–",
      note: latest ? `${latest.version}; first version ${percent(versions[0].all_pass)}` : "no eval results" },
    { label: "Live call: voice starts after the caller finishes", value: seconds(median(tables.live_turns.map((t) => t.first_audio_ms))),
      note: tables.live_turns.length ? `median of ${tables.live_turns.length} turns` : "no live calls on this server yet" },
    { label: "Test phrases heard correctly", value: clips ? percent((100 * correct) / clips) : "–",
      note: clips ? `${correct} of ${clips} clips` : "no voice checks" },
    { label: "Automated tests", value: tests.length ? String(tests.at(-1).tests) : "–",
      note: tests.length ? `from ${tests[0].tests} at the start` : "" },
    { label: "Deliberate bugs caught by the tests as first written", value: bugs ? percent((100 * firstTime) / bugs) : "–",
      note: bugs ? `${firstTime} of ${bugs}` : "" },
  ];
}

// Live speed per step, from the turn rows.
export function liveStages(turns) {
  const audio = turns.map((t) => t.first_audio_ms);
  const text = turns.map((t) => t.first_text_ms);
  const speech = turns.filter((t) => t.first_text_ms !== null).map((t) => t.first_audio_ms - t.first_text_ms);
  const withLookups = turns.filter((t) => t.lookups > 0).map((t) => t.first_audio_ms);
  const without = turns.filter((t) => t.lookups === 0).map((t) => t.first_audio_ms);
  const row = (stage, values) => ({ stage, p50: median(values), p95: percentile(values, 0.95), n: values.filter((v) => v !== null).length });
  return [
    row("to the first words", text),
    row("speech (first audio after first words)", speech),
    row("to the first audio", audio),
    row("to the first audio, turns with lookups", withLookups),
    row("to the first audio, turns without", without),
  ];
}

// Horizontal bars: one per row, the value as a share of `max`.
export function horizontalBars(rows, { label, value, max, width = 520, rowHeight = 28, labelWidth = 200 }) {
  const room = width - labelWidth - 60;
  return {
    width,
    height: rows.length * rowHeight + 8,
    bars: rows.map((row, i) => ({
      label: label(row),
      text: value(row).text,
      x: labelWidth,
      y: 4 + i * rowHeight,
      w: max ? Math.max(0, (room * value(row).amount) / max) : 0,
      h: rowHeight - 10,
    })),
  };
}

// Each live turn's first audio as a column, coloured by whether it looked
// anything up, with the target as a line.
export function turnColumns(turns, { width = 520, height = 200, target = TARGET_S } = {}) {
  const top = Math.max(target, ...turns.map((t) => t.first_audio_ms / 1000)) * 1.1;
  const step = turns.length ? width / turns.length : width;
  const y = (s) => height - (height * s) / top;
  return {
    width,
    height,
    targetY: y(target),
    columns: turns.map((t, i) => ({
      x: i * step + step * 0.15,
      w: step * 0.7,
      y: y(t.first_audio_ms / 1000),
      h: height - y(t.first_audio_ms / 1000),
      kind: t.lookups > 0 ? "lookup" : "plain",
      label: `call ${t.call}, turn ${t.turn}: ${seconds(t.first_audio_ms)}${t.lookups > 0 ? `, ${t.lookups} lookup(s)` : ""}`,
    })),
  };
}

// Tests over time as a polyline.
export function testLine(points, { width = 520, height = 160 } = {}) {
  if (points.length === 0) return { width, height, points: "" };
  const top = Math.max(...points.map((p) => p.tests));
  const step = points.length > 1 ? width / (points.length - 1) : 0;
  return {
    width,
    height,
    points: points.map((p, i) => `${(i * step).toFixed(1)},${(height - (height * p.tests) / top).toFixed(1)}`).join(" "),
  };
}
