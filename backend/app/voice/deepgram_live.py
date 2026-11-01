"""Deepgram live speech behind the live_speech contracts (M5.2, D79): Flux
(`/v2/listen`) for listening and a speak WebSocket for speaking: Aura-2
(`/v1/speak`) for English, or a Flux voice (`/v2/speak`) such as Hindi's
`flux-rahul-hi` (M7.6). The two speak sockets work alike; only their
message names differ (_SpeakProtocol).

Messages are told apart by their `type` field, not their class: the SDK
(deepgram-sdk 7.16.0) parses the speak socket's "Cleared" and "Warning"
messages as SpeakV1Flushed objects, because those types accept any value.
"""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError
from deepgram.speak.v1.types import SpeakV1Text
from deepgram.speak.v2.types import SpeakV2Speak
from websockets.exceptions import ConnectionClosed

from backend.app.voice.deepgram import MAX_TTS_CHARS, _is_transient
from backend.app.voice.live_speech import AudioFormat, HeardEvent, SegmentDone, SpokenAudio
from backend.app.voice.speech import SpeechUnavailableError

logger = logging.getLogger(__name__)

LIVE_TTS_SAMPLE_RATE = 24_000
# After an interruption, audio is discarded until the socket confirms it. A
# confirmation that never comes mustn't silence the next reply (M7.6).
INTERRUPT_WAIT_S = 2.0

# Failing to reach or keep a socket: worth a "try again". An ApiError from the
# handshake is checked by status like the REST adapters (D66).
_NETWORK_ERRORS = (OSError, asyncio.TimeoutError, ConnectionClosed)

# Flux turn events we use; EagerEndOfTurn and TurnResumed only come when
# eager end-of-turn is enabled, which it isn't (D77). New events are ignored.
_FLUX_EVENTS = {"StartOfTurn": "speech_started", "Update": "update", "EndOfTurn": "end_of_turn"}


def _unavailable(service: str, exc: BaseException | None) -> SpeechUnavailableError:
    reason = type(exc).__name__ if exc is not None else "closed"
    logger.warning("Deepgram %s unavailable: %s", service, reason)
    return SpeechUnavailableError(reason)


async def _enter(stack: AsyncExitStack, connection: AbstractAsyncContextManager, service: str) -> Any:
    """Open a socket; our own mistakes (400, 401, 403 ...) raise unchanged."""
    try:
        return await stack.enter_async_context(connection)
    except ApiError as exc:
        if not _is_transient(exc):
            raise
        raise _unavailable(service, exc) from exc
    except _NETWORK_ERRORS as exc:
        raise _unavailable(service, exc) from exc


# --- listening ---------------------------------------------------------------


class _FluxListener:
    def __init__(self, socket: Any) -> None:
        self._socket = socket

    async def send_audio(self, chunk: bytes) -> None:
        try:
            await self._socket.send_media(chunk)
        except ConnectionClosed as exc:
            raise _unavailable("live speech-to-text", exc) from exc

    async def events(self) -> AsyncIterator[HeardEvent]:
        try:
            async for message in self._socket:
                kind = getattr(message, "type", None)
                if kind == "TurnInfo":
                    event = _FLUX_EVENTS.get(message.event)
                    if event is None:
                        continue
                    confidences = (
                        tuple(word.confidence for word in message.words) if event == "end_of_turn" else ()
                    )
                    yield HeardEvent(event, message.transcript, confidences)
                elif kind == "Error":
                    # The code only: descriptions can echo request details.
                    logger.warning("Flux error %s", message.code)
                    raise SpeechUnavailableError(f"flux_{message.code}")
                elif kind == "Warning":
                    logger.warning("Flux warning %s", message.code)
        except ConnectionClosed as exc:
            raise _unavailable("live speech-to-text", exc) from exc

    async def finish(self) -> None:
        try:
            await self._socket.send_close_stream()
        except ConnectionClosed:
            pass  # already closed: nothing left to finish


class DeepgramLiveSTT:
    def __init__(
        self,
        client: AsyncDeepgramClient,
        *,
        model: str,
        eot_threshold: float,
        eot_timeout_ms: int,
        language_hints: Sequence[str] = (),
    ) -> None:
        self._client = client
        self._model = model
        self._eot_threshold = eot_threshold
        self._eot_timeout_ms = eot_timeout_ms
        # Languages to expect, e.g. ("hi", "en") for Hinglish (M7.6). Only the
        # multilingual model takes them (flux-general-en refuses with a 400).
        self._language_hints = tuple(language_hints)

    @asynccontextmanager
    async def open(self, *, sample_rate: int, keyterms: Sequence[str] = (),
                   encoding: str = "linear16") -> AsyncIterator[_FluxListener]:
        async with AsyncExitStack() as stack:
            socket = await _enter(
                stack,
                self._client.listen.v2.connect(
                    model=self._model,
                    encoding=encoding,
                    sample_rate=sample_rate,
                    eot_threshold=self._eot_threshold,
                    eot_timeout_ms=self._eot_timeout_ms,
                    # Strings: these go into the URL, where Python's True
                    # would read "True".
                    numerals="true",
                    mip_opt_out="true",
                    **({"keyterm": list(keyterms)} if keyterms else {}),
                    **({"language_hint": list(self._language_hints)} if self._language_hints else {}),
                ),
                "live speech-to-text",
            )
            yield _FluxListener(socket)


# --- speaking ----------------------------------------------------------------


@dataclass
class _Segment:
    number: int
    sentences: list[int] = field(default_factory=list)
    # A Flux voice's id for this segment's speech, from SpeechStarted.
    speech_id: str | None = None


_CLOSED = object()


@dataclass(frozen=True)
class _SpeakProtocol:
    """What differs between the speak sockets: how a sentence is sent, how
    queued speech is dropped and the message confirming it was, and which
    message says a flushed segment's audio has all arrived."""

    name: str
    send_sentence: Callable[[Any, str], Any]
    send_interrupt: Callable[[Any], Any]
    interrupted: str
    done: str
    # A Flux voice names each speech (SpeechStarted, then SpeechMetadata with
    # the same id), so a segment completes only on its own speech's message.
    started: str | None = None


AURA = _SpeakProtocol("Aura", lambda socket, text: socket.send_text(SpeakV1Text(text=text)),
                      lambda socket: socket.send_clear(), "Cleared", done="Flushed")
# /v2/speak (D111, Deepgram's flux-tts docs): Flushed comes before the last
# audio; SpeechMetadata is "no more audio is coming". It adds no whitespace
# between Speak messages, so each sentence ends with a space.
FLUX_VOICE = _SpeakProtocol("Flux voice", lambda socket, text: socket.send_speak(SpeakV2Speak(text=text + " ")),
                            lambda socket: socket.send_interrupt(), "SpeechInterrupted",
                            done="SpeechMetadata", started="SpeechStarted")


def speak_protocol(voice: str) -> _SpeakProtocol:
    """Flux voices (flux-*) are on /v2/speak, Aura's on /v1/speak."""
    return FLUX_VOICE if voice.startswith("flux-") else AURA


class _Speaker:
    """One speak socket (Aura or a Flux voice) for one call.

    Aura caps flushes at 20 per minute (assumed for Flux voices too until
    measured), so a reply's first sentence is
    flushed at once (fastest first audio) and the rest together at
    end_reply(): at most two per reply. Our own queue of open segments says
    which reply audio belongs to; each Flushed completes the oldest.
    """

    def __init__(self, connect: Callable[[], AbstractAsyncContextManager], sample_rate: int,
                 protocol: _SpeakProtocol = AURA) -> None:
        self.sample_rate = sample_rate
        self._connect = connect
        self._protocol = protocol
        self._interrupt_timer: asyncio.Task | None = None
        self._stack: AsyncExitStack | None = None
        self._socket: Any = None
        self._reader: asyncio.Task | None = None
        self._output: asyncio.Queue = asyncio.Queue()
        self._open: deque[_Segment] = deque()  # sent, audio not complete yet
        self._rest: _Segment | None = None  # the current reply's unflushed sentences
        self._reply_started = False
        self._discarding = False  # between clear() and the socket confirming it
        self._sentences = 0
        self._segments = 0

    def _new_segment(self, sentence: int) -> _Segment:
        segment = _Segment(self._segments, [sentence])
        self._segments += 1
        self._open.append(segment)
        return segment

    def _forget_reply(self) -> None:
        self._open.clear()
        self._rest = None
        self._reply_started = False

    async def connect(self) -> None:
        if self._socket is not None:
            return
        stack = AsyncExitStack()
        try:
            socket = await _enter(stack, self._connect(), "live text-to-speech")
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self._socket = stack, socket
        self._reader = asyncio.create_task(self._read(socket))

    async def _read(self, socket: Any) -> None:
        error: BaseException | None = None
        try:
            async for message in socket:
                if isinstance(message, bytes):
                    if self._discarding:
                        continue
                    if self._open:
                        self._output.put_nowait(SpokenAudio(self._open[0].number, message))
                    else:
                        logger.warning("%s audio with no reply waiting; dropped", self._protocol.name)
                    continue
                kind = getattr(message, "type", None)
                if kind == self._protocol.interrupted:
                    self._discarding = False
                elif kind == self._protocol.started:
                    # The oldest segment without a speech yet is this one.
                    waiting = next((s for s in self._open if s.speech_id is None), None)
                    if waiting is not None and not self._discarding:
                        waiting.speech_id = message.speech_id
                elif kind == self._protocol.done:
                    if self._discarding or not self._open:
                        continue
                    if self._protocol.started is not None and self._open[0].speech_id != message.speech_id:
                        continue  # another speech's (an interrupted one's): not this segment's end
                    done = self._open.popleft()
                    self._output.put_nowait(SegmentDone(done.number, tuple(done.sentences)))
                elif kind == "Warning":
                    logger.warning("%s warning %s", self._protocol.name, message.code)
                elif kind == "Error":
                    # The code only: the description may quote the text.
                    logger.warning("%s error %s", self._protocol.name, message.code)
                    if self._open:  # this reply's audio will never come
                        self._forget_reply()
                        self._output.put_nowait(SpeechUnavailableError(f"tts_{message.code}"))
        except ConnectionClosed as exc:
            error = exc
        # The socket closed. Idle, that's harmless: the next say reopens it.
        # Mid-reply, the rest of the reply will never come.
        if self._socket is socket:
            self._socket = None
            if self._open:
                self._forget_reply()
                self._output.put_nowait(_unavailable("live text-to-speech", error))

    async def say(self, sentence: str) -> int:
        if len(sentence) > MAX_TTS_CHARS:
            raise SpeechUnavailableError("text_too_long")
        await self.connect()
        number = self._sentences
        self._sentences += 1
        # Bookkeeping first: audio can arrive while a send is in progress.
        first = not self._reply_started
        if first:
            self._new_segment(number)
            self._reply_started = True
        elif self._rest is None:
            self._rest = self._new_segment(number)
        else:
            self._rest.sentences.append(number)
        try:
            await self._protocol.send_sentence(self._socket, sentence)
            if first:
                await self._socket.send_flush()
        except ConnectionClosed as exc:
            raise _unavailable("live text-to-speech", exc) from exc
        return number

    async def end_reply(self) -> None:
        rest, self._rest = self._rest, None
        self._reply_started = False
        if rest is not None and self._socket is not None:
            try:
                await self._socket.send_flush()
            except ConnectionClosed as exc:
                raise _unavailable("live text-to-speech", exc) from exc

    async def clear(self) -> None:
        speaking = bool(self._open)  # audio may still be on its way
        self._forget_reply()
        if self._socket is None:
            return
        # Everything until the socket confirms belongs to the cleared reply.
        # With nothing open there's nothing new to drop, and a Flux voice sends
        # no confirmation then (D111): only an earlier interruption's wait, if
        # any, carries on.
        if speaking:
            self._discarding = True
        try:
            await self._protocol.send_interrupt(self._socket)
        except ConnectionClosed:
            self._discarding = False  # nothing more will arrive anyway
            return
        # One wait at a time (a new one may only lengthen it); a wait with
        # nothing to drop, or outliving its confirmation, finds nothing to undo.
        if self._interrupt_timer is not None:
            self._interrupt_timer.cancel()
        self._interrupt_timer = asyncio.create_task(self._give_up_waiting())

    async def _give_up_waiting(self) -> None:
        await asyncio.sleep(INTERRUPT_WAIT_S)
        if self._discarding:
            logger.warning("%s never confirmed an interruption; speaking again", self._protocol.name)
            self._discarding = False


    async def output(self) -> AsyncIterator[SpokenAudio | SegmentDone]:
        while True:
            item = await self._output.get()
            if item is _CLOSED:
                return
            if isinstance(item, SpeechUnavailableError):
                raise item
            yield item

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                await socket.send_close()
            except ConnectionClosed:
                pass
        if self._stack is not None:
            await self._stack.aclose()
        if self._reader is not None:
            self._reader.cancel()
        if self._interrupt_timer is not None:
            self._interrupt_timer.cancel()
        self._output.put_nowait(_CLOSED)


class DeepgramLiveTTS:
    def __init__(
        self,
        client: AsyncDeepgramClient,
        *,
        voice: str,
        sample_rate: int = LIVE_TTS_SAMPLE_RATE,
    ) -> None:
        self._client = client
        self._voice = voice
        self._protocol = speak_protocol(voice)
        self.sample_rate = sample_rate

    @asynccontextmanager
    async def open(self, audio: AudioFormat | None = None) -> AsyncIterator[_Speaker]:
        audio = audio or AudioFormat("linear16", self.sample_rate)
        speak = self._client.speak.v2 if self._protocol is FLUX_VOICE else self._client.speak.v1

        def connection() -> AbstractAsyncContextManager:
            return speak.connect(
                model=self._voice,
                encoding=audio.encoding,
                sample_rate=audio.sample_rate,
                mip_opt_out="true",
            )

        speaker = _Speaker(connection, audio.sample_rate, self._protocol)
        # Opened at the start of the call, so the first reply doesn't wait
        # for the handshake.
        await speaker.connect()
        try:
            yield speaker
        finally:
            await speaker.close()


def build_deepgram_live_speech(
    *, stt_model: str, tts_voice: str, eot_threshold: float, eot_timeout_ms: int, api_key: str,
    language_hints: Sequence[str] = (),
) -> tuple[DeepgramLiveSTT, DeepgramLiveTTS]:
    """One async SDK client for both. Creating it makes no request."""
    client = AsyncDeepgramClient(api_key=api_key)
    return (
        DeepgramLiveSTT(client, model=stt_model, eot_threshold=eot_threshold, eot_timeout_ms=eot_timeout_ms,
                        language_hints=language_hints),
        DeepgramLiveTTS(client, voice=tts_voice),
    )
