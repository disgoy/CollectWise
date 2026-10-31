// Audio helpers for the live-call page (M5.6, D84). No browser APIs here, so
// they run under Node's test runner too.

// What the server wants from the microphone: mono 16-bit PCM at 16 kHz in
// 80 ms frames (Flux's recommendation).
export const CALLER_RATE = 16000;
export const FRAME_SAMPLES = (CALLER_RATE * 80) / 1000; // 1280 samples = 2560 bytes

// Turns a stream of blocks at the microphone's rate (often 44.1 or 48 kHz)
// into a stream at `outputRate`, by linear interpolation. The output doesn't
// depend on how the input was cut into blocks.
export class Resampler {
  constructor(inputRate, outputRate = CALLER_RATE) {
    if (!(inputRate > 0) || !(outputRate > 0)) throw new Error("sample rates must be positive");
    this.step = inputRate / outputRate; // input samples per output sample
    this.previous = 0; // the last input sample of the block before
    this.position = 1; // next output's position; index 0 is `previous`
  }

  process(input) {
    const output = [];
    let position = this.position;
    // Positions index [previous, ...input]; interpolating needs the sample
    // after floor(position), so stop before the last one.
    while (position < input.length) {
      const index = Math.floor(position);
      const fraction = position - index;
      const a = index === 0 ? this.previous : input[index - 1];
      const b = input[index];
      output.push(a + (b - a) * fraction);
      position += this.step;
    }
    if (input.length > 0) {
      this.previous = input[input.length - 1];
      this.position = position - input.length;
    }
    return Float32Array.from(output);
  }
}

// -1.0..1.0 floats to 16-bit integers, clipping anything louder.
export function floatToPcm16(samples) {
  const pcm = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    pcm[i] = Math.round(s < 0 ? s * 0x8000 : s * 0x7fff);
  }
  return pcm;
}

// Collects 16-bit samples and hands out exactly `frameSamples` at a time as
// little-endian bytes, keeping the remainder for the next push.
export class Framer {
  constructor(frameSamples = FRAME_SAMPLES) {
    this.frameSamples = frameSamples;
    this.pending = new Int16Array(0);
  }

  push(pcm) {
    const all = new Int16Array(this.pending.length + pcm.length);
    all.set(this.pending);
    all.set(pcm, this.pending.length);
    const frames = [];
    let start = 0;
    for (; start + this.frameSamples <= all.length; start += this.frameSamples) {
      const bytes = new ArrayBuffer(this.frameSamples * 2);
      const view = new DataView(bytes);
      for (let i = 0; i < this.frameSamples; i++) view.setInt16(i * 2, all[start + i], true);
      frames.push(bytes);
    }
    this.pending = all.slice(start);
    return frames;
  }
}

// An audio frame from the server: an 8-byte header (reply, segment as
// little-endian uint32), then 16-bit little-endian PCM.
export function parseAudioFrame(buffer) {
  if (buffer.byteLength < 8 || (buffer.byteLength - 8) % 2 !== 0) throw new Error("bad audio frame");
  const view = new DataView(buffer);
  const samples = new Float32Array((buffer.byteLength - 8) / 2);
  for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(8 + i * 2, true) / 0x8000;
  return { reply: view.getUint32(0, true), segment: view.getUint32(4, true), samples };
}
