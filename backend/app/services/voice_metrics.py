import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.app.db.models import LiveTurnMetricRecord, VoiceTurnMetricRecord

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


@dataclass(frozen=True)
class LiveTurnMetric:
    """Timings and codes for one caller turn in a live call (M5.3). Never
    holds what was said or replied (D40)."""

    session_id: str
    customer_id: str
    turn: int
    outcome: str
    confidence_mean: float | None
    confidence_min: float | None
    transcript_chars: int
    reply_chars: int
    sentences: int
    first_text_ms: int | None
    first_audio_ms: int | None
    agent_ms: int | None
    audio_ok: bool


def record_live_turn_metric(db: Session, metric: LiveTurnMetric) -> None:
    """Best effort, like record_voice_turn_metric."""
    try:
        db.add(LiveTurnMetricRecord(**metric.__dict__))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not record a live turn metric")
