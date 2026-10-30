"""Audio clips and WAV handling for voice turns.

A caller's audio arrives as a WAV file: a short header followed by raw PCM
samples. Only one shape is accepted: uncompressed 16-bit PCM, mono, 8-48 kHz.
Everything is checked before any speech service sees (or bills for) the audio.
"""

import io
import wave
from dataclasses import dataclass

SAMPLE_WIDTH_BYTES = 2  # PCM16: each sample is a 16-bit little-endian integer
MIN_SAMPLE_RATE = 8_000  # phone audio
MAX_SAMPLE_RATE = 48_000  # browser audio
MAX_DURATION_MS = 30_000  # one caller turn
# 30 s at 48 kHz mono PCM16 is 2,880,000 bytes, plus the header.
MAX_WAV_BYTES = 3 * 1024 * 1024


class AudioFormatError(ValueError):
    """Audio we won't accept. `reason` is a stable code for callers to map
    (e.g. to an HTTP status); it never echoes the audio itself."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AudioClip:
    """Mono PCM16 samples without a header, and their sample rate."""

    data: bytes
    sample_rate: int

    def __post_init__(self) -> None:
        if not MIN_SAMPLE_RATE <= self.sample_rate <= MAX_SAMPLE_RATE:
            raise AudioFormatError("unsupported_sample_rate")
        if len(self.data) % SAMPLE_WIDTH_BYTES:
            raise AudioFormatError("truncated")

    @property
    def samples(self) -> int:
        return len(self.data) // SAMPLE_WIDTH_BYTES

    @property
    def duration_ms(self) -> int:
        return self.samples * 1000 // self.sample_rate


def read_wav(raw: bytes, *, max_duration_ms: int = MAX_DURATION_MS) -> AudioClip:
    """Parse and check a WAV file. Raises AudioFormatError."""
    # Size first: nothing is parsed until we know it's small enough.
    if len(raw) > MAX_WAV_BYTES:
        raise AudioFormatError("too_large")

    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            declared_samples = wav.getnframes()

            if sample_width != SAMPLE_WIDTH_BYTES:
                raise AudioFormatError("unsupported_sample_width")
            if channels != 1:
                raise AudioFormatError("unsupported_channels")
            if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
                raise AudioFormatError("unsupported_sample_rate")
            if declared_samples == 0:
                raise AudioFormatError("empty")
            # Checked on the header's own claim, before reading the samples.
            if declared_samples * 1000 // sample_rate > max_duration_ms:
                raise AudioFormatError("too_long")

            data = wav.readframes(declared_samples)
    except wave.Error as exc:
        # The wave module only reads uncompressed PCM. Float, mu-law and the
        # "extensible" header variant all arrive here as "unknown format".
        if "unknown format" in str(exc):
            raise AudioFormatError("unsupported_encoding") from exc
        raise AudioFormatError("not_wav") from exc
    except EOFError as exc:  # the header itself is cut short
        raise AudioFormatError("not_wav") from exc

    # The header may claim more samples than the file holds.
    if len(data) != declared_samples * SAMPLE_WIDTH_BYTES:
        raise AudioFormatError("truncated")

    return AudioClip(data=data, sample_rate=sample_rate)


def write_wav(clip: AudioClip) -> bytes:
    """Wrap a clip's samples in a WAV header."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav.setframerate(clip.sample_rate)
        wav.writeframes(clip.data)
    return buffer.getvalue()
