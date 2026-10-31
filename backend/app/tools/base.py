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
    # When it was proposed, on the monotonic clock (D73).
    proposed_at: float
    submitted: bool = False
    # False when the caller interrupted the reply that read it back, so they
    # may not have heard it (live calls, D82). Typed and M4 voice turns
    # never change it.
    readback_heard: bool = True


@dataclass(frozen=True)
class Handoff:
    """The conversation was handed to a human; the agent must stop acting."""

    reason: str
    # The caller turn in which the handoff happened.
    turn: int


@dataclass
class ConversationState:
    """Mutable per-conversation state that tools read and update."""

    # Number of caller utterances so far. Only the conversation manager
    # advances it (start_caller_turn); tools only read it.
    turn: int = 0
    pending_payment: PendingPayment | None = None
    handoff: Handoff | None = None

    def start_caller_turn(self) -> None:
        """Record that the caller has spoken again."""
        self.turn += 1


@dataclass
class ToolContext:
    """Everything a tool may use besides the LLM-supplied arguments."""

    db: Session
    caller: CallerContext
    state: ConversationState = field(default_factory=ConversationState)


def caller_facing_reason(reason: str) -> str:
    """Map a policy reason to what the caller may hear.

    Someone else's account must look exactly like a missing one, so the
    caller cannot probe which account IDs exist (D21).
    """
    if reason == "customer_account_mismatch":
        return "account_not_found"
    return reason


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
    def failure(cls, error: str, **data: Any) -> "ToolResult":
        return cls(ok=False, data=data, error=error)
