"""Contracts for live (streaming) speech, for real-time calls (M5).

Like speech.py, but connections stay open for the whole call: caller audio
streams in and turn events stream out; reply sentences stream in and audio
streams out. Provider-neutral, so the call engine is tested with fakes.
Failures a caller should hear about as "try again" are SpeechUnavailableError.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class HeardEvent:
    """What the listener noticed.

    - speech_started: the caller began speaking (used to interrupt the agent)
    - update: the words so far in this turn (for captions only)
    - end_of_turn: the caller finished; `text` is final. Only this can become
      a caller turn, and only after the guard (D77).
    """

    kind: Literal["speech_started", "update", "end_of_turn"]
    text: str
    # Per word, on end_of_turn; the guard turns these into one number.
    word_confidences: tuple[float, ...] = ()


class LiveListener(Protocol):
    async def send_audio(self, chunk: bytes) -> None:
        """Mono 16-bit PCM at the rate the listener was opened with."""
        ...

    def events(self) -> AsyncIterator[HeardEvent]:
        """Events until the stream ends; raises SpeechUnavailableError if the
        service fails mid-call."""
        ...

    async def finish(self) -> None:
        """No more audio: let the service finish and close the stream."""
        ...


class LiveSpeechToText(Protocol):
    def open(
        self, *, sample_rate: int, keyterms: Sequence[str] = ()
    ) -> AbstractAsyncContextManager[LiveListener]:
        """One listener for one call. `keyterms` are words to expect, such as
        the caller's account names."""
        ...


@dataclass(frozen=True)
class SpokenAudio:
    """Mono 16-bit PCM for part of a reply, at the speaker's sample rate."""

    segment: int
    data: bytes


@dataclass(frozen=True)
class SegmentDone:
    """All audio for these sentences has been delivered."""

    segment: int
    sentences: tuple[int, ...]


class LiveSpeaker(Protocol):
    sample_rate: int

    async def say(self, sentence: str) -> int:
        """Queue one sentence of the current reply; returns its number."""
        ...

    async def end_reply(self) -> None:
        """The reply has no more sentences: speak everything queued."""
        ...

    async def clear(self) -> None:
        """Drop everything not yet delivered (the caller interrupted)."""
        ...

    def output(self) -> AsyncIterator[SpokenAudio | SegmentDone]:
        """Audio and segment ends, in order; raises SpeechUnavailableError if
        the service fails while a reply is being spoken."""
        ...


class LiveTextToSpeech(Protocol):
    def open(self) -> AbstractAsyncContextManager[LiveSpeaker]:
        """One speaker for one call."""
        ...
