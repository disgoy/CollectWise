// The call's timeline for the page (D88): a clock from the moment the call
// connects, when the caller spoke, when the agent's voice started playing and
// how long after the caller finished that was. Times are milliseconds since
// the call connected. No browser APIs, so Node can test it.

// 12_400 -> "0:12.4"
export function formatClock(ms) {
  const tenths = Math.max(0, Math.round(ms / 100));
  const seconds = Math.floor(tenths / 10);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}.${tenths % 10}`;
}

// 222_000 -> "3:42"
export function formatDuration(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

export class CallTimeline {
  constructor(now = () => performance.now()) {
    this.now = now;
    this.startedAt = null;
    this.endedAt = null;
    this.entries = [];
    this.replies = new Map();
    this.speakingSince = null; // when the current caller turn started
    this.lastFinished = null; // when the caller's last turn ended (for the reply delay)
  }

  start() {
    this.startedAt = this.now();
  }

  end() {
    if (this.startedAt !== null && this.endedAt === null) this.endedAt = this.now();
  }

  elapsed() {
    if (this.startedAt === null) return 0;
    return (this.endedAt ?? this.now()) - this.startedAt;
  }

  // The listener heard the caller begin. A reply that follows without a
  // caller turn (e.g. "say that again") gets no delay figure.
  callerSpeaking() {
    if (this.speakingSince === null) this.speakingSince = this.elapsed();
    this.lastFinished = null;
  }

  // The caller's turn ended and went to the agent.
  callerSaid(text) {
    const finished = this.elapsed();
    const entry = { who: "caller", text, from: this.speakingSince ?? finished, to: finished };
    this.entries.push(entry);
    this.speakingSince = null;
    this.lastFinished = finished;
    return entry;
  }

  reply(number) {
    if (!this.replies.has(number)) {
      const entry = { who: "agent", reply: number, text: "", at: null, delay: null, interruptedAt: null };
      this.replies.set(number, entry);
      this.entries.push(entry);
      this.speakingSince = null; // the caller has finished, even if unheard
    }
    return this.replies.get(number);
  }

  agentSentence(number, text) {
    const entry = this.reply(number);
    entry.text = entry.text ? `${entry.text} ${text}` : text;
    return entry;
  }

  // The reply's first audio starts playing `inMs` from now.
  agentAudioStarts(number, inMs = 0) {
    const entry = this.reply(number);
    if (entry.at === null) {
      entry.at = this.elapsed() + inMs;
      if (this.lastFinished !== null) entry.delay = entry.at - this.lastFinished;
    }
    return entry;
  }

  interrupted(number) {
    const entry = this.reply(number);
    if (entry.interruptedAt === null) entry.interruptedAt = this.elapsed();
    return entry;
  }

  // The whole conversation with times, for copying.
  toText() {
    const lines = [`CollectWise live call, ${formatDuration(this.elapsed())}`];
    for (const entry of this.entries) {
      if (entry.who === "caller") {
        lines.push(`${formatClock(entry.from)}-${formatClock(entry.to)}  You: ${entry.text}`);
      } else {
        const at = entry.at === null ? "(text only)" : formatClock(entry.at);
        const delay = entry.delay === null ? "" : ` (replied ${(entry.delay / 1000).toFixed(1)} s after you finished)`;
        const cut = entry.interruptedAt === null ? "" : ` [interrupted at ${formatClock(entry.interruptedAt)}]`;
        lines.push(`${at}  Agent${delay}: ${entry.text}${cut}`);
      }
    }
    return lines.join("\n");
  }
}
