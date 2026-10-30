from types import SimpleNamespace

import httpx
import pytest
from deepgram.core.api_error import ApiError
from deepgram.errors import BadRequestError
from fastapi import HTTPException

from backend.app import config
from backend.app.api import providers
from backend.app.voice.audio import AudioClip, read_wav
from backend.app.voice.deepgram import (
    MAX_TTS_CHARS,
    REQUEST_OPTIONS,
    TTS_SAMPLE_RATE,
    DeepgramSTT,
    DeepgramTTS,
)
from backend.app.voice.speech import SpeechUnavailableError, Transcript

CLIP = AudioClip(data=bytes(2 * 1_600), sample_rate=16_000)


def transcription(*alternatives, channels=True):
    return SimpleNamespace(
        metadata=SimpleNamespace(request_id="req_123", duration=0.1),
        results=SimpleNamespace(
            channels=[SimpleNamespace(alternatives=list(alternatives))] if channels else []
        ),
    )


class StubSDK:
    """Stands in for DeepgramClient: records calls, returns or raises what it's told."""

    def __init__(self, *, transcribe=None, speak=None) -> None:
        self.calls: list[tuple[str, dict]] = []

        def transcribe_file(**kwargs):
            self.calls.append(("transcribe", kwargs))
            if isinstance(transcribe, Exception):
                raise transcribe
            return transcribe

        def generate(**kwargs):
            # Like the SDK: a lazy generator, so errors surface while iterating.
            self.calls.append(("speak", kwargs))
            for chunk in speak:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk

        self.listen = SimpleNamespace(v1=SimpleNamespace(media=SimpleNamespace(transcribe_file=transcribe_file)))
        self.speak = SimpleNamespace(v1=SimpleNamespace(audio=SimpleNamespace(generate=generate)))


TRANSIENT = [
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
    httpx.ReadError("reset"),
    httpx.RemoteProtocolError("bad frame"),
    ApiError(status_code=500, body="oops"),
    ApiError(status_code=503, body="busy"),
    ApiError(status_code=429, body="slow down"),
    ApiError(status_code=408, body="timeout"),
]
TRANSIENT_IDS = ["connect", "timeout", "read", "protocol", "500", "503", "429", "408"]
OUR_FAULT = [
    BadRequestError(body="bad"),
    ApiError(status_code=401, body="bad key"),
    ApiError(status_code=403, body="forbidden"),
    ApiError(status_code=413, body="too long"),
]
OUR_FAULT_IDS = ["400", "401", "403", "413"]


# --- listening ---------------------------------------------------------------------


def test_stt_sends_the_clip_as_wav_with_our_options() -> None:
    sdk = StubSDK(transcribe=transcription(SimpleNamespace(transcript="Pay 120.", confidence=0.93)))

    DeepgramSTT(sdk, model="nova-3").transcribe(CLIP)

    [(name, kwargs)] = sdk.calls
    assert name == "transcribe"
    assert read_wav(kwargs.pop("request")) == CLIP
    assert kwargs == {
        "model": "nova-3", "language": "en", "smart_format": True, "request_options": REQUEST_OPTIONS,
    }


def test_no_sdk_retries_and_a_short_timeout() -> None:
    # The SDK can wait up to 60 s between retries; a caller can't.
    assert REQUEST_OPTIONS == {"timeout": 10.0, "max_retries": 0}


def test_stt_returns_the_best_alternative() -> None:
    sdk = StubSDK(transcribe=transcription(
        SimpleNamespace(transcript="Pay 120.", confidence=0.93),
        SimpleNamespace(transcript="Play 120.", confidence=0.41),
    ))

    assert DeepgramSTT(sdk, model="nova-3").transcribe(CLIP) == Transcript("Pay 120.", 0.93)


@pytest.mark.parametrize(
    "response",
    [
        transcription(channels=False),
        transcription(),
        transcription(SimpleNamespace(transcript=None, confidence=None)),
    ],
    ids=["no-channels", "no-alternatives", "no-transcript"],
)
def test_stt_with_nothing_recognised_is_an_empty_transcript(response) -> None:
    result = DeepgramSTT(StubSDK(transcribe=response), model="nova-3").transcribe(CLIP)

    assert result.text == ""


@pytest.mark.parametrize("error", TRANSIENT, ids=TRANSIENT_IDS)
def test_stt_transient_failures_become_unavailable(error) -> None:
    with pytest.raises(SpeechUnavailableError):
        DeepgramSTT(StubSDK(transcribe=error), model="nova-3").transcribe(CLIP)


@pytest.mark.parametrize("error", OUR_FAULT, ids=OUR_FAULT_IDS)
def test_stt_request_errors_are_not_hidden(error) -> None:
    with pytest.raises(ApiError):
        DeepgramSTT(StubSDK(transcribe=error), model="nova-3").transcribe(CLIP)


def test_stt_does_not_swallow_unrelated_errors() -> None:
    with pytest.raises(RuntimeError):
        DeepgramSTT(StubSDK(transcribe=RuntimeError("bug")), model="nova-3").transcribe(CLIP)


# --- speaking -------------------------------------------------------------------------


def test_tts_asks_for_raw_pcm_and_wraps_it() -> None:
    sdk = StubSDK(speak=[b"\x01\x00" * 100, b"\x02\x00" * 50])

    clip = DeepgramTTS(sdk, voice="aura-2-thalia-en").synthesize("Hello there.")

    assert clip == AudioClip(data=b"\x01\x00" * 100 + b"\x02\x00" * 50, sample_rate=TTS_SAMPLE_RATE)
    [(name, kwargs)] = sdk.calls
    assert name == "speak"
    assert kwargs == {
        "text": "Hello there.", "model": "aura-2-thalia-en", "encoding": "linear16",
        "container": "none", "sample_rate": 24_000, "request_options": REQUEST_OPTIONS,
    }


def test_tts_at_phone_quality() -> None:
    sdk = StubSDK(speak=[b"\x00\x00" * 8_000])

    clip = DeepgramTTS(sdk, voice="aura-2-draco-en", sample_rate=8_000).synthesize("Pay fifty dollars.")

    assert clip.sample_rate == 8_000 and clip.duration_ms == 1_000
    assert sdk.calls[0][1]["sample_rate"] == 8_000


@pytest.mark.parametrize("rate", [11_025, 44_100, 0])
def test_tts_rejects_rates_deepgram_cannot_produce(rate) -> None:
    with pytest.raises(ValueError):
        DeepgramTTS(StubSDK(speak=[]), voice="aura-2-thalia-en", sample_rate=rate)


@pytest.mark.parametrize("error", TRANSIENT, ids=TRANSIENT_IDS)
def test_tts_transient_failures_while_streaming_become_unavailable(error) -> None:
    sdk = StubSDK(speak=[b"\x00\x00", error])

    with pytest.raises(SpeechUnavailableError):
        DeepgramTTS(sdk, voice="aura-2-thalia-en").synthesize("Hello")


@pytest.mark.parametrize("error", OUR_FAULT, ids=OUR_FAULT_IDS)
def test_tts_request_errors_are_not_hidden(error) -> None:
    with pytest.raises(ApiError):
        DeepgramTTS(StubSDK(speak=[error]), voice="aura-2-thalia-en").synthesize("Hello")


def test_tts_text_over_the_limit_is_text_only_without_a_request() -> None:
    sdk = StubSDK(speak=[b"\x00\x00"])

    with pytest.raises(SpeechUnavailableError):
        DeepgramTTS(sdk, voice="aura-2-thalia-en").synthesize("a" * (MAX_TTS_CHARS + 1))
    assert sdk.calls == []


def test_tts_text_at_the_limit_is_spoken() -> None:
    sdk = StubSDK(speak=[b"\x00\x00"])

    DeepgramTTS(sdk, voice="aura-2-thalia-en").synthesize("a" * MAX_TTS_CHARS)

    assert len(sdk.calls) == 1


# --- switching it on -----------------------------------------------------------------


@pytest.fixture
def fresh_speech():
    providers._deepgram_speech.cache_clear()
    yield
    providers._deepgram_speech.cache_clear()


def test_tests_never_have_speech_switched_on() -> None:
    assert config.SPEECH_PROVIDER is None


@pytest.mark.parametrize(
    ("provider", "key"),
    [(None, "test-key"), ("deepgram", None), ("deepgram", ""), ("other", "test-key")],
    ids=["switch-off", "no-key", "empty-key", "unknown-provider"],
)
def test_speech_is_not_configured_without_switch_and_key(fresh_speech, monkeypatch, provider, key) -> None:
    monkeypatch.setattr(config, "SPEECH_PROVIDER", provider)
    if key is None:
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPGRAM_API_KEY", key)

    for dependency in (providers.get_stt, providers.get_tts):
        with pytest.raises(HTTPException) as caught:
            dependency()
        assert (caught.value.status_code, caught.value.detail) == (503, "Voice is not configured")


def test_switched_on_with_a_key_builds_both_adapters(fresh_speech, monkeypatch) -> None:
    # Building the SDK client makes no request.
    monkeypatch.setattr(config, "SPEECH_PROVIDER", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key-not-real")

    stt, tts = providers.get_stt(), providers.get_tts()

    assert isinstance(stt, DeepgramSTT) and isinstance(tts, DeepgramTTS)
    assert stt._client is tts._client  # one SDK client for both
    assert stt._model == config.STT_MODEL
    assert tts._voice == config.TTS_VOICE
