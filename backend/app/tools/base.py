from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class CallerContext:
    """The authenticated caller for one conversation.

    Created only by trusted code from an authenticated session (for example
    after validating a session token). It is never built from LLM output, and
    no tool argument model contains a customer ID.
    """

    customer_id: str
    session_id: str


@dataclass(frozen=True)
class PendingPayment:
    """A payment proposed to the caller, waiting for their confirmation."""

    request_id: str
    account_id: str
    amount: Decimal
    currency: str
    # The caller turn in which the payment was proposed.
    proposed_turn: int
    submitted: bool = False


@dataclass
class ConversationState:
    """Mutable per-conversation state that tools read and update."""

    # Number of caller utterances so far. Only the conversation manager
    # advances it (start_caller_turn); tools only read it.
    turn: int = 0
    pending_payment: PendingPayment | None = None

    def start_caller_turn(self) -> None:
        """Record that the caller has spoken again."""
        self.turn += 1


@dataclass
class ToolContext:
    """Everything a tool may use besides the LLM-supplied arguments."""

    db: Session
    caller: CallerContext
    state: ConversationState = field(default_factory=ConversationState)


@dataclass(frozen=True)
class ToolResult:
    """Structured tool outcome handed back to the LLM.

    Tools return a failure with a stable error code instead of raising, so the
    agent can explain the outcome to the caller. `data` must be JSON-safe.
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def success(cls, **data: Any) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, error: str) -> "ToolResult":
        return cls(ok=False, error=error)
