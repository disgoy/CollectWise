"""Dependencies the agent and voice routes share: the conversation store
and the model and speech clients. Tests override these with fakes."""

from functools import lru_cache

from fastapi import HTTPException

from backend.app import config
from backend.app.agent.claude import build_claude_client
from backend.app.agent.llm import LLMClient
from backend.app.agent.sessions import SessionStore
from backend.app.voice.deepgram import (
    DeepgramSTT,
    DeepgramTTS,
    build_deepgram_speech,
    deepgram_configured,
)
from backend.app.voice.speech import SpeechToText, TextToSpeech

_session_store = SessionStore()


def get_session_store() -> SessionStore:
    return _session_store


@lru_cache(maxsize=1)
def _claude_client(model: str, effort: str) -> LLMClient:
    return build_claude_client(model=model, effort=effort)


def get_llm() -> LLMClient:
    # Off unless COLLECTWISE_AGENT_LLM=anthropic. Tests override this
    # dependency with a scripted fake.
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
