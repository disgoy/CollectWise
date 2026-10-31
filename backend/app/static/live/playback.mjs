// Keeps track of what the caller has actually heard (M5.6, D84), so the server
// can tell the model how much of an interrupted reply got through (D82).
//
// `schedule(samples, sampleRate)` plays a chunk after everything already
// scheduled and returns { stop(), onEnded(callback) }; `send(message)` goes to
// the server. Both are injected, so this runs without a browser in tests.

export class PlaybackTracker {
  constructor({ schedule, send, reset = () => {} }) {
    this.schedule = schedule;
    this.send = send;
    this.reset = reset;
    this.segments = new Map(); // "reply:segment" -> state
    this.playing = new Set(); // chunks scheduled and not yet ended or stopped
  }

  segment(reply, segment) {
    const key = `${reply}:${segment}`;
    if (!this.segments.has(key)) {
      this.segments.set(key, { reply, segment, waiting: 0, complete: false, played: false, stopped: false });
    }
    return this.segments.get(key);
  }

  // A chunk of a segment's audio arrived: play it after the rest.
  audio(reply, segment, samples, sampleRate) {
    const state = this.segment(reply, segment);
    if (state.stopped) return;
    state.waiting += 1;
    const chunk = this.schedule(samples, sampleRate);
    this.playing.add(chunk);
    chunk.onEnded(() => {
      // A stopped chunk "ends" too; reportIfPlayed ignores stopped segments.
      this.playing.delete(chunk);
      state.waiting -= 1;
      this.reportIfPlayed(state);
    });
  }

  // The server has sent all of this segment's audio.
  segmentEnd(reply, segment) {
    const state = this.segment(reply, segment);
    state.complete = true;
    this.reportIfPlayed(state);
  }

  reportIfPlayed(state) {
    if (state.complete && state.waiting === 0 && !state.played && !state.stopped) {
      state.played = true;
      this.send({ type: "segment_played", reply: state.reply, segment: state.segment });
    }
  }

  get isPlaying() {
    return this.playing.size > 0;
  }

  // The caller interrupted: silence everything now and say which of this
  // reply's segments played in full.
  stop(reply) {
    for (const state of this.segments.values()) {
      if (!state.played) state.stopped = true;
    }
    for (const chunk of [...this.playing]) chunk.stop();
    this.playing.clear();
    this.reset();
    const played = [...this.segments.values()]
      .filter((state) => state.reply === reply && state.played)
      .map((state) => state.segment)
      .sort((a, b) => a - b);
    this.send({ type: "playback_stopped", reply, played });
  }
}
