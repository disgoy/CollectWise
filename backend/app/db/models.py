from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Numeric,
    String,
    Text,
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
            "'model_call', 'tool_call', 'turn_completed')",
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
