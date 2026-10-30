"""One spoken caller turn: listen, check what was heard, run the agent, speak.

The M3 agent (`run_turn`) is reused unchanged, so every rule it enforces still
applies (D60). Voice adds a guard in front of it, so misheard or empty input
never reaches the model, and fallbacks around it, so a speech outage gets a
polite reply instead of an error.
"""

import logging
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent.llm import LLMClient
from backend.app.agent.loop import REPLY_HANDED_OFF, run_turn
from backend.app.agent.sessions import ConversationSession
from backend.app.services.voice_metrics import VoiceTurnMetric, record_voice_turn_metric
from backend.app.voice.audio import AudioClip
from backend.app.voice.speech import SpeechToText, SpeechUnavailableError, TextToSpeech

logger = logging.getLogger(__name__)

# The same limit as a typed turn.
MAX_TRANSCRIPT_CHARS = 2000

# Fixed replies for turns the agent never sees. They promise nothing.
REPLY_NOT_UNDERSTOOD = "Sorry, I didn't catch that. Could you say it again?"
REPLY_TOO_LONG = "Sorry, that was a lot at once. Could you say it more briefly?"
REPLY_CANT_HEAR = "Sorry, I'm having trouble hearing right now. Please try again in a moment."


@dataclass(frozen=True)
class VoiceTurnResult:
    # What was heard; None when we didn't listen or listening failed.
    transcript: str | None
    reply: str
    handed_off: bool
    # The reply spoken aloud; None when speech synthesis failed.
    audio: AudioClip | None


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def run_voice_turn(
    session: ConversationSession,
    audio: AudioClip,
    *,
    stt: SpeechToText,
    tts: TextToSpeech,
    llm: LLMClient,
    db: Session,
    system: str,
) -> VoiceTurnResult:
    turn_started = time.perf_counter()
    transcript: str | None = None
    confidence: float | None = None
    stt_ms = agent_ms = None

    if session.state.handoff is not None:
        # The reply doesn't depend on what was said: don't pay to transcribe.
        reply, outcome = REPLY_HANDED_OFF, "already_handed_off"
    else:
        started = time.perf_counter()
        try:
            heard = stt.transcribe(audio)
        except SpeechUnavailableError:
            logger.warning("Speech-to-text unavailable in session %s", session.session_id)
            heard = None
        stt_ms = _elapsed_ms(started)

        if heard is None:
            reply, outcome = REPLY_CANT_HEAR, "stt_unavailable"
        else:
            transcript = heard.text.strip()
            confidence = heard.confidence
            unsure = confidence is not None and confidence < config.VOICE_MIN_CONFIDENCE
            if not transcript or unsure:
                # Not a caller turn: the turn counter doesn't move (D24).
                reply, outcome = REPLY_NOT_UNDERSTOOD, "not_understood"
            elif len(transcript) > MAX_TRANSCRIPT_CHARS:
                reply, outcome = REPLY_TOO_LONG, "too_long"
            else:
                started = time.perf_counter()
                result = run_turn(session, transcript, llm=llm, db=db, system=system)
                agent_ms = _elapsed_ms(started)
                reply, outcome = result.reply, "reply"

    started = time.perf_counter()
    try:
        reply_audio = tts.synthesize(reply)
    except SpeechUnavailableError:
        # The caller's app can still show the reply as text.
        logger.warning("Text-to-speech unavailable in session %s", session.session_id)
        reply_audio = None
    tts_ms = _elapsed_ms(started)

    record_voice_turn_metric(
        db,
        VoiceTurnMetric(
            session_id=session.session_id,
            customer_id=session.caller.customer_id,
            turn=session.state.turn,
            outcome=outcome,
            audio_ok=reply_audio is not None,
            audio_in_ms=audio.duration_ms,
            audio_out_ms=reply_audio.duration_ms if reply_audio is not None else None,
            stt_ms=stt_ms,
            agent_ms=agent_ms,
            tts_ms=tts_ms,
            total_ms=_elapsed_ms(turn_started),
            confidence=confidence,
            transcript_chars=len(transcript) if transcript is not None else None,
            reply_chars=len(reply),
        ),
    )

    return VoiceTurnResult(
        transcript=transcript,
        reply=reply,
        handed_off=session.state.handoff is not None,
        audio=reply_audio,
    )
