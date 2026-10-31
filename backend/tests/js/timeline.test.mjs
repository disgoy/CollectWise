import assert from "node:assert/strict";
import { test } from "node:test";

import { CallTimeline, formatClock, formatDuration } from "../../app/static/live/timeline.mjs";

function clock() {
  const state = { now: 1000 };
  return { state, now: () => state.now };
}

test("clock formats", () => {
  assert.equal(formatClock(0), "0:00.0");
  assert.equal(formatClock(12_449), "0:12.4");
  assert.equal(formatClock(12_450), "0:12.5");
  assert.equal(formatClock(61_000), "1:01.0");
  assert.equal(formatClock(-5), "0:00.0");
  assert.equal(formatDuration(222_999), "3:42");
  assert.equal(formatDuration(5_000), "0:05");
});

test("the clock runs from the start and stops at the end", () => {
  const { state, now } = clock();
  const timeline = new CallTimeline(now);
  assert.equal(timeline.elapsed(), 0); // not started
  timeline.start();
  state.now += 4_000;
  assert.equal(timeline.elapsed(), 4_000);
  timeline.end();
  state.now += 10_000;
  assert.equal(timeline.elapsed(), 4_000);
  timeline.end(); // a second end changes nothing
  assert.equal(timeline.elapsed(), 4_000);
});

test("a turn: when the caller spoke, and how long the agent took to answer", () => {
  const { state, now } = clock();
  const timeline = new CallTimeline(now);
  timeline.start();
  state.now += 12_400;
  timeline.callerSpeaking();
  state.now += 1_000;
  timeline.callerSpeaking(); // repeated starts within one turn keep the first
  state.now += 700;
  const caller = timeline.callerSaid("What's my balance?");
  assert.deepEqual([caller.from, caller.to], [12_400, 14_100]);

  state.now += 2_500;
  timeline.agentSentence(0, "Your main account has 500 US dollars.");
  const agent = timeline.agentAudioStarts(0, 400); // plays 0.4 s from now
  assert.equal(agent.at, 17_000);
  assert.equal(agent.delay, 2_900);
  timeline.agentAudioStarts(0, 0); // later chunks don't move it
  assert.equal(agent.at, 17_000);
  timeline.agentSentence(0, "Anything else?");
  assert.equal(agent.text, "Your main account has 500 US dollars. Anything else?");
});

test("a reply with no caller turn before it (e.g. 'say that again') has no delay", () => {
  const { state, now } = clock();
  const timeline = new CallTimeline(now);
  timeline.start();
  timeline.callerSaid("first");
  state.now += 2_000;
  timeline.agentAudioStarts(0);
  timeline.callerSpeaking(); // mumbled, never reached the agent
  state.now += 1_000;
  const guarded = timeline.agentAudioStarts(1);
  assert.equal(guarded.delay, null);
  // ...and the next real turn starts its own clock
  state.now += 1_000;
  timeline.callerSpeaking();
  assert.equal(timeline.callerSaid("second").from, 4_000);
});

test("an interruption is marked with its time, once", () => {
  const { state, now } = clock();
  const timeline = new CallTimeline(now);
  timeline.start();
  timeline.agentSentence(0, "A long answer.");
  state.now += 21_600;
  timeline.interrupted(0);
  state.now += 500;
  timeline.interrupted(0);
  assert.equal(timeline.reply(0).interruptedAt, 21_600);
});

test("the copied transcript has times, delays and interruptions", () => {
  const { state, now } = clock();
  const timeline = new CallTimeline(now);
  timeline.start();
  state.now += 12_400;
  timeline.callerSpeaking();
  state.now += 1_700;
  timeline.callerSaid("What's my balance?");
  state.now += 2_900;
  timeline.agentSentence(0, "500 US dollars.");
  timeline.agentAudioStarts(0);
  state.now += 1_000;
  timeline.interrupted(0);
  timeline.agentSentence(1, "Shown only.");
  state.now += 5_000;
  timeline.end();
  assert.equal(timeline.toText(), [
    "CollectWise live call, 0:23",
    "0:12.4-0:14.1  You: What's my balance?",
    "0:17.0  Agent (replied 2.9 s after you finished): 500 US dollars. [interrupted at 0:18.0]",
    "(text only)  Agent: Shown only.",
  ].join("\n"));
});
