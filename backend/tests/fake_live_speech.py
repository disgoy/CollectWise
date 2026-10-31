"""Stand-ins for live speech (M5.3): the test drives what the caller says and
watches what the speaker is asked to say. No network, no audio."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from backend.app.voice.live_speech import HeardEvent, SegmentDone, SpokenAudio
from backend.app.voice.speech import SpeechUnavailableError

_END = object()


class FakeListener:
    def __init__(self) -> None:
        self.events_in: asyncio.Queue = asyncio.Queue()
        self.audio: list[bytes] = []
        self.finished = False
        self.fail_on_audio = False

    def hear(self, kind: str, text: str = "", confidences: tuple[float, ...] = ()) -> None:
        self.events_in.put_nowait(HeardEvent(kind, text, confidences))

    def say_turn(self, text: str, confidence: float = 0.95) -> None:
        """A whole caller turn: speech starts, a caption, the final words."""
        self.hear("speech_started")
        self.hear("update", text)
        self.hear("end_of_turn", text, tuple(confidence for _ in text.split()))

    def break_down(self) -> None:
        self.events_in.put_nowait(SpeechUnavailableError("flux_down"))

    async def send_audio(self, chunk: bytes) -> None:
        if self.fail_on_audio:
            raise SpeechUnavailableError("dropped")
        self.audio.append(chunk)

    async def events(self):
        while True:
            item = await self.events_in.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def finish(self) -> None:
        self.finished = True
        self.events_in.put_nowait(_END)


@dataclass
class FakeLiveSTT:
    listener: FakeListener = field(default_factory=FakeListener)
    fail_to_open: bool = False
    opened_with: dict | None = None

    @asynccontextmanager
    async def open(self, *, sample_rate: int, keyterms=()):
        self.opened_with = {"sample_rate": sample_rate, "keyterms": list(keyterms)}
        if self.fail_to_open:
            raise SpeechUnavailableError("flux_unreachable")
        yield self.listener


class FakeSpeaker:
    """Audio for each sentence is 10 bytes per character. By default a
    sentence's audio is delivered as soon as it is said; with
    `hold_until_end_reply`, a reply's audio comes only at end_reply()."""

    sample_rate = 24_000

    def __init__(self, *, hold_until_end_reply: bool = False) -> None:
        self.said: list[str] = []
        self.replies_ended = 0
        self.hold_until_end_reply = hold_until_end_reply
        self.fail_on_say: int | None = None  # the said-count at which say() fails
        self.too_long: set[str] = set()
        self._held: list[int] = []
        self._output: asyncio.Queue = asyncio.Queue()
        self._segments = 0

    def _deliver(self, sentences: list[int]) -> None:
        segment = self._segments
        self._segments += 1
        for number in sentences:
            self._output.put_nowait(SpokenAudio(segment, b"\x00" * (10 * len(self.said[number]))))
        self._output.put_nowait(SegmentDone(segment, tuple(sentences)))

    async def say(self, sentence: str) -> int:
        if sentence in self.too_long:
            raise SpeechUnavailableError("text_too_long")
        if self.fail_on_say is not None and len(self.said) >= self.fail_on_say:
            raise SpeechUnavailableError("aura_down")
        number = len(self.said)
        self.said.append(sentence)
        if self.hold_until_end_reply:
            self._held.append(number)
        else:
            self._deliver([number])
        return number

    async def end_reply(self) -> None:
        self.replies_ended += 1
        if self._held:
            held, self._held = self._held, []
            self._deliver(held)

    async def clear(self) -> None:
        self._held = []

    def break_down(self) -> None:
        self._output.put_nowait(SpeechUnavailableError("aura_dropped"))

    async def output(self):
        while True:
            item = await self._output.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self) -> None:
        self._output.put_nowait(_END)


@dataclass
class FakeLiveTTS:
    speaker: FakeSpeaker = field(default_factory=FakeSpeaker)
    fail_to_open: bool = False

    @asynccontextmanager
    async def open(self):
        if self.fail_to_open:
            raise SpeechUnavailableError("aura_unreachable")
        try:
            yield self.speaker
        finally:
            self.speaker.close()


class FakeLink:
    """The caller's app: audio frames in until hang_up(), events recorded."""

    def __init__(self) -> None:
        self.events: list = []
        self._frames: asyncio.Queue = asyncio.Queue()
        self._arrived = asyncio.Condition()

    def send_frame(self, chunk: bytes) -> None:
        self._frames.put_nowait(chunk)

    def hang_up(self) -> None:
        self._frames.put_nowait(_END)

    async def audio_in(self):
        while True:
            chunk = await self._frames.get()
            if chunk is _END:
                return
            yield chunk

    async def send(self, event) -> None:
        async with self._arrived:
            self.events.append(event)
            self._arrived.notify_all()

    async def wait_for(self, kind: type, count: int = 1):
        """Wait until `count` events of this type have arrived; return the last."""
        async with self._arrived:
            await self._arrived.wait_for(lambda: len(self.of(kind)) >= count)
        return self.of(kind)[count - 1]

    def of(self, kind: type) -> list:
        return [event for event in self.events if isinstance(event, kind)]
