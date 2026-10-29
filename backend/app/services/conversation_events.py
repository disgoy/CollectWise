import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from backend.app.db.models import ConversationEventRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationEvent:
    """Metadata about one step of a conversation. Never holds message text,
    tool arguments or tool results (D40)."""

    session_id: str
    customer_id: str
    turn: int
    event_type: str
    name: str | None = None
    outcome: str | None = None
    latency_ms: int | None = None
    # Counts only, e.g. token usage.
    details: dict[str, int | None] = field(default_factory=dict)


def _save_events(db: Session, events: list[ConversationEvent]) -> None:
    db.add_all(
        ConversationEventRecord(
            session_id=event.session_id,
            customer_id=event.customer_id,
            turn=event.turn,
            event_type=event.event_type,
            name=event.name,
            outcome=event.outcome,
            latency_ms=event.latency_ms,
            details=json.dumps(event.details, sort_keys=True) if event.details else None,
        )
        for event in events
    )
    db.commit()


def record_conversation_events(db: Session, events: list[ConversationEvent]) -> None:
    """Save events in their own commit. Best effort: telemetry must never
    break a conversation, so a failure is logged and rolled back."""
    if not events:
        return
    try:
        _save_events(db, events)
    except Exception:
        db.rollback()
        logger.exception("Could not record %d conversation events", len(events))
