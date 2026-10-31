from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.db.types import UTCDateTime

class CustomerRecord(Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
    )
    display_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )


class AccountRecord(Base):
    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
    )
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customers.customer_id"),
        nullable=False,
        index=True,
    )
    balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "balance >= 0",
            name="ck_accounts_nonnegative_balance",
        ),
        CheckConstraint(
            "length(currency) = 3",
            name="ck_accounts_currency_length",
        ),
        CheckConstraint(
            "status IN ('active', 'frozen', 'closed')",
            name="ck_accounts_status",
        ),
    )

class ServicingRequestRecord(Base):
    __tablename__ = "servicing_requests"

    request_id: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
    )
    customer_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    account_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    amount: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    decision: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="evaluated",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "decision IN ('allow', 'deny')",
            name="ck_servicing_requests_decision",
        ),
        CheckConstraint(
            "status IN ('received', 'evaluated', 'failed')",
            name="ck_servicing_requests_status",
        ),
    )


class ServicingAuditEvent(Base):
    __tablename__ = "servicing_audit_events"

    event_id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )
    request_id: Mapped[str] = mapped_column(
        ForeignKey("servicing_requests.request_id"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    details: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN "
            "('request_received', 'policy_evaluated', 'decision_persisted')",
            name="ck_servicing_audit_events_type",
        ),
    )

class ConversationEventRecord(Base):
    """Agent conversation telemetry: metadata only, never message text or tool
    arguments (D40). Deliberately separate from servicing_audit_events (D3)."""

    __tablename__ = "conversation_events"

    event_id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )
    turn: Mapped[int] = mapped_column(
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    # Tool name for tool_call events.
    name: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    # A code such as "ok", "insufficient_funds", "end_turn" or "reply".
    outcome: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    latency_ms: Mapped[int | None] = mapped_column(
        nullable=True,
    )
    # JSON of counts only (e.g. token usage).
    details: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('session_started', 'turn_started', "
            "'model_call', 'tool_call', 'turn_completed', 'payment_cancelled')",
            name="ck_conversation_events_type",
        ),
        CheckConstraint(
            "turn >= 0",
            name="ck_conversation_events_turn",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_conversation_events_latency",
        ),
    )


class VoiceTurnMetricRecord(Base):
    """One row per voice turn: stage timings and codes, never words (D40, D60).
    A new table rather than new conversation_events types (D16)."""

    __tablename__ = "voice_turn_metrics"

    metric_id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )
    # The conversation's caller-turn count after this voice turn.
    turn: Mapped[int] = mapped_column(
        nullable=False,
    )
    outcome: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    # False when speech synthesis failed and only text was returned.
    audio_ok: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )
    audio_in_ms: Mapped[int] = mapped_column(
        nullable=False,
    )
    audio_out_ms: Mapped[int | None] = mapped_column(
        nullable=True,
    )
    # Stage timings; None for stages that didn't run.
    stt_ms: Mapped[int | None] = mapped_column(
        nullable=True,
    )
    agent_ms: Mapped[int | None] = mapped_column(
        nullable=True,
    )
    tts_ms: Mapped[int | None] = mapped_column(
        nullable=True,
    )
    total_ms: Mapped[int] = mapped_column(
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    transcript_chars: Mapped[int | None] = mapped_column(
        nullable=True,
    )
    reply_chars: Mapped[int] = mapped_column(
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('reply', 'not_understood', 'too_long', "
            "'stt_unavailable', 'already_handed_off')",
            name="ck_voice_turn_metrics_outcome",
        ),
        CheckConstraint(
            "turn >= 0 AND audio_in_ms >= 0 AND total_ms >= 0 AND reply_chars >= 0",
            name="ck_voice_turn_metrics_nonnegative",
        ),
        CheckConstraint(
            "(stt_ms IS NULL OR stt_ms >= 0) AND (agent_ms IS NULL OR agent_ms >= 0) "
            "AND (tts_ms IS NULL OR tts_ms >= 0) AND (audio_out_ms IS NULL OR audio_out_ms >= 0)",
            name="ck_voice_turn_metrics_stage_times",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_voice_turn_metrics_confidence",
        ),
    )


class LiveTurnMetricRecord(Base):
    """One row per caller turn in a live call (M5.3): timings and codes, never
    words (D40, D60). A new table: columns added to voice_turn_metrics
    wouldn't reach existing databases without migrations (D16)."""

    __tablename__ = "live_turn_metrics"

    metric_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # The conversation's caller-turn count after this turn.
    turn: Mapped[int] = mapped_column(nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    # Word confidences of the final transcript; None when there were none.
    confidence_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    transcript_chars: Mapped[int] = mapped_column(nullable=False)
    reply_chars: Mapped[int] = mapped_column(nullable=False)
    sentences: Mapped[int] = mapped_column(nullable=False)
    # From the end of the caller's turn; None when it never happened (no
    # streamed text, or speech failed).
    first_text_ms: Mapped[int | None] = mapped_column(nullable=True)
    first_audio_ms: Mapped[int | None] = mapped_column(nullable=True)
    agent_ms: Mapped[int | None] = mapped_column(nullable=True)
    # False when the reply could only be shown as text.
    audio_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('reply', 'not_understood', 'too_long', 'already_handed_off')",
            name="ck_live_turn_metrics_outcome",
        ),
        CheckConstraint("turn >= 0", name="ck_live_turn_metrics_turn"),
    )


class AccountLabelRecord(Base):
    """A spoken name for an account, such as "savings" (D58). Its own table,
    not a column on accounts, because create_all never alters tables (D16)."""

    __tablename__ = "account_labels"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.account_id"),
        primary_key=True,
    )
    # Copied from the account by the service (never from input), so the
    # database can enforce one label per name per customer.
    customer_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("customer_id", "label", name="uq_account_labels_customer_label"),
        CheckConstraint(
            "label = lower(label) AND length(label) BETWEEN 1 AND 30",
            name="ck_account_labels_label",
        ),
    )
