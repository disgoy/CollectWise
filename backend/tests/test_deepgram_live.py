"""Deepgram live speech adapters (M5.2): Flux listening and the Aura socket.

The fake sockets parse every message with the SDK's own parser, so the
tests see what the real socket hands over, quirks included (the speak socket
turns "Cleared" and "Warning" into SpeakV1Flushed objects).
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from deepgram.core.api_error import ApiError
from deepgram.core.unchecked_base_model import construct_type
from deepgram.listen.v2.socket_client import V2SocketClientResponse
from deepgram.speak.v1.socket_client import V1SocketClientResponse
from websockets.exceptions import ConnectionClosed

from backend.app.voice.deepgram import MAX_TTS_CHARS
from backend.app.voice.deepgram_live import DeepgramLiveSTT, DeepgramLiveTTS
from backend.app.voice.live_speech import HeardEvent, SegmentDone, SpokenAudio
from backend.app.voice.speech import SpeechUnavailableError

END = object()


def dropped() -> ConnectionClosed:
    return ConnectionClosed(None, None)


def turn(event: str, transcript: str = "", words: list[tuple[str, float]] = ()) -> dict:
    return {
        "type": "TurnInfo", "request_id": "r", "sequence_id": 1, "event": event, "turn_index": 0,
        "audio_window_start": 0.0, "audio_window_end": 1.0, "transcript": transcript,
        "words": [{"word": w, "confidence": c} for w, c in words], "end_of_turn_confidence": 0.9,
    }


class FakeFluxSocket:
    def __init__(self, messages: list) -> None:
        self.messages = messages
        self.audio: list[bytes] = []
        self.closed_stream = False
        self.broken = False

    async def __aiter__(self):
        for message in self.messages:
            if isinstance(message, BaseException):
                raise message
            yield construct_type(type_=V2SocketClientResponse, object_=message)

    async def send_media(self, chunk: bytes) -> None:
        if self.broken:
            raise dropped()
        self.audio.append(chunk)

    async def send_close_stream(self) -> None:
        if self.broken:
            raise dropped()
        self.closed_stream = True


class FakeAuraSocket:
    """Answers like Aura: audio and Flushed for each flush, Cleared for a clear."""

    def __init__(self, *, stale_audio_after_clear: bool = False) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[tuple[str, str]] = []
        self.pending = ""
        self.flushes = 0
        self.stale_audio_after_clear = stale_audio_after_clear

    async def __aiter__(self):
        while True:
            item = await self.incoming.get()
            if item is END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item if isinstance(item, bytes) else construct_type(type_=V1SocketClientResponse, object_=item)

    def push(self, item) -> None:
        self.incoming.put_nowait(item)

    async def send_text(self, message) -> None:
        self.sent.append(("text", message.text))
        self.pending += message.text

    async def send_flush(self) -> None:
        self.sent.append(("flush", ""))
        audio = self.pending.encode()
        self.pending = ""
        self.push(audio[: len(audio) // 2])
        self.push(audio[len(audio) // 2:])
        self.push({"type": "Flushed", "sequence_id": self.flushes})
        self.flushes += 1

    async def send_clear(self) -> None:
        self.sent.append(("clear", ""))
        self.pending = ""
        if self.stale_audio_after_clear:
            self.push(b"stale audio")
            self.push({"type": "Flushed", "sequence_id": 99})
        self.push({"type": "Cleared", "sequence_id": self.flushes})

    async def send_close(self) -> None:
        self.sent.append(("close", ""))
        self.push(END)


class FakeConnector:
    """Stands in for client.listen.v2.connect / client.speak.v1.connect."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)

        @asynccontextmanager
        async def connection():
            if isinstance(outcome, BaseException):
                raise outcome
            yield outcome

        return connection()


def client(listen=None, speak=None) -> SimpleNamespace:
    return SimpleNamespace(
        listen=SimpleNamespace(v2=SimpleNamespace(connect=listen)),
        speak=SimpleNamespace(v1=SimpleNamespace(connect=speak)),
    )


def stt(connector: FakeConnector) -> DeepgramLiveSTT:
    return DeepgramLiveSTT(client(listen=connector), model="flux-general-en", eot_threshold=0.7, eot_timeout_ms=5000)


def tts(connector: FakeConnector) -> DeepgramLiveTTS:
    return DeepgramLiveTTS(client(speak=connector), voice="aura-2-thalia-en")


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, timeout=5))


async def collect_events(listener) -> list[HeardEvent]:
    return [event async for event in listener.events()]


HANDSHAKE_FAILURES = [
    (ApiError(status_code=503, headers={}, body=""), SpeechUnavailableError),
    (ApiError(status_code=429, headers={}, body=""), SpeechUnavailableError),
    (OSError("network unreachable"), SpeechUnavailableError),
    (asyncio.TimeoutError(), SpeechUnavailableError),
    (ApiError(status_code=401, headers={}, body=""), ApiError),
    (ApiError(status_code=400, headers={}, body=""), ApiError),
]
HANDSHAKE_IDS = ["503", "429", "network", "timeout", "401-bad-key", "400-bad-request"]


# --- listening ----------------------------------------------------------------


def test_flux_is_opened_with_our_settings() -> None:
    connector = FakeConnector([FakeFluxSocket([]), FakeFluxSocket([])])

    async def go():
        async with stt(connector).open(sample_rate=16_000, keyterms=["main", "savings"]):
            pass
        async with stt(connector).open(sample_rate=16_000):
            pass

    run(go())

    assert connector.calls[0] == {
        "model": "flux-general-en", "encoding": "linear16", "sample_rate": 16_000,
        "eot_threshold": 0.7, "eot_timeout_ms": 5000, "numerals": "true", "mip_opt_out": "true",
        "keyterm": ["main", "savings"],
    }
    assert "keyterm" not in connector.calls[1]


def test_flux_events_become_our_three_kinds() -> None:
    socket = FakeFluxSocket([
        {"type": "Connected", "request_id": "r", "sequence_id": 0},
        turn("StartOfTurn"),
        turn("Update", "Pay one", [("pay", 0.9), ("one", 0.8)]),
        turn("EagerEndOfTurn", "Pay one twenty"),
        turn("TurnResumed", "Pay one twenty"),
        {"type": "Warning", "request_id": "r", "sequence_id": 5, "code": "W1", "description": "slow"},
        turn("SomethingNew", "x"),
        turn("EndOfTurn", "Pay 120.", [("pay", 0.95), ("120", 0.62)]),
    ])

    async def go():
        async with stt(FakeConnector([socket])).open(sample_rate=16_000) as listener:
            return await collect_events(listener)

    assert run(go()) == [
        HeardEvent("speech_started", ""),
        HeardEvent("update", "Pay one"),
        HeardEvent("end_of_turn", "Pay 120.", (0.95, 0.62)),
    ]


def test_a_flux_error_is_an_outage_and_only_its_code_is_logged(caplog) -> None:
    socket = FakeFluxSocket([
        {"type": "Error", "sequence_id": 1, "code": "INTERNAL", "description": "caller said something private"},
    ])

    async def go():
        async with stt(FakeConnector([socket])).open(sample_rate=16_000) as listener:
            await collect_events(listener)

    with caplog.at_level(logging.WARNING), pytest.raises(SpeechUnavailableError, match="flux_INTERNAL"):
        run(go())
    assert "INTERNAL" in caplog.text
    assert "private" not in caplog.text


def test_a_dropped_flux_connection_is_an_outage() -> None:
    socket = FakeFluxSocket([turn("StartOfTurn"), dropped()])

    async def go():
        async with stt(FakeConnector([socket])).open(sample_rate=16_000) as listener:
            await collect_events(listener)

    with pytest.raises(SpeechUnavailableError):
        run(go())


def test_audio_goes_through_and_finish_closes_the_stream() -> None:
    socket = FakeFluxSocket([])

    async def go():
        async with stt(FakeConnector([socket])).open(sample_rate=16_000) as listener:
            await listener.send_audio(b"\x00\x01" * 1280)
            await listener.finish()

    run(go())

    assert socket.audio == [b"\x00\x01" * 1280]
    assert socket.closed_stream is True


def test_sending_on_a_dropped_flux_connection_is_an_outage_but_finish_is_quiet() -> None:
    socket = FakeFluxSocket([])
    socket.broken = True

    async def go():
        async with stt(FakeConnector([socket])).open(sample_rate=16_000) as listener:
            await listener.finish()
            await listener.send_audio(b"\x00\x00")

    with pytest.raises(SpeechUnavailableError):
        run(go())


@pytest.mark.parametrize(("failure", "raised"), HANDSHAKE_FAILURES, ids=HANDSHAKE_IDS)
def test_flux_handshake_failures(failure, raised) -> None:
    async def go():
        async with stt(FakeConnector([failure])).open(sample_rate=16_000):
            pass

    with pytest.raises(raised):
        run(go())


# --- speaking -----------------------------------------------------------------


async def take_until_done(speaker, segment: int) -> list:
    items = []
    async for item in speaker.output():
        items.append(item)
        if isinstance(item, SegmentDone) and item.segment == segment:
            return items
    return items


async def take(speaker, count: int) -> list:
    items = []
    output = speaker.output()
    while len(items) < count:
        items.append(await anext(output))
    return items


def test_the_aura_socket_opens_at_the_start_of_the_call_with_our_settings() -> None:
    connector = FakeConnector([FakeAuraSocket()])

    async def go():
        async with tts(connector).open() as speaker:
            assert speaker.sample_rate == 24_000
            assert len(connector.calls) == 1  # before anything is said

    run(go())

    assert connector.calls == [
        {"model": "aura-2-thalia-en", "encoding": "linear16", "sample_rate": 24_000, "mip_opt_out": "true"}
    ]


def test_a_one_sentence_reply_is_flushed_at_once() -> None:
    socket = FakeAuraSocket()

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            number = await speaker.say("Hello there.")
            await speaker.end_reply()
            return number, await take(speaker, 3)

    number, items = run(go())

    assert number == 0
    assert socket.sent == [("text", "Hello there."), ("flush", ""), ("close", "")]
    assert items == [SpokenAudio(0, b"Hello "), SpokenAudio(0, b"there."), SegmentDone(0, (0,))]


def test_a_longer_reply_uses_two_flushes_first_sentence_then_the_rest() -> None:
    socket = FakeAuraSocket()

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            for sentence in ("One.", "Two.", "Three."):
                await speaker.say(sentence)
            await speaker.end_reply()
            return await take(speaker, 6)

    items = run(go())

    assert socket.sent[:-1] == [("text", "One."), ("flush", ""), ("text", "Two."), ("text", "Three."), ("flush", "")]
    assert [item for item in items if isinstance(item, SegmentDone)] == [SegmentDone(0, (0,)), SegmentDone(1, (1, 2))]
    assert b"".join(i.data for i in items if isinstance(i, SpokenAudio) and i.segment == 1) == b"Two.Three."


def test_audio_for_the_rest_arriving_before_its_flush_belongs_to_the_rest() -> None:
    socket = FakeAuraSocket()

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("One.")
            await speaker.say("Two.")
            first = await take(speaker, 3)
            socket.push(b"early")  # Aura started on "Two." before the flush
            return first, await take(speaker, 1)

    _, [early] = run(go())

    assert early == SpokenAudio(1, b"early")


def test_a_warning_never_completes_a_segment() -> None:
    # The SDK parses Warning as a SpeakV1Flushed object; only `type` tells.
    socket = FakeAuraSocket()

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            socket.push({"type": "Warning", "sequence_id": 0, "code": "FLUSH_LIMIT", "description": "x"})
            await speaker.say("Hello.")
            await speaker.end_reply()
            return await take(speaker, 3)

    items = run(go())

    assert items[-1] == SegmentDone(0, (0,))
    assert all(isinstance(item, SpokenAudio) for item in items[:-1])


def test_clear_discards_everything_until_cleared_then_the_next_reply_works() -> None:
    socket = FakeAuraSocket(stale_audio_after_clear=True)

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("A long first reply.")
            await speaker.clear()
            number = await speaker.say("Next.")
            await speaker.end_reply()
            return number, await take_until_done(speaker, 1)

    number, items = run(go())

    # Here the cleared reply's audio, its Flushed and the stale audio all
    # arrive after the clear, so none of it comes out; the next reply does.
    assert number == 1
    assert items == [SpokenAudio(1, b"Ne"), SpokenAudio(1, b"xt."), SegmentDone(1, (1,))]


def test_clear_drops_the_open_reply_so_end_reply_sends_no_flush() -> None:
    socket = FakeAuraSocket()

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("One.")
            await speaker.say("Two.")
            await speaker.clear()
            await speaker.end_reply()

    run(go())

    assert socket.sent.count(("flush", "")) == 1


def test_an_idle_close_is_quiet_and_the_next_sentence_reconnects() -> None:
    first, second = FakeAuraSocket(), FakeAuraSocket()
    connector = FakeConnector([first, second])

    async def go():
        async with tts(connector).open() as speaker:
            first.push(END)  # Deepgram closed the idle socket
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await speaker.say("Still there?")
            await speaker.end_reply()
            return await take(speaker, 3)

    items = run(go())

    assert len(connector.calls) == 2
    assert second.sent[:2] == [("text", "Still there?"), ("flush", "")]
    assert items[-1] == SegmentDone(0, (0,))


def test_a_drop_mid_reply_is_an_outage() -> None:
    socket = FakeAuraSocket()

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("One.")
            await speaker.say("Two.")
            await take(speaker, 3)
            socket.push(dropped())
            await take(speaker, 1)

    with pytest.raises(SpeechUnavailableError):
        run(go())


def test_a_sentence_over_the_limit_is_refused_before_sending() -> None:
    socket = FakeAuraSocket()

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("x" * (MAX_TTS_CHARS + 1))

    with pytest.raises(SpeechUnavailableError, match="text_too_long"):
        run(go())
    assert [kind for kind, _ in socket.sent] == ["close"]


def test_sentence_numbers_keep_counting_across_replies() -> None:
    socket = FakeAuraSocket()

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            numbers = [await speaker.say("A."), await speaker.say("B.")]
            await speaker.end_reply()
            numbers.append(await speaker.say("C."))
            return numbers

    assert run(go()) == [0, 1, 2]
    assert socket.sent.count(("flush", "")) == 3  # A, then B as the rest, then C


def test_closing_the_speaker_closes_the_socket_and_ends_the_output() -> None:
    socket = FakeAuraSocket()

    async def go():
        async with tts(FakeConnector([socket])).open() as speaker:
            output = speaker.output()
        return [item async for item in output]

    assert run(go()) == []
    assert socket.sent == [("close", "")]


@pytest.mark.parametrize(("failure", "raised"), HANDSHAKE_FAILURES, ids=HANDSHAKE_IDS)
def test_aura_handshake_failures(failure, raised) -> None:
    async def go():
        async with tts(FakeConnector([failure])).open():
            pass

    with pytest.raises(raised):
        run(go())
