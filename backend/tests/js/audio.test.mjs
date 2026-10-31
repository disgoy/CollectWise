// Node's built-in test runner: node --test backend/tests/js
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CALLER_RATE,
  FRAME_SAMPLES,
  Framer,
  Resampler,
  floatToPcm16,
  parseAudioFrame,
} from "../../app/static/live/audio.mjs";

function chunks(array, size) {
  const out = [];
  for (let i = 0; i < array.length; i += size) out.push(array.slice(i, i + size));
  return out;
}

function resampleAll(input, inputRate, blockSize) {
  const resampler = new Resampler(inputRate);
  const out = [];
  for (const block of chunks(input, blockSize)) out.push(...resampler.process(block));
  return Float32Array.from(out);
}

test("one second at 48 kHz becomes one second at 16 kHz", () => {
  const out = resampleAll(new Float32Array(48000).fill(0.25), 48000, 128);
  assert.ok(Math.abs(out.length - CALLER_RATE) <= 1, `got ${out.length}`);
  assert.ok(out.every((s) => Math.abs(s - 0.25) < 1e-6));
});

test("44.1 kHz, which doesn't divide evenly, also comes out at 16 kHz", () => {
  const out = resampleAll(new Float32Array(44100 * 3), 44100, 128);
  assert.ok(Math.abs(out.length - 3 * CALLER_RATE) <= 1, `got ${out.length}`);
});

test("the output doesn't depend on how the microphone cut the blocks", () => {
  const input = Float32Array.from({ length: 4800 }, (_, i) => Math.sin(i / 7));
  const whole = resampleAll(input, 48000, input.length);
  for (const size of [1, 3, 128, 1000]) {
    assert.deepEqual(resampleAll(input, 48000, size), whole, `block size ${size}`);
  }
});

test("values between samples are interpolated", () => {
  // A ramp 0, 1, 2 ... at 3x the rate: outputs land on every third sample.
  const ramp = Float32Array.from({ length: 30 }, (_, i) => i);
  const out = new Resampler(48000, 16000).process(ramp);
  assert.deepEqual([...out.slice(0, 4)], [0, 3, 6, 9]);
  const half = new Resampler(2, 1).process(Float32Array.from([0, 10, 20, 30]));
  assert.deepEqual([...half], [0, 20]);
  // Upsampling: the value at the last input sample needs the next one to
  // interpolate past it, so it comes with the next block.
  const up = new Resampler(1, 2);
  assert.deepEqual([...up.process(Float32Array.from([0, 10, 20]))], [0, 5, 10, 15]);
  assert.deepEqual([...up.process(Float32Array.from([30]))], [20, 25]);
});

test("sample rates must be positive", () => {
  assert.throws(() => new Resampler(0));
  assert.throws(() => new Resampler(48000, -1));
});

test("floats become 16-bit samples, clipped at full scale", () => {
  assert.deepEqual([...floatToPcm16(Float32Array.from([0, 1, -1, 0.5, 2, -3]))], [0, 32767, -32768, 16384, 32767, -32768]);
});

test("frames are exactly 80 ms and the rest waits for the next push", () => {
  assert.equal(FRAME_SAMPLES, 1280);
  const framer = new Framer();
  assert.equal(framer.push(new Int16Array(1000)).length, 0);
  const frames = framer.push(Int16Array.from({ length: 2000 }, (_, i) => i));
  assert.equal(frames.length, 2);
  assert.ok(frames.every((f) => f.byteLength === 2560));
  assert.equal(framer.pending.length, 3000 - 2560);
  // Little-endian, in order: frame 2 starts with input sample 1280 - 1000 = 280 of the second push.
  assert.equal(new DataView(frames[1]).getInt16(0, true), 280);
});

test("an audio frame's header gives reply and segment", () => {
  const buffer = new ArrayBuffer(8 + 4);
  const view = new DataView(buffer);
  view.setUint32(0, 3, true);
  view.setUint32(4, 70000, true);
  view.setInt16(8, 16384, true);
  view.setInt16(10, -32768, true);
  const frame = parseAudioFrame(buffer);
  assert.equal(frame.reply, 3);
  assert.equal(frame.segment, 70000);
  assert.deepEqual([...frame.samples], [0.5, -1]);
});

test("a malformed audio frame is refused", () => {
  assert.throws(() => parseAudioFrame(new ArrayBuffer(4)));
  assert.throws(() => parseAudioFrame(new ArrayBuffer(9)));
});
