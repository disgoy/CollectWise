"""Audio clips and WAV handling for voice turns.

A caller's audio arrives as a WAV file: a short header followed by raw PCM
samples. Only one shape is accepted: uncompressed 16-bit PCM, mono, 8-48 kHz.
Everything is checked before any speech service sees (or bills for) the audio.
"""

import io
import struct
import wave
from dataclasses import dataclass

SAMPLE_WIDTH_BYTES = 2  # PCM16: each sample is a 16-bit little-endian integer
MIN_SAMPLE_RATE = 8_000  # phone audio
MAX_SAMPLE_RATE = 48_000  # browser audio
MAX_DURATION_MS = 30_000  # one caller turn
# 30 s at 48 kHz mono PCM16 is 2,880,000 bytes, plus the header.
MAX_WAV_BYTES = 3 * 1024 * 1024


WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
# KSDATAFORMAT_SUBTYPE_PCM: the "extensible" header's way of saying plain PCM.
PCM_SUBFORMAT = bytes.fromhex("0100000000001000800000aa00389b71")


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


def _as_plain_pcm_header(raw: bytes) -> bytes:
    """If the file uses the "extensible" WAV header (format 0xFFFE) for plain
    PCM with full-width samples, return it relabelled as plain PCM, which is
    the same audio (D68). Tools such as macOS afconvert write this variant;
    Python 3.11's wave module refuses it. Anything else is returned unchanged
    for read_wav to judge."""
    pos = 12  # after "RIFF", size, "WAVE"
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        size = struct.unpack_from("<I", raw, pos + 4)[0]
        if chunk_id == b"fmt ":
            body = raw[pos + 8:pos + 8 + size]
            if size < 40 or len(body) < 40:
                return raw
            tag, bits = struct.unpack_from("<H", body, 0)[0], struct.unpack_from("<H", body, 14)[0]
            valid_bits = struct.unpack_from("<H", body, 18)[0]
            if tag == WAVE_FORMAT_EXTENSIBLE and body[24:40] == PCM_SUBFORMAT and valid_bits == bits:
                return raw[:pos + 8] + struct.pack("<H", WAVE_FORMAT_PCM) + raw[pos + 10:]
            return raw
        pos += 8 + size + (size & 1)
    return raw


def read_wav(raw: bytes, *, max_duration_ms: int = MAX_DURATION_MS) -> AudioClip:
    """Parse and check a WAV file. Raises AudioFormatError."""
    # Size first: nothing is parsed until we know it's small enough.
    if len(raw) > MAX_WAV_BYTES:
        raise AudioFormatError("too_large")

    raw = _as_plain_pcm_header(raw)
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
        # The wave module only reads uncompressed PCM. Float, mu-law and
        # extensible headers for anything but plain PCM arrive here as
        # "unknown format".
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
