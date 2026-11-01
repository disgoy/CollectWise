"""Dependencies the agent, voice and live routes share: the conversation
store, the model and speech clients, and a database-session factory. Tests
override these with fakes."""

import logging
import os
from collections.abc import Callable
from functools import lru_cache

from fastapi import HTTPException

from backend.app import config
from backend.app.agent.claude import build_claude_client
from backend.app.agent.llm import LLMClient
from backend.app.agent.sessions import SessionStore
from backend.app.agent.simulated import LoadTestOnly, SimulatedLLM
from backend.app.db.session import SessionLocal
from backend.app.voice.deepgram import (
    DeepgramSTT,
    DeepgramTTS,
    build_deepgram_speech,
    deepgram_configured,
)
from backend.app.voice.deepgram_live import DeepgramLiveSTT, DeepgramLiveTTS, build_deepgram_live_speech
from backend.app.voice.live_speech import LiveSpeechToText, LiveTextToSpeech
from backend.app.voice.simulated_live import SimulatedLiveSTT, SimulatedLiveTTS
from backend.app.voice.speech import SpeechToText, TextToSpeech

logger = logging.getLogger(__name__)

_session_store = SessionStore()


def get_session_store() -> SessionStore:
    return _session_store


@lru_cache(maxsize=1)
def _claude_client(model: str, effort: str) -> LLMClient:
    return build_claude_client(model=model, effort=effort)


@lru_cache(maxsize=1)
def _simulated_llm(speed: float) -> LLMClient:
    return SimulatedLLM(speed)


def get_llm() -> LLMClient:
    # Off unless COLLECTWISE_AGENT_LLM=anthropic (or "simulated" in a load
    # test, M6.5). Tests override this dependency with a scripted fake.
    if config.AGENT_LLM == "simulated":
        try:
            return _simulated_llm(config.SIMULATED_SPEED)
        except LoadTestOnly:
            logger.error("COLLECTWISE_AGENT_LLM=simulated needs COLLECTWISE_LOAD_TEST=1; the agent stays off")
    if config.AGENT_LLM != "anthropic":
        raise HTTPException(status_code=503, detail="Agent is not configured")
    return _claude_client(config.AGENT_MODEL, config.AGENT_EFFORT)


@lru_cache(maxsize=1)
def _deepgram_speech(stt_model: str, tts_voice: str) -> tuple[DeepgramSTT, DeepgramTTS]:
    return build_deepgram_speech(stt_model=stt_model, tts_voice=tts_voice)


def _speech() -> tuple[DeepgramSTT, DeepgramTTS]:
    # Off unless COLLECTWISE_SPEECH=deepgram and a key is set. Tests override
    # get_stt / get_tts with fakes.
    if config.SPEECH_PROVIDER != "deepgram" or not deepgram_configured():
        raise HTTPException(status_code=503, detail="Voice is not configured")
    return _deepgram_speech(config.STT_MODEL, config.TTS_VOICE)


def get_stt() -> SpeechToText:
    return _speech()[0]


def get_tts() -> TextToSpeech:
    return _speech()[1]


# --- the live endpoint (D83) ----------------------------------------------------
# It has to accept the socket before it can refuse with a close code, so these
# return None when something is switched off instead of raising.


def get_live_llm() -> LLMClient | None:
    try:
        return get_llm()
    except HTTPException:
        return None


@lru_cache(maxsize=1)
def _deepgram_live_speech(
    stt_model: str, tts_voice: str, eot_threshold: float, eot_timeout_ms: int
) -> tuple[DeepgramLiveSTT, DeepgramLiveTTS]:
    return build_deepgram_live_speech(
        stt_model=stt_model,
        tts_voice=tts_voice,
        eot_threshold=eot_threshold,
        eot_timeout_ms=eot_timeout_ms,
        api_key=os.environ["DEEPGRAM_API_KEY"],
    )


@lru_cache(maxsize=1)
def _simulated_live_speech(speed: float) -> tuple[SimulatedLiveSTT, SimulatedLiveTTS]:
    return SimulatedLiveSTT(), SimulatedLiveTTS(speed)


def _live_speech() -> tuple[LiveSpeechToText, LiveTextToSpeech] | None:
    # Off unless COLLECTWISE_SPEECH=deepgram and a key is set, like _speech(),
    # or "simulated" in a load test (M6.5).
    if config.SPEECH_PROVIDER == "simulated":
        try:
            return _simulated_live_speech(config.SIMULATED_SPEED)
        except LoadTestOnly:
            logger.error("COLLECTWISE_SPEECH=simulated needs COLLECTWISE_LOAD_TEST=1; live speech stays off")
            return None
    if config.SPEECH_PROVIDER != "deepgram" or not deepgram_configured():
        return None
    return _deepgram_live_speech(config.LIVE_STT_MODEL, config.TTS_VOICE, config.EOT_THRESHOLD, config.EOT_TIMEOUT_MS)


def get_live_stt() -> LiveSpeechToText | None:
    speech = _live_speech()
    return None if speech is None else speech[0]


def get_live_tts() -> LiveTextToSpeech | None:
    speech = _live_speech()
    return None if speech is None else speech[1]


def get_db_factory() -> Callable[[], object]:
    """New database sessions for work that runs outside a request (a live call)."""
    return SessionLocal
