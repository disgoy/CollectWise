import assert from "node:assert/strict";
import { test } from "node:test";

import { PlaybackTracker, playOutAtEnd } from "../../app/static/live/playback.mjs";

// A scheduler whose chunks end when the test says so; stopping a chunk fires
// its "ended" callbacks, as Web Audio does.
function fakeAudio() {
  const chunks = [];
  const sent = [];
  let resets = 0;
  const schedule = () => {
    const callbacks = [];
    const chunk = {
      ended: false,
      stopped: false,
      onEnded: (cb) => callbacks.push(cb),
      end() {
        this.ended = true;
        callbacks.forEach((cb) => cb());
      },
      stop() {
        this.stopped = true;
        this.end();
      },
    };
    chunks.push(chunk);
    return chunk;
  };
  const tracker = new PlaybackTracker({ schedule, send: (m) => sent.push(m), reset: () => resets++ });
  return { tracker, chunks, sent, resets: () => resets };
}

const SAMPLES = new Float32Array(10);

test("a segment is reported once its audio is complete and has finished playing", () => {
  const { tracker, chunks, sent } = fakeAudio();
  tracker.audio(0, 0, SAMPLES, 24000);
  tracker.audio(0, 0, SAMPLES, 24000);
  chunks[0].end();
  assert.deepEqual(sent, []);
  tracker.segmentEnd(0, 0); // all audio sent, one chunk still playing
  assert.deepEqual(sent, []);
  chunks[1].end();
  assert.deepEqual(sent, [{ type: "segment_played", reply: 0, segment: 0 }]);
});

test("a segment whose audio finished before its end marker arrived is reported on the marker", () => {
  const { tracker, chunks, sent } = fakeAudio();
  tracker.audio(1, 4, SAMPLES, 24000);
  chunks[0].end();
  tracker.segmentEnd(1, 4);
  assert.deepEqual(sent, [{ type: "segment_played", reply: 1, segment: 4 }]);
  tracker.segmentEnd(1, 4);
  assert.equal(sent.length, 1); // reported once
});

test("a stop silences everything and lists only the segments played in full", () => {
  const { tracker, chunks, sent, resets } = fakeAudio();
  tracker.audio(2, 0, SAMPLES, 24000);
  tracker.segmentEnd(2, 0);
  chunks[0].end(); // segment 0 heard
  tracker.audio(2, 1, SAMPLES, 24000); // segment 1 playing
  tracker.audio(2, 1, SAMPLES, 24000);
  chunks[1].end();
  assert.ok(tracker.isPlaying);

  tracker.stop(2);

  assert.ok(chunks[2].stopped);
  assert.ok(!tracker.isPlaying);
  assert.equal(resets(), 1);
  assert.deepEqual(sent.at(-1), { type: "playback_stopped", reply: 2, played: [0] });
  // Stopping fired "ended" on segment 1's chunks; that is not hearing them.
  tracker.segmentEnd(2, 1);
  assert.ok(!sent.some((m) => m.type === "segment_played" && m.segment === 1));
});

test("audio for a stopped segment is not played", () => {
  const { tracker, chunks } = fakeAudio();
  tracker.audio(3, 0, SAMPLES, 24000);
  tracker.stop(3);
  tracker.audio(3, 0, SAMPLES, 24000); // late audio of the interrupted reply
  assert.equal(chunks.length, 1);
});

test("the stop answer names only that reply's segments", () => {
  const { tracker, chunks, sent } = fakeAudio();
  tracker.audio(0, 0, SAMPLES, 24000);
  tracker.segmentEnd(0, 0);
  chunks[0].end();
  tracker.audio(1, 1, SAMPLES, 24000);
  tracker.stop(1);
  assert.deepEqual(sent.at(-1), { type: "playback_stopped", reply: 1, played: [] });
});

test("a later reply plays normally after a stop", () => {
  const { tracker, chunks, sent } = fakeAudio();
  tracker.audio(0, 0, SAMPLES, 24000);
  tracker.stop(0);
  tracker.audio(1, 1, SAMPLES, 24000);
  tracker.segmentEnd(1, 1);
  chunks[1].end();
  assert.deepEqual(sent.at(-1), { type: "segment_played", reply: 1, segment: 1 });
});

test("a gap between chunks of one segment isn't mistaken for the end of it", () => {
  const { tracker, chunks, sent } = fakeAudio();
  tracker.audio(0, 0, SAMPLES, 24000);
  chunks[0].end(); // played out before the next chunk arrived
  assert.deepEqual(sent, []);
  tracker.audio(0, 0, SAMPLES, 24000);
  tracker.segmentEnd(0, 0);
  assert.deepEqual(sent, []);
  chunks[1].end();
  assert.deepEqual(sent, [{ type: "segment_played", reply: 0, segment: 0 }]);
});

// --- the end of a call -------------------------------------------------------

test("the last reply keeps playing after the call closes, until its final chunk ends", () => {
  const { tracker, chunks } = fakeAudio();
  tracker.audio(5, 0, SAMPLES, 24000);
  tracker.audio(5, 0, SAMPLES, 24000);
  let quiet = 0;
  tracker.whenQuiet(() => quiet++);
  chunks[0].end();
  assert.equal(quiet, 0); // one chunk still to play
  chunks[1].end();
  assert.equal(quiet, 1);
  chunks[1].end(); // a late "ended" calls nobody twice
  assert.equal(quiet, 1);
});

test("with nothing playing, waiting for quiet ends at once", () => {
  const { tracker } = fakeAudio();
  let quiet = 0;
  tracker.whenQuiet(() => quiet++);
  assert.equal(quiet, 1);
});

test("a stop while waiting for quiet ends the wait", () => {
  const { tracker } = fakeAudio();
  tracker.audio(6, 0, SAMPLES, 24000);
  let quiet = 0;
  tracker.whenQuiet(() => quiet++);
  tracker.stop(6);
  assert.equal(quiet, 1);
});

test("only a normal end that the caller didn't cause lets the audio finish", () => {
  assert.equal(playOutAtEnd(1000, "handed_off"), true);
  assert.equal(playOutAtEnd(1000, "time_limit"), true);
  assert.equal(playOutAtEnd(1000, "caller_hung_up"), false);
  assert.equal(playOutAtEnd(4401, null), false); // the call broke
  assert.equal(playOutAtEnd(1006, "handed_off"), false);
});
