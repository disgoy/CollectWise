"""Simulated live speech for load tests (M6.5): free, timed like the real one.

Listening ends a caller turn once a set amount of audio has streamed in (the
load test's caller sends that much, then waits for the reply), with scripted
words and confident scores. Speaking returns silence: the first audio of a
reply after ~0.3 s (Aura's measured first-audio time), then the rest faster
than real time, as Aura streams it. Both refuse to exist unless
COLLECTWISE_LOAD_TEST=1.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from backend.app.agent.simulated import require_load_test
from backend.app.voice.live_speech import HeardEvent, SegmentDone, SpokenAudio

PHRASES = ("What's my balance?", "And my savings account?", "Thanks, what else can you do?")
TURN_AUDIO_S = 2.0
FIRST_AUDIO_S = 0.3
SPEAKING_PACE = 4.0  # seconds of speech delivered per second
SECONDS_PER_CHAR = 0.065  # about 15 characters a second, spoken
OUTPUT_RATE = 24_000
_END = object()


class _Listener:
    def __init__(self, sample_rate: int) -> None:
        self._turn_bytes = int(TURN_AUDIO_S * sample_rate) * 2
        self._heard = 0
        self._turns = 0
        self._events: asyncio.Queue = asyncio.Queue()

    async def send_audio(self, chunk: bytes) -> None:
        if self._heard == 0:
            self._events.put_nowait(HeardEvent("speech_started", ""))
        self._heard += len(chunk)
        if self._heard >= self._turn_bytes:
            words = PHRASES[self._turns % len(PHRASES)]
            self._turns += 1
            self._heard = 0
            self._events.put_nowait(HeardEvent("end_of_turn", words, tuple(0.95 for _ in words.split())))

    async def events(self) -> AsyncIterator[HeardEvent]:
        while (event := await self._events.get()) is not _END:
            yield event

    async def finish(self) -> None:
        self._events.put_nowait(_END)


class SimulatedLiveSTT:
    def __init__(self) -> None:
        require_load_test()

    @asynccontextmanager
    async def open(self, *, sample_rate: int, keyterms: Sequence[str] = (), encoding: str = "linear16"):
        # Counts audio as 16-bit samples: load tests stream PCM (M6.5).
        yield _Listener(sample_rate)


class _Speaker:
    sample_rate = OUTPUT_RATE

    def __init__(self, speed: float) -> None:
        self._speed = speed
        self._sentences = 0
        self._segments = 0
        self._reply_started = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._output: asyncio.Queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._speak())

    async def _speak(self) -> None:
        while True:
            number, sentence, first = await self._queue.get()
            if first:
                await asyncio.sleep(FIRST_AUDIO_S / self._speed)
            segment = self._segments
            self._segments += 1
            seconds = len(sentence) * SECONDS_PER_CHAR
            chunks = max(1, round(seconds / 0.5))
            gap = seconds / chunks / SPEAKING_PACE / self._speed
            for chunk in range(chunks):
                if chunk:  # the first chunk comes at once, after the first-audio wait
                    await asyncio.sleep(gap)
                self._output.put_nowait(SpokenAudio(segment, bytes(int(OUTPUT_RATE * seconds / chunks) * 2)))
            await asyncio.sleep(gap)
            self._output.put_nowait(SegmentDone(segment, (number,)))

    async def say(self, sentence: str) -> int:
        number = self._sentences
        self._sentences += 1
        self._queue.put_nowait((number, sentence, not self._reply_started))
        self._reply_started = True
        return number

    async def end_reply(self) -> None:
        self._reply_started = False

    async def clear(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._reply_started = False

    async def output(self) -> AsyncIterator[SpokenAudio | SegmentDone]:
        while (item := await self._output.get()) is not _END:
            yield item

    def close(self) -> None:
        self._worker.cancel()
        self._output.put_nowait(_END)


class SimulatedLiveTTS:
    sample_rate = OUTPUT_RATE

    def __init__(self, speed: float = 1.0) -> None:
        require_load_test()
        self._speed = speed

    @asynccontextmanager
    async def open(self, audio=None):
        speaker = _Speaker(self._speed)
        try:
            yield speaker
        finally:
            speaker.close()
