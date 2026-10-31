// The live-call page (M5.6, D84): sign in, then talk to the agent over the
// live WebSocket (protocol in the README). The token is kept only in this
// page's memory.

import { CALLER_RATE, Framer, Resampler, floatToPcm16, parseAudioFrame } from "./audio.mjs";
import { PlaybackTracker } from "./playback.mjs";
import { CallTimeline, formatClock, formatDuration } from "./timeline.mjs";

const CLOSE_MESSAGES = {
  1000: "Call ended.",
  4400: "The call was cut off: this page sent something the server didn't accept.",
  4401: "Your sign-in isn't valid any more. Sign in again.",
  4404: "That conversation wasn't found. Start a new call.",
  4408: "The server didn't receive the sign-in in time.",
  4409: "This conversation is already in a call somewhere else.",
  4503: "Live calls aren't switched on on this server (voice or AI is off).",
};

const END_REASONS = {
  caller_hung_up: "You hung up.",
  handed_off: "You've been passed to a member of the team.",
  listening_unavailable: "The call ended: listening stopped working.",
  time_limit: "The call reached its time limit.",
  silence: "The call ended after a long silence.",
};

const MICROPHONE_ERRORS = {
  NotAllowedError: "Microphone access was blocked. Allow it in the browser's site settings and try again.",
  NotFoundError: "No microphone was found.",
  NotReadableError: "The microphone is in use by another app.",
};

const $ = (id) => document.getElementById(id);

let token = null; // in memory only
let call = null;
let lastTimeline = null; // the most recent call's timeline, for copying

function setStatus(text, { error = false } = {}) {
  $("status").textContent = text;
  $("status").classList.toggle("error", error);
}

function signedIn(newToken, who) {
  token = newToken;
  $("signed-in").hidden = false;
  $("signed-in").textContent = `Signed in as ${who}.`;
  $("start").disabled = false;
  setStatus("Ready. Press Start call and allow the microphone.");
}

// --- signing in ------------------------------------------------------------

async function loadDemoCustomers() {
  let customers;
  try {
    const response = await fetch("/demo/customers");
    if (!response.ok) throw new Error(String(response.status));
    customers = await response.json();
  } catch {
    $("demo-off").hidden = false;
    $("token-sign-in").open = true;
    return;
  }
  const box = $("demo-customers");
  for (const customer of customers) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `Sign in as ${customer.display_name}`;
    button.addEventListener("click", () => demoSignIn(customer));
    box.append(button);
  }
  if (customers.length === 0) {
    $("demo-off").hidden = false;
    $("demo-off").textContent = "Demo sign-in is on, but no demo customer exists yet. Run scripts/seed_demo.py.";
  }
}

async function demoSignIn(customer) {
  const response = await fetch("/demo/sign-in", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ customer_id: customer.customer_id }),
  });
  if (!response.ok) {
    const reason = response.status === 503
      ? "The server can't sign tokens: set COLLECTWISE_SESSION_SECRET (32+ characters) and restart it."
      : `Sign-in failed (${response.status}).`;
    setStatus(reason, { error: true });
    return;
  }
  signedIn((await response.json()).token, customer.display_name);
}

function useToken() {
  const value = $("token").value.trim();
  $("token").value = "";
  if (value) signedIn(value, "the token's customer");
}

// --- the call ----------------------------------------------------------------

async function createConversation() {
  const response = await fetch("/agent/sessions", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (response.status === 401) throw new Error(CLOSE_MESSAGES[4401]);
  if (response.status === 429) throw new Error("Too many new calls just now. Wait a minute and try again.");
  if (response.status === 503) throw new Error(CLOSE_MESSAGES[4503]);
  if (!response.ok) throw new Error(`Couldn't start a conversation (${response.status}).`);
  return (await response.json()).session_id;
}

// Plays chunks back to back on one AudioContext; a chunk's buffer keeps its
// own sample rate and the context resamples it.
function makeScheduler(context) {
  let next = 0;
  return {
    schedule(samples, sampleRate) {
      const buffer = context.createBuffer(1, samples.length, sampleRate);
      buffer.copyToChannel(samples, 0);
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      next = Math.max(next, context.currentTime + 0.02);
      source.start(next);
      const startsIn = next - context.currentTime; // seconds from now
      next += buffer.duration;
      const callbacks = [];
      source.onended = () => callbacks.forEach((callback) => callback());
      return {
        startsIn,
        stop() {
          try {
            source.stop();
          } catch {
            // already finished
          }
        },
        onEnded(callback) {
          callbacks.push(callback);
        },
      };
    },
    reset() {
      next = 0;
    },
  };
}

class Conversation {
  constructor() {
    $("conversation").replaceChildren();
    this.items = new Map(); // timeline entry -> list item
  }

  // Show (or refresh) one timeline entry.
  show(entry) {
    let item = this.items.get(entry);
    if (!item) {
      item = document.createElement("li");
      item.className = entry.who;
      const label = document.createElement("span");
      label.className = "who";
      const body = document.createElement("span");
      body.className = "text";
      item.append(label, body);
      $("conversation").append(item);
      this.items.set(entry, item);
    }
    item.querySelector(".who").textContent = describe(entry);
    item.querySelector(".text").textContent = entry.text;
    let cut = item.querySelector(".cut");
    if (entry.interruptedAt !== undefined && entry.interruptedAt !== null && !cut) {
      cut = document.createElement("span");
      cut.className = "cut";
      item.append(cut);
    }
    if (cut) cut.textContent = ` (interrupted at ${formatClock(entry.interruptedAt)})`;
    item.scrollIntoView({ block: "nearest" });
  }
}

function describe(entry) {
  if (entry.who === "caller") return `${formatClock(entry.from)}-${formatClock(entry.to)} · You`;
  const at = entry.at === null ? "text only" : formatClock(entry.at);
  const delay = entry.delay === null ? "" : ` · replied ${(entry.delay / 1000).toFixed(1)} s after you finished`;
  return `${at} · Agent${delay}`;
}

async function startCall() {
  $("start").disabled = true;
  setStatus("Connecting...");
  try {
    const sessionId = await createConversation();
    // Created in the click handler: browsers only allow audio after a gesture.
    const context = new AudioContext();
    await context.audioWorklet.addModule("/live/static/capture-worklet.js");
    setStatus("Allow microphone access to continue...");
    let microphone;
    try {
      microphone = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (error) {
      context.close();
      throw new Error(MICROPHONE_ERRORS[error.name] || "Couldn't use the microphone.");
    }
    setStatus("Connecting...");
    call = openCall(sessionId, context, microphone);
  } catch (error) {
    setStatus(error.message || "Couldn't start the call.", { error: true });
    $("start").disabled = false;
  }
}

function openCall(sessionId, context, microphone) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/agent/sessions/${encodeURIComponent(sessionId)}/live`);
  socket.binaryType = "arraybuffer";

  const send = (message) => {
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  };
  const scheduler = makeScheduler(context);
  let startsIn = 0; // when the chunk just scheduled will start playing, in seconds
  const schedule = (samples, sampleRate) => {
    const chunk = scheduler.schedule(samples, sampleRate);
    startsIn = chunk.startsIn;
    return chunk;
  };
  const playback = new PlaybackTracker({ schedule, send, reset: scheduler.reset });
  const conversation = new Conversation();
  const timeline = new CallTimeline();
  lastTimeline = timeline;
  $("copy").disabled = true;
  let clockTimer = null;
  let outputRate = 24000;
  let ready = false;

  // Microphone -> 16 kHz -> 80 ms frames -> server.
  const source = context.createMediaStreamSource(microphone);
  const capture = new AudioWorkletNode(context, "capture");
  const silent = context.createGain();
  silent.gain.value = 0; // keeps the worklet running without playing the mic back
  source.connect(capture).connect(silent).connect(context.destination);
  const resampler = new Resampler(context.sampleRate, CALLER_RATE);
  const framer = new Framer();
  capture.port.onmessage = (event) => {
    if (!ready || socket.readyState !== WebSocket.OPEN) return;
    for (const frame of framer.push(floatToPcm16(resampler.process(event.data)))) socket.send(frame);
  };

  socket.onopen = () => socket.send(JSON.stringify({ type: "start", token }));

  socket.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      const frame = parseAudioFrame(event.data);
      startsIn = null;
      playback.audio(frame.reply, frame.segment, frame.samples, outputRate);
      if (startsIn !== null) conversation.show(timeline.agentAudioStarts(frame.reply, startsIn * 1000));
      setStatus("Agent speaking...");
      return;
    }
    const message = JSON.parse(event.data);
    switch (message.type) {
      case "ready":
        ready = true;
        outputRate = message.sample_rate_out;
        timeline.start();
        clockTimer = setInterval(() => {
          $("clock").textContent = formatDuration(timeline.elapsed());
        }, 250);
        $("hang-up").disabled = false;
        setStatus("Listening. Go ahead and speak.");
        break;
      case "caller_speaking":
        timeline.callerSpeaking();
        break;
      case "caption":
        $("caption").textContent = message.text;
        break;
      case "caller_said":
        $("caption").textContent = "";
        conversation.show(timeline.callerSaid(message.text));
        break;
      case "agent_sentence":
        conversation.show(timeline.agentSentence(message.reply, message.text));
        break;
      case "segment_end":
        playback.segmentEnd(message.reply, message.segment);
        break;
      case "stop_playback":
        playback.stop(message.reply);
        conversation.show(timeline.interrupted(message.reply));
        setStatus("Listening.");
        break;
      case "reply_done":
        if (!playback.isPlaying) setStatus("Listening.");
        break;
      case "call_ended":
        setStatus(END_REASONS[message.reason] || "Call ended.");
        break;
      default:
        break; // newer server messages are ignored
    }
  };

  socket.onclose = (event) => {
    ready = false;
    timeline.end();
    clearInterval(clockTimer);
    $("clock").textContent = formatDuration(timeline.elapsed());
    $("copy").disabled = false;
    microphone.getTracks().forEach((track) => track.stop());
    context.close();
    $("hang-up").disabled = true;
    $("start").disabled = token === null;
    if (event.code !== 1000) setStatus(CLOSE_MESSAGES[event.code] || `The call was cut off (${event.code}).`, { error: true });
    call = null;
  };

  return { hangUp: () => send({ type: "hang_up" }) };
}

// --- wiring --------------------------------------------------------------------

$("use-token").addEventListener("click", useToken);
$("start").addEventListener("click", startCall);
$("hang-up").addEventListener("click", () => call?.hangUp());
$("copy").addEventListener("click", async () => {
  if (!lastTimeline) return;
  try {
    await navigator.clipboard.writeText(lastTimeline.toText());
    setStatus("Transcript copied.");
  } catch {
    setStatus("Couldn't copy: the browser blocked the clipboard.", { error: true });
  }
});
loadDemoCustomers();
