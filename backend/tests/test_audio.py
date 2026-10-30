import io
import math
import struct
import wave

import pytest

from backend.app.voice.audio import (
    MAX_DURATION_MS,
    MAX_WAV_BYTES,
    PCM_SUBFORMAT,
    AudioClip,
    AudioFormatError,
    read_wav,
    write_wav,
)


def tone_samples(duration_ms: int, sample_rate: int, *, frequency: float = 440.0) -> bytes:
    """A sine wave as mono PCM16 samples."""
    count = sample_rate * duration_ms // 1000
    return b"".join(
        struct.pack("<h", int(12_000 * math.sin(2 * math.pi * frequency * i / sample_rate)))
        for i in range(count)
    )


def wav_bytes(
    data: bytes,
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """A WAV file in any shape the wave module can write, valid or not for us."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(data)
    return buffer.getvalue()


def handmade_wav(*, format_tag: int, sample_rate: int, bits: int, samples: bytes) -> bytes:
    """A WAV assembled byte by byte, for headers the wave module won't write."""
    block = bits // 8
    fmt = struct.pack("<HHIIHH", format_tag, 1, sample_rate, sample_rate * block, block, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", len(body)) + body


def float_wav() -> bytes:
    """A 32-bit float WAV (format tag 3), which wave can't read."""
    samples = struct.pack("<4f", 0.0, 0.5, -0.5, 0.0)
    return handmade_wav(format_tag=3, sample_rate=16_000, bits=32, samples=samples)


def reason(raw: bytes) -> str:
    with pytest.raises(AudioFormatError) as caught:
        read_wav(raw)
    return caught.value.reason


@pytest.mark.parametrize("sample_rate", [8_000, 16_000, 48_000])
def test_reads_mono_pcm16(sample_rate) -> None:
    data = tone_samples(1_500, sample_rate)

    clip = read_wav(wav_bytes(data, sample_rate=sample_rate))

    assert clip == AudioClip(data=data, sample_rate=sample_rate)
    assert clip.duration_ms == 1_500
    assert clip.samples == sample_rate * 3 // 2


def test_write_then_read_round_trips() -> None:
    clip = AudioClip(data=tone_samples(250, 16_000), sample_rate=16_000)

    assert read_wav(write_wav(clip)) == clip


def test_written_header_describes_the_clip() -> None:
    clip = AudioClip(data=tone_samples(100, 8_000), sample_rate=8_000)

    with wave.open(io.BytesIO(write_wav(clip)), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (
            1, 2, 8_000, 800,
        )


def test_exactly_the_maximum_duration_is_accepted() -> None:
    clip = read_wav(wav_bytes(bytes(2 * 8_000 * MAX_DURATION_MS // 1000), sample_rate=8_000))

    assert clip.duration_ms == MAX_DURATION_MS


def test_one_sample_over_the_maximum_is_too_long() -> None:
    samples = 8_000 * MAX_DURATION_MS // 1000 + 8  # 1 ms over
    assert reason(wav_bytes(bytes(2 * samples), sample_rate=8_000)) == "too_long"


def test_the_duration_limit_can_be_lowered() -> None:
    raw = wav_bytes(tone_samples(2_000, 8_000), sample_rate=8_000)

    with pytest.raises(AudioFormatError) as caught:
        read_wav(raw, max_duration_ms=1_000)
    assert caught.value.reason == "too_long"


def test_oversized_upload_is_rejected_before_parsing() -> None:
    # Not even a WAV: size alone decides.
    assert reason(b"x" * (MAX_WAV_BYTES + 1)) == "too_large"


def test_largest_allowed_clip_fits_the_size_limit() -> None:
    raw = wav_bytes(bytes(2 * 48_000 * MAX_DURATION_MS // 1000), sample_rate=48_000)

    assert len(raw) <= MAX_WAV_BYTES
    assert read_wav(raw).duration_ms == MAX_DURATION_MS


@pytest.mark.parametrize("raw", [b"", b"hello", b"RIFF", b"RIFF\x00\x00\x00\x00WAVEjunk"])
def test_not_a_wav(raw) -> None:
    assert reason(raw) == "not_wav"


@pytest.mark.parametrize("sample_width", [1, 3, 4])
def test_only_16_bit_samples(sample_width) -> None:
    raw = wav_bytes(bytes(sample_width * 1_600), sample_width=sample_width)

    assert reason(raw) == "unsupported_sample_width"


def test_stereo_is_rejected() -> None:
    assert reason(wav_bytes(bytes(4 * 1_600), channels=2)) == "unsupported_channels"


@pytest.mark.parametrize("sample_rate", [4_000, 7_999, 48_001, 96_000])
def test_sample_rate_outside_range(sample_rate) -> None:
    raw = wav_bytes(bytes(2 * 1_000), sample_rate=sample_rate)

    assert reason(raw) == "unsupported_sample_rate"


FLOAT_SUBFORMAT = bytes.fromhex("0300000000001000800000aa00389b71")


def extensible_wav(
    samples: bytes,
    *,
    sample_rate: int = 16_000,
    subformat: bytes = PCM_SUBFORMAT,
    bits: int = 16,
    valid_bits: int = 16,
    chunk_before_fmt: bool = False,
) -> bytes:
    """A WAV with the "extensible" header, as macOS afconvert writes."""
    block = bits // 8
    fmt = struct.pack("<HHIIHH", 0xFFFE, 1, sample_rate, sample_rate * block, block, bits)
    fmt += struct.pack("<HHI", 22, valid_bits, 4) + subformat
    body = b"WAVE"
    if chunk_before_fmt:
        body += b"JUNK" + struct.pack("<I", 4) + b"\x00" * 4
    body += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"FLLR" + struct.pack("<I", 6) + b"\x00" * 6  # padding chunk, as afconvert adds
    body += b"data" + struct.pack("<I", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize("chunk_before_fmt", [False, True], ids=["fmt-first", "junk-first"])
def test_extensible_header_with_plain_pcm_is_accepted(chunk_before_fmt) -> None:
    data = tone_samples(500, 16_000)

    clip = read_wav(extensible_wav(data, chunk_before_fmt=chunk_before_fmt))

    assert clip == AudioClip(data=data, sample_rate=16_000)


@pytest.mark.parametrize(
    "kwargs",
    [{"subformat": FLOAT_SUBFORMAT}, {"valid_bits": 12}],
    ids=["float-subformat", "partial-bits"],
)
def test_other_extensible_audio_is_still_rejected(kwargs) -> None:
    assert reason(extensible_wav(tone_samples(100, 16_000), **kwargs)) == "unsupported_encoding"


def test_extensible_stereo_is_still_rejected() -> None:
    raw = bytearray(extensible_wav(bytes(4 * 1_600)))
    channels_at = raw.index(b"fmt ") + 10
    struct.pack_into("<H", raw, channels_at, 2)

    assert reason(bytes(raw)) == "unsupported_channels"


@pytest.mark.parametrize(
    ("cut_after_fmt", "expected"),
    [(14, "not_wav"), (30, "unsupported_encoding")],
    ids=["inside-basic-fields", "inside-extension"],
)
def test_extensible_header_cut_short_is_rejected_not_a_crash(cut_after_fmt, expected) -> None:
    # Cut inside the extension, the header is left as is and its 0xFFFE tag
    # is refused; cut earlier, it isn't a readable WAV at all.
    raw = extensible_wav(tone_samples(100, 16_000))

    assert reason(raw[: raw.index(b"fmt ") + cut_after_fmt]) == expected


def test_header_claiming_zero_hz_is_rejected_not_a_crash() -> None:
    # Without the rate check, the duration calculation would divide by zero.
    raw = handmade_wav(format_tag=1, sample_rate=0, bits=16, samples=bytes(200))

    assert reason(raw) == "unsupported_sample_rate"


def test_float_audio_is_an_unsupported_encoding() -> None:
    assert reason(float_wav()) == "unsupported_encoding"


def test_empty_clip() -> None:
    assert reason(wav_bytes(b"")) == "empty"


def test_file_cut_short_inside_the_samples() -> None:
    raw = wav_bytes(tone_samples(1_000, 16_000))

    assert reason(raw[: len(raw) // 2]) == "truncated"


def test_header_claiming_more_samples_than_present() -> None:
    raw = bytearray(wav_bytes(tone_samples(100, 16_000)))
    data_size_at = raw.index(b"data") + 4
    struct.pack_into("<I", raw, data_size_at, 10_000)  # claims 5,000 samples

    assert reason(bytes(raw)) == "truncated"


def test_clip_rejects_half_a_sample() -> None:
    with pytest.raises(AudioFormatError) as caught:
        AudioClip(data=b"\x00\x00\x00", sample_rate=16_000)
    assert caught.value.reason == "truncated"


@pytest.mark.parametrize("sample_rate", [0, 7_999, 48_001])
def test_clip_rejects_unsupported_rates(sample_rate) -> None:
    with pytest.raises(AudioFormatError):
        AudioClip(data=b"", sample_rate=sample_rate)


def test_error_carries_only_the_reason() -> None:
    error = AudioFormatError("too_long")

    assert str(error) == "too_long"
    assert error.reason == "too_long"
