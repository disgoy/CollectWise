import dataclasses
import math

import pytest

from backend.app.voice.audio import AudioClip, read_wav, write_wav
from backend.app.voice.speech import (
    SpeechToText,
    SpeechUnavailableError,
    TextToSpeech,
    Transcript,
)
from backend.tests.fake_speech import FakeSTT, FakeTTS

CLIP = AudioClip(data=bytes(3_200), sample_rate=16_000)  # 100 ms of silence


@pytest.mark.parametrize("confidence", [None, 0.0, 0.42, 1.0])
def test_transcript_accepts_confidence_in_range(confidence) -> None:
    assert Transcript("pay 120", confidence).confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01, math.nan, math.inf])
def test_transcript_rejects_confidence_out_of_range(confidence) -> None:
    with pytest.raises(ValueError):
        Transcript("pay 120", confidence)


def test_transcript_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Transcript("pay 120", 0.9).text = "pay 9000"


def test_fakes_satisfy_the_contracts() -> None:
    # Type annotations make a type checker hold the fakes to the protocols;
    # here we just use them through the protocol types.
    stt: SpeechToText = FakeSTT([Transcript("hello", 0.9)])
    tts: TextToSpeech = FakeTTS()

    assert stt.transcribe(CLIP).text == "hello"
    assert isinstance(tts.synthesize("hello"), AudioClip)


def test_fake_stt_replays_its_script_and_records_clips() -> None:
    stt = FakeSTT([Transcript("pay 120", 0.93), SpeechUnavailableError("down")])

    assert stt.transcribe(CLIP) == Transcript("pay 120", 0.93)
    with pytest.raises(SpeechUnavailableError):
        stt.transcribe(CLIP)
    assert stt.clips == [CLIP, CLIP]


def test_fake_stt_fails_loudly_when_its_script_runs_out() -> None:
    with pytest.raises(AssertionError):
        FakeSTT([]).transcribe(CLIP)


def test_fake_tts_returns_silence_sized_to_the_text() -> None:
    tts = FakeTTS(sample_rate=8_000, ms_per_char=50)

    clip = tts.synthesize("You have one account.")  # 21 characters

    assert clip.duration_ms == 21 * 50
    assert clip.sample_rate == 8_000
    assert set(clip.data) == {0}
    assert read_wav(write_wav(clip)) == clip  # a valid clip end to end
    assert tts.texts == ["You have one account."]


def test_fake_tts_raises_queued_failures_first() -> None:
    tts = FakeTTS(failures=[SpeechUnavailableError("busy")])

    with pytest.raises(SpeechUnavailableError):
        tts.synthesize("Hello")
    assert tts.synthesize("Hello").duration_ms > 0
    assert tts.texts == ["Hello", "Hello"]


def test_unavailable_is_its_own_error_type() -> None:
    # The voice turn catches only this, so it must not be a broad base class
    # shared with programming errors.
    assert not issubclass(SpeechUnavailableError, (ValueError, LookupError, AssertionError))
