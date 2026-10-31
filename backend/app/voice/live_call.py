"""A live call (M5.3, D81): the caller's audio streams in, each finished turn
goes through the guard and the agent, and the reply is spoken sentence by
sentence while the model is still writing.

Transport-free: the WebSocket endpoint (5.5) supplies the `CallerLink`.
Interruptions, heard-tracking and call limits come in 5.4; here the agent
always finishes a reply, and turns that end meanwhile wait their turn.
"""

import asyncio
import logging
import statistics
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent.llm import LLMClient
from backend.app.agent.loop import REPLY_HANDED_OFF, TurnResult, run_turn
from backend.app.agent.sessions import ConversationSession
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

# A reply whose audio hasn't all arrived by then is given up on, so a lost
# Flushed can't stall the call.
REPLY_AUDIO_TIMEOUT_S = 60.0


# --- what the caller's app receives ------------------------------------------


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
class ReplyDone:
    """Everything of this reply has been sent."""

    reply: int


@dataclass(frozen=True)
class CallEnded:
    # caller_hung_up, handed_off, listening_unavailable
    reason: str


CallEvent = Caption | CallerSaid | AgentSentence | AgentAudio | ReplyDone | CallEnded


class CallerLink(Protocol):
    def audio_in(self) -> AsyncIterator[bytes]:
        """The caller's audio frames, until they hang up."""
        ...

    async def send(self, event: CallEvent) -> None: ...


# --- the call -----------------------------------------------------------------


@dataclass
class _Reply:
    number: int
    # The speaker's numbers for the sentences it was given, and those whose
    # audio has all arrived (a sentence can finish before the reply ends).
    spoken: list[int] = field(default_factory=list)
    delivered: set[int] = field(default_factory=set)
    chars: int = 0
    sentences: int = 0
    ended: bool = False
    first_audio_at: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


_HUNG_UP = object()
_LISTENING_FAILED = object()


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
    ) -> None:
        self._session = session
        self._link = link
        self._llm = llm
        self._db_factory = db_factory
        self._system = system
        self._clock = clock
        self._turns: asyncio.Queue = asyncio.Queue()
        self._speaker: LiveSpeaker | None = None
        self._replies: deque[_Reply] = deque()  # replies with audio still to come
        self._reply_count = 0
        self._sentence_count = 0

    # --- listening ---

    async def uplink(self, listener) -> None:
        try:
            async for chunk in self._link.audio_in():
                await listener.send_audio(chunk)
            await listener.finish()
        except SpeechUnavailableError:
            self._turns.put_nowait(_LISTENING_FAILED)

    async def listen(self, listener) -> None:
        try:
            async for event in listener.events():
                if event.kind == "update" and event.text:
                    await self._link.send(Caption(event.text))
                elif event.kind == "end_of_turn":
                    self._turns.put_nowait(event)
        except SpeechUnavailableError:
            self._turns.put_nowait(_LISTENING_FAILED)
            return
        self._turns.put_nowait(_HUNG_UP)

    # --- speaking ---

    def _speech_failed(self) -> None:
        """The rest of the call is text only (as M4, D60)."""
        if self._speaker is None:
            return
        logger.warning("Live speech failed in session %s; continuing as text", self._session.session_id)
        self._speaker = None
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
                    continue
                reply = self._replies[0]
                if isinstance(item, SpokenAudio):
                    if reply.first_audio_at is None:
                        reply.first_audio_at = self._clock()
                    await self._link.send(AgentAudio(reply.number, item.segment, item.data))
                elif isinstance(item, SegmentDone):
                    reply.delivered.update(item.sentences)
                    self._finish_if_delivered(reply)
        except SpeechUnavailableError:
            self._speech_failed()

    def _new_reply(self) -> _Reply:
        reply = _Reply(self._reply_count)
        self._reply_count += 1
        return reply

    async def _say(self, reply: _Reply, sentence: str) -> None:
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
        reply.chars += len(sentence)
        reply.sentences += 1
        await self._link.send(AgentSentence(reply.number, self._sentence_count, sentence))
        self._sentence_count += 1

    async def _end_reply(self, reply: _Reply) -> None:
        reply.ended = True
        if self._speaker is not None and reply.spoken:
            try:
                await self._speaker.end_reply()
            except SpeechUnavailableError:
                self._speech_failed()
        if not reply.spoken or self._speaker is None:
            self._finish_reply(reply)
        else:
            self._finish_if_delivered(reply)
        try:
            await asyncio.wait_for(reply.done.wait(), REPLY_AUDIO_TIMEOUT_S)
        except TimeoutError:
            logger.warning("Reply audio timed out in session %s", self._session.session_id)
            self._finish_reply(reply)
        await self._link.send(ReplyDone(reply.number))

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
            await self._take_turn(item)
            if self._session.state.handoff is not None:
                return "handed_off"

    async def _take_turn(self, heard: HeardEvent) -> None:
        text = heard.text.strip()
        if not text:
            # Flux's silence timeout: nothing was said, so nothing is replied.
            return
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
            await asyncio.to_thread(self._note_unheard, guard.reply)
            reply = await self._speak_fixed(guard.reply)
            await self._record(metric(guard.outcome, reply, None, None))
            return

        await self._link.send(CallerSaid(text))
        reply = self._new_reply()
        result, first_text_at = await self._run_agent(reply, text)
        agent_ms = int((self._clock() - ended_at) * 1000)
        if result.fixed_reply:
            # Ours, not the model's (an outage, a refusal ...): never streamed.
            await self._say(reply, result.reply)
        await self._end_reply(reply)
        await self._record(metric("reply", reply, first_text_at, agent_ms))

    async def _run_agent(self, reply: _Reply, text: str) -> tuple[TurnResult, float | None]:
        """Run the agent on a worker thread, speaking each sentence as soon as
        the streamed text completes it."""
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

    def _note_unheard(self, reply: str) -> None:
        with self._session.lock:
            self._session.note_unheard(reply)

    async def _record(self, metric: LiveTurnMetric) -> None:
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
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Run one call until the caller hangs up, it is handed off, or listening
    fails. Returns the reason, which is also sent as CallEnded. Our own bugs
    propagate."""
    call = _LiveCall(session, link, llm=llm, db_factory=db_factory, system=system, clock=clock)
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
                ]
                if call._speaker is not None:
                    helpers.append(tasks.create_task(call.downlink()))
                reason = await call.take_turns()
                for helper in helpers:
                    helper.cancel()
    await link.send(CallEnded(reason))
    return reason
