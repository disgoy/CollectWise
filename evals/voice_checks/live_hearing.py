"""Hear a clip the way a live call does (M5.7): streamed into a live listener
(Flux) in 80 ms frames, with quiet before and after, and the final turn's
words and word confidences collected. Used by run_voice_checks --live."""

import asyncio
import statistics
from dataclasses import dataclass

from backend.app.voice.audio import AudioClip
from backend.app.voice.live_speech import LiveSpeechToText

FRAME_MS = 80
LEAD_SILENCE_S = 0.5
# Long enough for the listener to decide the turn is over.
TRAIL_SILENCE_S = 2.0


@dataclass(frozen=True)
class LiveHearing:
    text: str
    word_confidences: tuple[float, ...]

    @property
    def mean(self) -> float | None:
        return statistics.fmean(self.word_confidences) if self.word_confidences else None

    @property
    def lowest(self) -> float | None:
        return min(self.word_confidences) if self.word_confidences else None


def frames(clip: AudioClip) -> list[bytes]:
    """The clip with quiet before and after, cut into 80 ms frames (the last
    one padded with silence)."""
    frame_bytes = clip.sample_rate * 2 * FRAME_MS // 1000
    lead = bytes(int(clip.sample_rate * LEAD_SILENCE_S) * 2)
    trail = bytes(int(clip.sample_rate * TRAIL_SILENCE_S) * 2)
    audio = lead + clip.data + trail
    return [audio[i:i + frame_bytes].ljust(frame_bytes, b"\0") for i in range(0, len(audio), frame_bytes)]


async def hear_live(stt: LiveSpeechToText, clip: AudioClip, *, pace: float = 1.0) -> LiveHearing:
    """`pace` 2.0 streams twice as fast as real time (the listener judges the
    audio, not the wall clock). Several turns in one clip are joined."""
    texts: list[str] = []
    confidences: list[float] = []
    async with stt.open(sample_rate=clip.sample_rate) as listener:

        async def send() -> None:
            for frame in frames(clip):
                await listener.send_audio(frame)
                await asyncio.sleep(FRAME_MS / 1000 / pace)
            await listener.finish()

        sender = asyncio.create_task(send())
        async for event in listener.events():
            if event.kind == "end_of_turn" and event.text.strip():
                texts.append(event.text.strip())
                confidences.extend(event.word_confidences)
        await sender
    return LiveHearing(" ".join(texts), tuple(confidences))
