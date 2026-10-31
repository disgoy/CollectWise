"""Deepgram live speech behind the live_speech contracts (M5.2, D79): Flux
(`/v2/listen`) for listening and the Aura-2 WebSocket for speaking.

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
from websockets.exceptions import ConnectionClosed

from backend.app.voice.deepgram import MAX_TTS_CHARS, _is_transient
from backend.app.voice.live_speech import HeardEvent, SegmentDone, SpokenAudio
from backend.app.voice.speech import SpeechUnavailableError

logger = logging.getLogger(__name__)

LIVE_TTS_SAMPLE_RATE = 24_000

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
    ) -> None:
        self._client = client
        self._model = model
        self._eot_threshold = eot_threshold
        self._eot_timeout_ms = eot_timeout_ms

    @asynccontextmanager
    async def open(self, *, sample_rate: int, keyterms: Sequence[str] = ()) -> AsyncIterator[_FluxListener]:
        async with AsyncExitStack() as stack:
            socket = await _enter(
                stack,
                self._client.listen.v2.connect(
                    model=self._model,
                    encoding="linear16",
                    sample_rate=sample_rate,
                    eot_threshold=self._eot_threshold,
                    eot_timeout_ms=self._eot_timeout_ms,
                    # Strings: these go into the URL, where Python's True
                    # would read "True".
                    numerals="true",
                    mip_opt_out="true",
                    **({"keyterm": list(keyterms)} if keyterms else {}),
                ),
                "live speech-to-text",
            )
            yield _FluxListener(socket)


# --- speaking ----------------------------------------------------------------


@dataclass
class _Segment:
    number: int
    sentences: list[int] = field(default_factory=list)


_CLOSED = object()


class _AuraSpeaker:
    """One Aura socket for one call.

    Flushes are capped at 20 per minute, so a reply's first sentence is
    flushed at once (fastest first audio) and the rest together at
    end_reply(): at most two per reply. Our own queue of open segments says
    which reply audio belongs to; each Flushed completes the oldest.
    """

    def __init__(self, connect: Callable[[], AbstractAsyncContextManager], sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self._connect = connect
        self._stack: AsyncExitStack | None = None
        self._socket: Any = None
        self._reader: asyncio.Task | None = None
        self._output: asyncio.Queue = asyncio.Queue()
        self._open: deque[_Segment] = deque()  # sent, audio not complete yet
        self._rest: _Segment | None = None  # the current reply's unflushed sentences
        self._reply_started = False
        self._discarding = False  # between clear() and Deepgram's Cleared
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
                        logger.warning("Aura audio with no reply waiting; dropped")
                    continue
                kind = getattr(message, "type", None)
                if kind == "Cleared":
                    self._discarding = False
                elif kind == "Flushed":
                    if self._discarding or not self._open:
                        continue
                    done = self._open.popleft()
                    self._output.put_nowait(SegmentDone(done.number, tuple(done.sentences)))
                elif kind == "Warning":
                    logger.warning("Aura warning %s", message.code)
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
            await self._socket.send_text(SpeakV1Text(text=sentence))
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
        self._forget_reply()
        if self._socket is None:
            return
        # Everything until Deepgram's Cleared belongs to the cleared reply.
        self._discarding = True
        try:
            await self._socket.send_clear()
        except ConnectionClosed:
            self._discarding = False  # nothing more will arrive anyway

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
        self.sample_rate = sample_rate

    def _connection(self) -> AbstractAsyncContextManager:
        return self._client.speak.v1.connect(
            model=self._voice,
            encoding="linear16",
            sample_rate=self.sample_rate,
            mip_opt_out="true",
        )

    @asynccontextmanager
    async def open(self) -> AsyncIterator[_AuraSpeaker]:
        speaker = _AuraSpeaker(self._connection, self.sample_rate)
        # Opened at the start of the call, so the first reply doesn't wait
        # for the handshake.
        await speaker.connect()
        try:
            yield speaker
        finally:
            await speaker.close()


def build_deepgram_live_speech(
    *, stt_model: str, tts_voice: str, eot_threshold: float, eot_timeout_ms: int, api_key: str
) -> tuple[DeepgramLiveSTT, DeepgramLiveTTS]:
    """One async SDK client for both. Creating it makes no request."""
    client = AsyncDeepgramClient(api_key=api_key)
    return (
        DeepgramLiveSTT(client, model=stt_model, eot_threshold=eot_threshold, eot_timeout_ms=eot_timeout_ms),
        DeepgramLiveTTS(client, voice=tts_voice),
    )
