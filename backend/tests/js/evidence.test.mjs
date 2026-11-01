import assert from "node:assert/strict";
import { test } from "node:test";

import {
  TARGET_S, headline, horizontalBars, liveStages, median, percentile, testLine, turnColumns,
} from "../../app/static/evidence/evidence.mjs";

const TABLES = {
  eval_versions: [{ version: "baseline", all_pass: 70 }, { version: "v2", all_pass: 98 }],
  voice_conditions: [{ condition: "clean", correct: 44, total: 45 }, { condition: "noise", correct: 41, total: 45 }],
  live_turns: [
    { call: 1, turn: 1, first_text_ms: 3900, first_audio_ms: 4200, lookups: 2 },
    { call: 1, turn: 2, first_text_ms: 1500, first_audio_ms: 2300, lookups: 0 },
    { call: 2, turn: 1, first_text_ms: 2600, first_audio_ms: 2900, lookups: 1 },
  ],
  tests_over_time: [{ step: "start", tests: 51 }, { step: "now", tests: 962 }],
  mutation_steps: [{ step: "a", bugs: 10, first_time: 9, test_gaps: 1 }, { step: "b", bugs: 10, first_time: 10, test_gaps: 0 }],
};

test("medians and nearest-rank percentiles ignore missing values", () => {
  assert.equal(median([3, null, 1, 2]), 2);
  assert.equal(median([4, 1, 3, 2]), 2.5);
  assert.equal(median([]), null);
  assert.equal(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20], 0.95), 19);
});

test("the headline tiles come from the tables", () => {
  const tiles = headline(TABLES);

  assert.deepEqual(tiles.map((t) => t.value), ["98%", "2.9 s", "94.4%", "962", "95%"]);
  assert.equal(tiles[0].note, "v2; first version 70%");
  assert.equal(tiles[1].note, "median of 3 turns");
  assert.equal(tiles[2].note, "85 of 90 clips");
});

test("with no live calls the speed tile says so", () => {
  const [, live] = headline({ ...TABLES, live_turns: [] });

  assert.equal(live.value, "–");
  assert.equal(live.note, "no live calls on this server yet");
});

test("live speed per step splits turns by lookups", () => {
  const stages = Object.fromEntries(liveStages(TABLES.live_turns).map((s) => [s.stage, s]));

  assert.equal(stages["to the first audio"].p50, 2900);
  assert.equal(stages["speech (first audio after first words)"].p50, 300);
  assert.deepEqual([stages["to the first audio, turns with lookups"].n, stages["to the first audio, turns without"].n], [2, 1]);
  assert.equal(stages["to the first audio, turns with lookups"].p50, 3550);
});

test("bars are scaled to the maximum and keep their labels", () => {
  const chart = horizontalBars(TABLES.eval_versions, {
    label: (v) => v.version, value: (v) => ({ amount: v.all_pass, text: `${v.all_pass}%` }), max: 100,
    width: 360, labelWidth: 100,
  });

  assert.deepEqual(chart.bars.map((b) => b.label), ["baseline", "v2"]);
  assert.equal(chart.bars[1].w, 200 * 0.98); // (360 - 100 - 60) of room
  assert.ok(chart.bars[1].y > chart.bars[0].y);
});

test("turn columns are coloured by lookups and the target line sits at 2 s", () => {
  const chart = turnColumns(TABLES.live_turns, { width: 300, height: 100 });

  assert.deepEqual(chart.columns.map((c) => c.kind), ["lookup", "plain", "lookup"]);
  const top = 4.2 * 1.1;
  assert.ok(Math.abs(chart.targetY - (100 - (100 * TARGET_S) / top)) < 1e-9);
  assert.ok(chart.columns[0].h > chart.columns[1].h); // the slowest turn is the tallest
  assert.match(chart.columns[0].label, /call 1, turn 1: 4.2 s, 2 lookup/);
});

test("the target is always on the chart, even when every turn is faster", () => {
  const chart = turnColumns([{ call: 1, turn: 1, first_text_ms: 500, first_audio_ms: 900, lookups: 0 }], { height: 100 });

  assert.ok(chart.targetY >= 0);
});

test("the test line rises to the latest count", () => {
  const line = testLine(TABLES.tests_over_time, { width: 100, height: 50 });

  assert.equal(line.points, `0.0,${(50 - (50 * 51) / 962).toFixed(1)} 100.0,0.0`);
  assert.equal(testLine([], {}).points, "");
});
