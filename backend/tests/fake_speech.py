from dataclasses import dataclass, field

from backend.app.voice.audio import AudioClip
from backend.app.voice.speech import Transcript


@dataclass
class FakeSTT:
    """Replays a script of transcripts (or raises scripted exceptions) and
    records every clip it was given."""

    script: list[Transcript | Exception]
    clips: list[AudioClip] = field(default_factory=list)

    def transcribe(self, clip: AudioClip) -> Transcript:
        self.clips.append(clip)
        if not self.script:
            raise AssertionError("FakeSTT script exhausted: an unexpected transcription")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@dataclass
class FakeTTS:
    """Returns silence whose length follows the text, records every text it
    was asked to speak, and raises any queued failures first."""

    sample_rate: int = 16_000
    ms_per_char: int = 60
    failures: list[Exception] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    def synthesize(self, text: str) -> AudioClip:
        self.texts.append(text)
        if self.failures:
            raise self.failures.pop(0)
        samples = self.sample_rate * len(text) * self.ms_per_char // 1000
        return AudioClip(data=bytes(2 * samples), sample_rate=self.sample_rate)
