import base64
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent.claude import build_claude_client
from backend.app.agent.llm import LLMClient
from backend.app.agent.loop import run_turn
from backend.app.agent.prompts import SYSTEM_PROMPT
from backend.app.agent.sessions import SessionStore
from backend.app.auth.dependencies import authenticated_customer_id
from backend.app.db.session import get_db
from backend.app.services.conversation_events import (
    ConversationEvent,
    record_conversation_events,
)
from backend.app.services.customers import customer_exists
from backend.app.voice.audio import MAX_WAV_BYTES, AudioFormatError, read_wav, write_wav
from backend.app.voice.deepgram import (
    DeepgramSTT,
    DeepgramTTS,
    build_deepgram_speech,
    deepgram_configured,
)
from backend.app.voice.speech import SpeechToText, TextToSpeech
from backend.app.voice.turn import run_voice_turn

router = APIRouter(prefix="/agent", tags=["Agent"])

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


class SessionCreatedResponse(BaseModel):
    session_id: str


class TurnRequest(BaseModel):
    # Only the caller's text: identity comes from the session token (D39).
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class TurnResponse(BaseModel):
    # No tool-call details: the caller's app has no use for internal tool
    # names or error codes.
    reply: str
    handed_off: bool


@router.post("/sessions", response_model=SessionCreatedResponse, status_code=201)
def create_session(
    customer_id: str = Depends(authenticated_customer_id),
    db: Session = Depends(get_db),
    store: SessionStore = Depends(get_session_store),
) -> SessionCreatedResponse:
    if not customer_exists(db, customer_id):
        raise HTTPException(status_code=404, detail="Customer not found")

    session = store.create(customer_id)
    record_conversation_events(
        db,
        [
            ConversationEvent(
                session_id=session.session_id,
                customer_id=customer_id,
                turn=0,
                event_type="session_started",
            )
        ],
    )
    return SessionCreatedResponse(session_id=session.session_id)


@router.post("/sessions/{session_id}/turns", response_model=TurnResponse)
def send_turn(
    session_id: str,
    body: TurnRequest,
    customer_id: str = Depends(authenticated_customer_id),
    db: Session = Depends(get_db),
    store: SessionStore = Depends(get_session_store),
    llm: LLMClient = Depends(get_llm),
) -> TurnResponse:
    # Someone else's session looks exactly like a missing one.
    session = store.get_for_customer(session_id, customer_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    result = run_turn(session, body.text, llm=llm, db=db, system=SYSTEM_PROMPT)
    return TurnResponse(reply=result.reply, handed_off=result.handed_off)


WAV_CONTENT_TYPES = {"audio/wav", "audio/x-wav", "audio/wave"}

# AudioFormatError reasons that mean "the audio is the right kind but
# unusable"; every other reason means the format itself is wrong (415).
UNUSABLE_AUDIO = {"empty", "too_long", "truncated"}


class VoiceTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # What was heard, for captions. Returned to the caller's own app only;
    # never stored or logged (D40, D59).
    transcript: str | None
    reply: str
    handed_off: bool
    # Base64 WAV of the spoken reply; null when speech synthesis failed.
    audio: str | None
    audio_format: Literal["wav"] | None


async def _read_body_capped(request: Request, limit: int) -> bytes:
    """Read the upload, giving up as soon as it passes `limit` bytes, so an
    oversized upload is never held in memory in full."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="too_large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="too_large")
    return bytes(body)


@router.post("/sessions/{session_id}/voice-turns", response_model=VoiceTurnResponse)
async def send_voice_turn(
    session_id: str,
    request: Request,
    customer_id: str = Depends(authenticated_customer_id),
    db: Session = Depends(get_db),
    store: SessionStore = Depends(get_session_store),
    llm: LLMClient = Depends(get_llm),
    stt: SpeechToText = Depends(get_stt),
    tts: TextToSpeech = Depends(get_tts),
) -> VoiceTurnResponse:
    # Async only so the upload can be read with a size cap (D64); the turn
    # itself is sync code and runs on a worker thread below.
    session = store.get_for_customer(session_id, customer_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in WAV_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="unsupported_media_type")

    raw = await _read_body_capped(request, MAX_WAV_BYTES)
    try:
        clip = read_wav(raw)
    except AudioFormatError as exc:
        status = 413 if exc.reason == "too_large" else 422 if exc.reason in UNUSABLE_AUDIO else 415
        raise HTTPException(status_code=status, detail=exc.reason) from exc

    result = await run_in_threadpool(
        run_voice_turn, session, clip, stt=stt, tts=tts, llm=llm, db=db, system=SYSTEM_PROMPT,
    )
    audio = None if result.audio is None else base64.b64encode(write_wav(result.audio)).decode("ascii")
    return VoiceTurnResponse(
        transcript=result.transcript,
        reply=result.reply,
        handed_off=result.handed_off,
        audio=audio,
        audio_format="wav" if audio is not None else None,
    )
