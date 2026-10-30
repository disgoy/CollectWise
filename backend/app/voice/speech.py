"""Contracts for turning speech into text and text into speech.

The voice turn depends only on these shapes, never on a vendor SDK: a provider
adapter (Deepgram, step 4.6) implements them, and tests use fakes. This mirrors
LLMClient for the model.
"""

import math
from dataclasses import dataclass
from typing import Protocol

from backend.app.voice.audio import AudioClip


class SpeechUnavailableError(Exception):
    """The speech service could not be reached or is temporarily failing.

    Adapters translate only transient failures into this (connection errors,
    timeouts, rate limits, server errors), so the voice turn can apologise and
    ask the caller to retry. Anything else (a bad key, a malformed request)
    is our bug or misconfiguration and must raise as it is (as D45).
    """


@dataclass(frozen=True)
class Transcript:
    text: str
    # How sure the service is that it heard correctly, from 0 to 1. None when
    # the service doesn't say.
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not (
            math.isfinite(self.confidence) and 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("confidence must be between 0 and 1")


class SpeechToText(Protocol):
    def transcribe(self, clip: AudioClip) -> Transcript:
        """Return what was said in the clip. Raises SpeechUnavailableError
        for transient failures."""
        ...


class TextToSpeech(Protocol):
    def synthesize(self, text: str) -> AudioClip:
        """Return the text spoken aloud as mono PCM16 audio. Raises
        SpeechUnavailableError for transient failures."""
        ...
