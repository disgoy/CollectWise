"""Spoken caller turns: the same conversation as the typed turns in
api/agent.py, taken with audio (D64)."""

import base64
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.app.agent.llm import LLMClient
from backend.app.agent.prompts import SYSTEM_PROMPT
from backend.app.agent.sessions import SessionStore
from backend.app.api.providers import get_llm, get_session_store, get_stt, get_tts
from backend.app.api.rate_limits import limit_turns
from backend.app.auth.dependencies import authenticated_customer_id
from backend.app.db.session import get_db
from backend.app.voice.audio import MAX_WAV_BYTES, AudioFormatError, read_wav, write_wav
from backend.app.voice.speech import SpeechToText, TextToSpeech
from backend.app.voice.turn import run_voice_turn

router = APIRouter(prefix="/agent", tags=["Agent"])


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


# The rate limit runs before speech, the model or the upload (D72).
@router.post(
    "/sessions/{session_id}/voice-turns",
    response_model=VoiceTurnResponse,
    dependencies=[Depends(limit_turns)],
)
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
