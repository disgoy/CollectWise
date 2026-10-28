from dataclasses import dataclass, field
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


@dataclass
class ConversationState:
    """Mutable per-conversation state that tools read and update."""


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
