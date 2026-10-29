import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from backend.app.tools.base import CallerContext, ConversationState


@dataclass
class ConversationSession:
    """One caller's conversation: identity, tool state and message history."""

    caller: CallerContext
    state: ConversationState = field(default_factory=ConversationState)
    # Append-only (D38): entries are never edited or removed.
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Serialises turns, so two requests can't interleave in one conversation.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def session_id(self) -> str:
        return self.caller.session_id


class SessionStore:
    """In-memory sessions (lost on restart; persistence comes later)."""

    def __init__(self) -> None:
        self._sessions: dict[str, ConversationSession] = {}
        self._lock = threading.Lock()

    def create(self, customer_id: str) -> ConversationSession:
        """Start a session for an already-authenticated customer."""
        session = ConversationSession(
            caller=CallerContext(
                customer_id=customer_id,
                session_id=f"sess_{uuid4().hex}",
            )
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get_for_customer(
        self,
        session_id: str,
        customer_id: str,
    ) -> ConversationSession | None:
        """Return the session only if it belongs to this customer.

        Someone else's session looks exactly like a missing one.
        """
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None or session.caller.customer_id != customer_id:
            return None
        return session
