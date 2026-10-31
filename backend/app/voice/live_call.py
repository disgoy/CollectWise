"""A live call (M5.3, D81; live safety M5.4, D82): the caller's audio streams
in, each finished turn goes through the guard and the agent, and the reply is
spoken sentence by sentence while the model is still writing.

The caller can interrupt: their first words over the agent stop it, and the
model is told how much of its reply they heard. A payment whose read-back was
interrupted can't be confirmed until it is read back again. Calls end after a
time limit or a long silence.

Transport-free: the WebSocket endpoint (5.5) supplies the `CallerLink`.
"""

import asyncio
import logging
import re
import statistics
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from typing import Protocol

from sqlalchemy.orm import Session

from backend.app import config, telemetry
from backend.app.agent.llm import LLMClient
from backend.app.agent.loop import REPLY_HANDED_OFF, TurnResult, run_turn
from backend.app.agent.sessions import ConversationSession
from backend.app.services.conversation_events import ConversationEvent, record_conversation_events
from backend.app.services.voice_metrics import LiveTurnMetric, record_live_turn_metric
from backend.app.voice.guard import check_transcript
from backend.app.voice.live_speech import (
    HeardEvent,
    LiveSpeaker,
    LiveSpeechToText,
    LiveTextToSpeech,
    SegmentDone,
    SpokenAudio,
)
from backend.app.voice.sentences import SentenceSplitter
from backend.app.voice.speech import SpeechUnavailableError

logger = logging.getLogger(__name__)

# The caller's audio: mono 16-bit PCM at 16 kHz (Flux's recommended rate).
CALLER_SAMPLE_RATE = 16_000

REPLY_GOODBYE_CANT_HEAR = (
    "Sorry, I'm having trouble hearing you, so I'll have to end this call. Please try again later."
)
REPLY_TIME_LIMIT = "We've reached the time limit for this call, so I'll end it here. Thank you for calling."
REPLY_SILENCE = "I haven't heard anything for a while, so I'll end this call. Please call again any time."
REPLY_SLOW_DOWN = "Sorry, I need a moment to catch up. Please say that again in a few seconds."

# A reply whose audio hasn't all arrived by then is given up on, so a lost
# Flushed can't stall the call.
REPLY_AUDIO_TIMEOUT_S = 60.0
# How long to wait for the caller's app to say what it played after a stop.
STOP_ANSWER_TIMEOUT_S = 1.0
# How often the call limits are checked.
WATCHDOG_TICK_S = 1.0


# --- what the caller's app receives ------------------------------------------


@dataclass(frozen=True)
class CallerSpeaking:
    """The listener heard the caller start a turn (for the page's timeline)."""


@dataclass(frozen=True)
class Caption:
    """The caller's words so far in this turn."""

    text: str


@dataclass(frozen=True)
class CallerSaid:
    """A finished caller turn that passed the guard and went to the agent."""

    text: str


@dataclass(frozen=True)
class AgentSentence:
    """One sentence of a reply, as text (also the fallback when speech fails)."""

    reply: int
    sentence: int
    text: str


@dataclass(frozen=True)
class AgentAudio:
    """Mono 16-bit PCM of part of a reply, at the speaker's sample rate."""

    reply: int
    segment: int
    pcm: bytes


@dataclass(frozen=True)
class SegmentEnd:
    """All audio of this segment has been sent; report it once played."""

    reply: int
    segment: int


@dataclass(frozen=True)
class StopPlayback:
    """The caller interrupted: stop playing now and say what played in full."""

    reply: int


@dataclass(frozen=True)
class ReplyDone:
    """Everything of this reply has been sent."""

    reply: int


@dataclass(frozen=True)
class CallEnded:
    # caller_hung_up, handed_off, listening_unavailable, time_limit, silence
    reason: str


CallEvent = (
    CallerSpeaking | Caption | CallerSaid | AgentSentence | AgentAudio
    | SegmentEnd | StopPlayback | ReplyDone | CallEnded
)


# --- what the caller's app sends besides audio ---------------------------------


@dataclass(frozen=True)
class SegmentPlayed:
    """A segment finished playing in full."""

    reply: int
    segment: int


@dataclass(frozen=True)
class PlaybackStopped:
    """The answer to StopPlayback: the segments of the reply that played in full."""

    reply: int
    played: tuple[int, ...]


class CallerLink(Protocol):
    def incoming(self) -> AsyncIterator[bytes | SegmentPlayed | PlaybackStopped]:
        """The caller's audio frames and playback reports, until they hang up."""
        ...

    async def send(self, event: CallEvent) -> None: ...


class TurnLimiter(Protocol):
    def hit(self, key: str) -> float | None:
        """None if allowed (and counted), else seconds to wait."""
        ...


# --- the call -----------------------------------------------------------------


@dataclass
class _Reply:
    number: int
    # The speaker's numbers for the sentences it was given, and those whose
    # audio has all arrived (a sentence can finish before the reply ends).
    spoken: list[int] = field(default_factory=list)
    delivered: set[int] = field(default_factory=set)
    texts: dict[int, str] = field(default_factory=dict)  # speaker number -> sentence
    # Segments whose audio was sent, their sentences, and those the caller's
    # app reported as played in full.
    segments: list[int] = field(default_factory=list)
    segment_sentences: dict[int, tuple[int, ...]] = field(default_factory=dict)
    played: set[int] = field(default_factory=set)
    words: set[str] = field(default_factory=set)  # for telling echo from interruption
    chars: int = 0
    sentences: int = 0
    ended: bool = False
    sent_done: bool = False
    interrupted: bool = False
    # For agent replies: the pending payment before the turn, and whether the
    # agent has finished (so the payment state is final).
    agent: bool = False
    pending_before: str | None = None
    agent_done: bool = False
    first_audio_at: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    stop_answer: asyncio.Future | None = None


_HUNG_UP = object()
_LISTENING_FAILED = object()
_TIME_LIMIT = object()
_SILENCE = object()

_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class _LiveCall:
    def __init__(
        self,
        session: ConversationSession,
        link: CallerLink,
        *,
        llm: LLMClient,
        db_factory: Callable[[], Session],
        system: str,
        clock: Callable[[], float],
        turn_limiter: TurnLimiter | None,
    ) -> None:
        self._session = session
        self._link = link
        self._llm = llm
        self._db_factory = db_factory
        self._system = system
        self._clock = clock
        self._turn_limiter = turn_limiter
        self._turns: asyncio.Queue = asyncio.Queue()
        self._speaker: LiveSpeaker | None = None
        self._replies: deque[_Reply] = deque()  # replies with audio still to come
        self._recent: dict[int, _Reply] = {}  # by number, for playback reports
        self._audible: _Reply | None = None  # the reply the caller may be hearing
        self._reply_count = 0
        self._sentence_count = 0
        self._started = clock()
        self._last_activity = self._started
        self._turn_span = None  # the caller turn being answered, for tracing (M6.2)

    # --- listening ---

    async def uplink(self, listener) -> None:
        try:
            async for item in self._link.incoming():
                if isinstance(item, bytes):
                    await listener.send_audio(item)
                elif isinstance(item, SegmentPlayed):
                    self._segment_played(item)
                elif isinstance(item, PlaybackStopped):
                    reply = self._recent.get(item.reply)
                    if reply is not None and reply.stop_answer is not None and not reply.stop_answer.done():
                        reply.stop_answer.set_result(item.played)
            await listener.finish()
        except SpeechUnavailableError:
            self._turns.put_nowait(_LISTENING_FAILED)

    async def listen(self, listener) -> None:
        try:
            async for event in listener.events():
                words = event.text.strip()
                if event.kind == "speech_started":
                    await self._link.send(CallerSpeaking())
                elif event.kind == "update" and words:
                    self._last_activity = self._clock()
                    await self._link.send(Caption(words))
                    await self._maybe_interrupt(words)
                elif event.kind == "end_of_turn":
                    if words:
                        self._last_activity = self._clock()
                        await self._maybe_interrupt(words)
                    self._turns.put_nowait(event)
        except SpeechUnavailableError:
            self._turns.put_nowait(_LISTENING_FAILED)
            return
        self._turns.put_nowait(_HUNG_UP)

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK_S)
            now = self._clock()
            if now - self._started >= config.LIVE_MAX_CALL_MINUTES * 60:
                self._turns.put_nowait(_TIME_LIMIT)
                return
            if now - self._last_activity >= config.LIVE_SILENCE_MINUTES * 60:
                self._turns.put_nowait(_SILENCE)
                return

    # --- interruptions ---

    async def _maybe_interrupt(self, words: str) -> None:
        reply = self._audible
        if reply is None or reply.interrupted:
            return
        heard = _words(words)
        # Words that all occur in what the agent is saying are taken to be its
        # own voice coming back through the caller's microphone.
        if not heard or heard <= reply.words:
            return
        await self._interrupt(reply)

    async def _interrupt(self, reply: _Reply) -> None:
        reply.interrupted = True
        if self._turn_span is not None:
            self._turn_span.add_event("interrupted")
        self._audible = None
        self._finish_reply(reply)  # the turn stops waiting for its audio
        if self._speaker is not None:
            try:
                await self._speaker.clear()
            except SpeechUnavailableError:
                self._speech_failed()
        if reply.segments:
            reply.stop_answer = asyncio.get_running_loop().create_future()
            await self._link.send(StopPlayback(reply.number))
            try:
                reply.played.update(await asyncio.wait_for(reply.stop_answer, STOP_ANSWER_TIMEOUT_S))
            except TimeoutError:
                logger.info("No answer to a stop in session %s; using earlier reports", self._session.session_id)
        heard = self._heard_sentences(reply)
        logger.info(
            "Caller interrupted reply %d in session %s after %d of %d sentences",
            reply.number, self._session.session_id, len(heard), reply.sentences,
        )
        await asyncio.to_thread(self._with_lock, self._session.note_interrupted, " ".join(heard))
        if reply.agent_done:
            await self._mark_readback_unheard(reply)

    def _heard_sentences(self, reply: _Reply) -> list[str]:
        """The sentences of the segments that played in full, in order."""
        numbers = sorted(
            number
            for segment in reply.played
            for number in reply.segment_sentences.get(segment, ())
        )
        return [reply.texts[number] for number in numbers if number in reply.texts]

    async def _mark_readback_unheard(self, reply: _Reply) -> None:
        """A payment proposed in this interrupted reply's turn may not have
        been heard: confirming it needs a new read-back (D82)."""

        def mark() -> None:
            with self._session.lock:
                pending = self._session.state.pending_payment
                if pending is not None and not pending.submitted and pending.request_id != reply.pending_before:
                    self._session.state.pending_payment = replace(pending, readback_heard=False)

        await asyncio.to_thread(mark)

    def _segment_played(self, report: SegmentPlayed) -> None:
        reply = self._recent.get(report.reply)
        if reply is None:
            return
        reply.played.add(report.segment)
        self._release_if_heard(reply)

    def _release_if_heard(self, reply: _Reply) -> None:
        """The caller has heard all of it: they can no longer interrupt it."""
        if reply is self._audible and reply.sent_done and set(reply.segments) <= reply.played:
            self._audible = None

    # --- speaking ---

    def _speech_failed(self) -> None:
        """The rest of the call is text only (as M4, D60)."""
        if self._speaker is None:
            return
        logger.warning("Live speech failed in session %s; continuing as text", self._session.session_id)
        self._speaker = None
        self._audible = None
        while self._replies:
            self._finish_reply(self._replies[0])

    def _finish_reply(self, reply: _Reply) -> None:
        if reply in self._replies:
            self._replies.remove(reply)
        reply.done.set()

    def _finish_if_delivered(self, reply: _Reply) -> None:
        if reply.ended and set(reply.spoken) <= reply.delivered:
            self._finish_reply(reply)

    async def downlink(self) -> None:
        speaker = self._speaker
        try:
            async for item in speaker.output():
                if not self._replies:
                    continue  # e.g. audio of an interrupted reply
                reply = self._replies[0]
                if isinstance(item, SpokenAudio):
                    if reply.first_audio_at is None:
                        reply.first_audio_at = self._clock()
                        if self._turn_span is not None:
                            self._turn_span.add_event("first_audio")
                    if item.segment not in reply.segments:
                        reply.segments.append(item.segment)
                    await self._link.send(AgentAudio(reply.number, item.segment, item.data))
                elif isinstance(item, SegmentDone):
                    reply.delivered.update(item.sentences)
                    reply.segment_sentences[item.segment] = item.sentences
                    await self._link.send(SegmentEnd(reply.number, item.segment))
                    self._finish_if_delivered(reply)
        except SpeechUnavailableError:
            self._speech_failed()

    def _new_reply(self, *, agent: bool = False) -> _Reply:
        reply = _Reply(self._reply_count, agent=agent)
        self._reply_count += 1
        self._recent[reply.number] = reply
        self._recent.pop(reply.number - 10, None)
        if agent:
            pending = self._session.state.pending_payment
            reply.pending_before = pending.request_id if pending is not None else None
        if self._speaker is not None:
            # Audible from the start: the caller may interrupt before any audio.
            self._audible = reply
        return reply

    async def _say(self, reply: _Reply, sentence: str) -> None:
        if reply.interrupted:
            return  # the caller cut it off: neither spoken nor shown
        reply.words |= _words(sentence)
        if self._speaker is not None:
            try:
                number = await self._speaker.say(sentence)
            except SpeechUnavailableError as exc:
                # An overlong sentence is shown, not spoken; anything else
                # means speech is down.
                if str(exc) != "text_too_long":
                    self._speech_failed()
            else:
                if not reply.spoken:
                    self._replies.append(reply)
                reply.spoken.append(number)
                reply.texts[number] = sentence
        reply.chars += len(sentence)
        reply.sentences += 1
        await self._link.send(AgentSentence(reply.number, self._sentence_count, sentence))
        self._sentence_count += 1

    async def _end_reply(self, reply: _Reply) -> None:
        reply.ended = True
        if self._speaker is not None and reply.spoken and not reply.interrupted:
            try:
                await self._speaker.end_reply()
            except SpeechUnavailableError:
                self._speech_failed()
        if not reply.spoken or self._speaker is None or reply.interrupted:
            self._finish_reply(reply)
        else:
            self._finish_if_delivered(reply)
        try:
            await asyncio.wait_for(reply.done.wait(), REPLY_AUDIO_TIMEOUT_S)
        except TimeoutError:
            logger.warning("Reply audio timed out in session %s", self._session.session_id)
            self._finish_reply(reply)
        reply.sent_done = True
        self._last_activity = self._clock()
        await self._link.send(ReplyDone(reply.number))
        if reply is self._audible and not reply.segments:
            self._audible = None  # nothing was played: nothing to interrupt
        self._release_if_heard(reply)

    async def _speak_fixed(self, text: str) -> _Reply:
        reply = self._new_reply()
        await self._say(reply, text)
        await self._end_reply(reply)
        return reply

    # --- turns ---

    async def take_turns(self) -> str:
        while True:
            item = await self._turns.get()
            if item is _HUNG_UP:
                return "caller_hung_up"
            if item is _LISTENING_FAILED:
                await self._speak_fixed(REPLY_GOODBYE_CANT_HEAR)
                return "listening_unavailable"
            if item is _TIME_LIMIT:
                await self._speak_fixed(REPLY_TIME_LIMIT)
                return "time_limit"
            if item is _SILENCE:
                await self._speak_fixed(REPLY_SILENCE)
                return "silence"
            await self._take_turn(item)
            if self._session.state.handoff is not None:
                return "handed_off"

    async def _take_turn(self, heard: HeardEvent) -> None:
        text = heard.text.strip()
        if not text:
            # Flux's silence timeout: nothing was said, so nothing is replied.
            return
        # One span per caller turn, from Flux's end of turn until the reply
        # has been sent; the agent's spans nest inside it.
        with telemetry.span("live.turn", session_id=self._session.session_id) as span:
            self._turn_span = span
            try:
                await self._answer(heard, text)
            finally:
                self._turn_span = None

    async def _answer(self, heard: HeardEvent, text: str) -> None:
        ended_at = self._clock()
        confidences = heard.word_confidences
        mean = statistics.fmean(confidences) if confidences else None
        lowest = min(confidences) if confidences else None

        def metric(outcome: str, reply: _Reply, first_text_at: float | None, agent_ms: int | None) -> LiveTurnMetric:
            def since_end(moment: float | None) -> int | None:
                return None if moment is None else int((moment - ended_at) * 1000)

            return LiveTurnMetric(
                session_id=self._session.session_id,
                customer_id=self._session.caller.customer_id,
                turn=self._session.state.turn,
                outcome=outcome,
                confidence_mean=mean,
                confidence_min=lowest,
                transcript_chars=len(text),
                reply_chars=reply.chars,
                sentences=reply.sentences,
                first_text_ms=since_end(first_text_at),
                first_audio_ms=since_end(reply.first_audio_at),
                agent_ms=agent_ms,
                audio_ok=reply.first_audio_at is not None,
            )

        if self._session.state.handoff is not None:
            reply = await self._speak_fixed(REPLY_HANDED_OFF)
            await self._record(metric("already_handed_off", reply, None, None))
            return

        guard = check_transcript(text, mean, min_confidence=config.LIVE_MIN_CONFIDENCE)
        if guard.reply is not None:
            # Not a caller turn (D24); the model hears about it next turn (D74).
            await asyncio.to_thread(self._with_lock, self._session.note_unheard, guard.reply)
            reply = await self._speak_fixed(guard.reply)
            await self._record(metric(guard.outcome, reply, None, None))
            return

        if self._turn_limiter is not None and self._turn_limiter.hit(self._session.caller.customer_id) is not None:
            # Shared with typed and M4 voice turns (D72); no model call.
            logger.info("Live turn rate-limited in session %s", self._session.session_id)
            telemetry.annotate(self._turn_span, outcome="rate_limited")
            await asyncio.to_thread(self._with_lock, self._session.note_unheard, REPLY_SLOW_DOWN)
            await self._speak_fixed(REPLY_SLOW_DOWN)
            return

        await self._link.send(CallerSaid(text))
        reply = self._new_reply(agent=True)
        result, first_text_at = await self._run_agent(reply, text)
        reply.agent_done = True
        if reply.interrupted:
            await self._mark_readback_unheard(reply)
        agent_ms = int((self._clock() - ended_at) * 1000)
        if result.fixed_reply:
            # Ours, not the model's (an outage, a refusal ...): never streamed.
            await self._say(reply, result.reply)
        await self._end_reply(reply)
        await self._record(metric("reply", reply, first_text_at, agent_ms))

    async def _run_agent(self, reply: _Reply, text: str) -> tuple[TurnResult, float | None]:
        """Run the agent on a worker thread, speaking each sentence as soon as
        the streamed text completes it. An interruption doesn't stop the agent
        (its tools and history stay consistent, D77); its words are dropped."""
        loop = asyncio.get_running_loop()
        pieces: asyncio.Queue = asyncio.Queue()

        def on_text(piece: str) -> None:
            loop.call_soon_threadsafe(pieces.put_nowait, piece)

        def work() -> TurnResult:
            db = self._db_factory()
            try:
                return run_turn(self._session, text, llm=self._llm, db=db, system=self._system, on_text=on_text)
            finally:
                db.close()
                loop.call_soon_threadsafe(pieces.put_nowait, None)

        agent = asyncio.create_task(asyncio.to_thread(work))
        splitter = SentenceSplitter()
        first_text_at = None
        while (piece := await pieces.get()) is not None:
            if first_text_at is None:
                first_text_at = self._clock()
            for sentence in splitter.feed(piece):
                await self._say(reply, sentence)
        result = await agent  # a bug in our code surfaces here
        rest = splitter.flush()
        if rest:
            await self._say(reply, rest)
        return result, first_text_at

    def _with_lock(self, note: Callable[[str], None], text: str) -> None:
        with self._session.lock:
            note(text)

    async def cancel_unconfirmed_payment(self) -> None:
        """At the end of a call, a payment that was read back but not
        confirmed is cancelled (M6.1): it can only be confirmed on the call
        where it was read back. Runs after the last turn has finished."""
        session = self._session

        def cancel() -> None:
            with session.lock:
                pending = session.state.pending_payment
                if pending is None or pending.submitted:
                    return
                session.state.pending_payment = None
                turn = session.state.turn
            db = self._db_factory()
            try:
                record_conversation_events(db, [ConversationEvent(
                    session_id=session.session_id, customer_id=session.caller.customer_id, turn=turn,
                    event_type="payment_cancelled", outcome="call_ended",
                )])
            finally:
                db.close()

        await asyncio.to_thread(cancel)

    async def _record(self, metric: LiveTurnMetric) -> None:
        if self._turn_span is not None:
            telemetry.annotate(
                self._turn_span, turn=metric.turn, outcome=metric.outcome, confidence_mean=metric.confidence_mean,
                confidence_min=metric.confidence_min, first_text_ms=metric.first_text_ms,
                first_audio_ms=metric.first_audio_ms, agent_ms=metric.agent_ms, audio_ok=metric.audio_ok,
            )
        def save() -> None:
            db = self._db_factory()
            try:
                record_live_turn_metric(db, metric)
            finally:
                db.close()

        await asyncio.to_thread(save)


async def run_live_call(
    session: ConversationSession,
    link: CallerLink,
    *,
    stt: LiveSpeechToText,
    tts: LiveTextToSpeech,
    llm: LLMClient,
    db_factory: Callable[[], Session],
    system: str,
    keyterms: Sequence[str] = (),
    turn_limiter: TurnLimiter | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Run one call until the caller hangs up, it is handed off, listening
    fails, or a call limit is reached. Returns the reason, which is also sent
    as CallEnded. Our own bugs propagate."""
    call = _LiveCall(
        session, link, llm=llm, db_factory=db_factory, system=system, clock=clock, turn_limiter=turn_limiter,
    )
    async with AsyncExitStack() as stack:
        try:
            listener = await stack.enter_async_context(
                stt.open(sample_rate=CALLER_SAMPLE_RATE, keyterms=keyterms)
            )
        except SpeechUnavailableError:
            reason = "listening_unavailable"
        else:
            try:
                call._speaker = await stack.enter_async_context(tts.open())
            except SpeechUnavailableError:
                logger.warning("Live speech unavailable in session %s; text only", session.session_id)
            async with asyncio.TaskGroup() as tasks:
                helpers = [
                    tasks.create_task(call.uplink(listener)),
                    tasks.create_task(call.listen(listener)),
                    tasks.create_task(call.watchdog()),
                ]
                if call._speaker is not None:
                    helpers.append(tasks.create_task(call.downlink()))
                reason = await call.take_turns()
                for helper in helpers:
                    helper.cancel()
    await call.cancel_unconfirmed_payment()
    await link.send(CallEnded(reason))
    return reason
