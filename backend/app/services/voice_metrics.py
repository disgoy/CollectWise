import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.app.db.models import VoiceTurnMetricRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceTurnMetric:
    """Timings and codes for one voice turn. Never holds the transcript, the
    reply or any audio (D40)."""

    session_id: str
    customer_id: str
    turn: int
    outcome: str
    audio_ok: bool
    audio_in_ms: int
    audio_out_ms: int | None
    stt_ms: int | None
    agent_ms: int | None
    tts_ms: int | None
    total_ms: int
    confidence: float | None
    transcript_chars: int | None
    reply_chars: int


def _save_metric(db: Session, metric: VoiceTurnMetric) -> None:
    db.add(VoiceTurnMetricRecord(**metric.__dict__))
    db.commit()


def record_voice_turn_metric(db: Session, metric: VoiceTurnMetric) -> None:
    """Save the metric in its own commit. Best effort, like conversation
    events (D48): a failure is logged and rolled back, never raised."""
    try:
        _save_metric(db, metric)
    except Exception:
        db.rollback()
        logger.exception("Could not record a voice turn metric")
