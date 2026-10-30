"""Deepgram speech-to-text and text-to-speech behind the speech contracts
(D57, D66), using the official SDK (deepgram-sdk 7.x).

Listening: pre-recorded `POST /v1/listen` with the caller's WAV. Speaking:
`POST /v1/speak` returning raw 16-bit PCM, which we wrap ourselves.
"""

import logging
import os

import httpx
from deepgram import DeepgramClient
from deepgram.core.api_error import ApiError

from backend.app.voice.audio import AudioClip, write_wav
from backend.app.voice.speech import SpeechUnavailableError, Transcript

logger = logging.getLogger(__name__)

# Raw PCM at this rate rather than Deepgram's WAV container: a streamed WAV
# header may carry placeholder sizes that read_wav would rightly reject.
TTS_SAMPLE_RATE = 24_000
# Deepgram's per-request limit for Aura voices; longer text gets a 413.
MAX_TTS_CHARS = 2_000

# A caller is waiting. The SDK honours Retry-After for up to 60 s between
# retries, so it doesn't retry at all: a failure goes straight to the polite
# "try again" path instead.
REQUEST_OPTIONS = {"timeout": 10.0, "max_retries": 0}


def _is_transient(exc: Exception) -> bool:
    """Failures worth a "try again". Anything else (400, 401, 403, 413 ...)
    means our request or configuration is wrong and must raise (as D45)."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    return (
        isinstance(exc, ApiError)
        and exc.status_code is not None
        and (exc.status_code >= 500 or exc.status_code in (408, 429))
    )


def _unavailable(exc: Exception, service: str) -> SpeechUnavailableError:
    logger.warning("Deepgram %s unavailable: %s", service, type(exc).__name__)
    return SpeechUnavailableError(type(exc).__name__)


class DeepgramSTT:
    def __init__(self, client: DeepgramClient, *, model: str, language: str = "en") -> None:
        self._client = client
        self._model = model
        self._language = language

    def transcribe(self, clip: AudioClip) -> Transcript:
        try:
            response = self._client.listen.v1.media.transcribe_file(
                request=write_wav(clip),
                model=self._model,
                language=self._language,
                # Punctuation, and numbers as digits ("one twenty" -> "120").
                smart_format=True,
                request_options=REQUEST_OPTIONS,
            )
        except (ApiError, httpx.HTTPError) as exc:
            if not _is_transient(exc):
                raise
            raise _unavailable(exc, "speech-to-text") from exc

        logger.info(
            "Deepgram STT: request_id=%s audio=%.2fs",
            response.metadata.request_id,
            response.metadata.duration,
        )
        channels = response.results.channels
        if not channels or not channels[0].alternatives:
            # Nothing recognised: an empty transcript, which the guard handles.
            return Transcript("", None)
        best = channels[0].alternatives[0]
        return Transcript(best.transcript or "", best.confidence)


# Rates Deepgram accepts for linear16 speech.
TTS_SAMPLE_RATES = (8_000, 16_000, 24_000, 32_000, 48_000)


class DeepgramTTS:
    def __init__(
        self,
        client: DeepgramClient,
        *,
        voice: str,
        sample_rate: int = TTS_SAMPLE_RATE,
    ) -> None:
        if sample_rate not in TTS_SAMPLE_RATES:
            raise ValueError(f"Deepgram can't produce linear16 at {sample_rate} Hz")
        self._client = client
        self._voice = voice
        self._sample_rate = sample_rate

    def synthesize(self, text: str) -> AudioClip:
        if len(text) > MAX_TTS_CHARS:
            # Treated like an outage: the caller still gets the text (D66).
            logger.warning("Reply of %d characters is over the TTS limit; returning text only", len(text))
            raise SpeechUnavailableError("text_too_long")
        try:
            # The SDK streams lazily: the request is made while iterating, so
            # errors surface inside this block.
            audio = b"".join(
                self._client.speak.v1.audio.generate(
                    text=text,
                    model=self._voice,
                    encoding="linear16",
                    container="none",
                    sample_rate=self._sample_rate,
                    request_options=REQUEST_OPTIONS,
                )
            )
        except (ApiError, httpx.HTTPError) as exc:
            if not _is_transient(exc):
                raise
            raise _unavailable(exc, "text-to-speech") from exc
        return AudioClip(data=audio, sample_rate=self._sample_rate)


def deepgram_configured() -> bool:
    return bool(os.environ.get("DEEPGRAM_API_KEY"))


def build_deepgram_speech(*, stt_model: str, tts_voice: str) -> tuple[DeepgramSTT, DeepgramTTS]:
    """One SDK client for both. The key comes from DEEPGRAM_API_KEY; creating
    the client makes no request."""
    client = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])
    return DeepgramSTT(client, model=stt_model), DeepgramTTS(client, voice=tts_voice)
