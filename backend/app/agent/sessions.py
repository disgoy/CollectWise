import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from backend.app import config
from backend.app.tools.base import CallerContext, ConversationState

logger = logging.getLogger(__name__)


# Automatic replies kept for the model's next turn (D74). Misheard voice turns
# don't count towards the turn limit, so the list needs a cap of its own.
MAX_UNHEARD_REPLIES = 3


class SessionStoreFullError(Exception):
    """The server already holds as many conversations as it allows."""


@dataclass
class ConversationSession:
    """One caller's conversation: identity, tool state and message history."""

    caller: CallerContext
    state: ConversationState = field(default_factory=ConversationState)
    # Append-only (D38): entries are never edited or removed.
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Serialises turns, so two requests can't interleave in one conversation.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    # When the conversation was last used, on the store's clock.
    last_active: float = 0.0
    # Fixed replies the caller got that the model never saw ("say that
    # again", "try again"), oldest first (D74). Only our own texts, never
    # the caller's words.
    unheard_by_model: list[str] = field(default_factory=list)

    @property
    def session_id(self) -> str:
        return self.caller.session_id

    def note_unheard(self, reply: str) -> None:
        """Keep a fixed reply for the model's next turn. Call with `lock` held."""
        self.unheard_by_model.append(reply)
        del self.unheard_by_model[:-MAX_UNHEARD_REPLIES]


class SessionStore:
    """In-memory conversations (D72).

    Idle conversations expire, each customer may hold only a few (a new one
    replaces their least recently used), and the server holds at most
    `max_total`. Expiry is checked when a conversation is used or a new one
    starts, so no background thread is needed. Lost on restart, by design:
    keeping them would mean storing what callers said.
    """

    def __init__(
        self,
        *,
        idle_timeout_s: float | None = None,
        max_per_customer: int | None = None,
        max_total: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle_timeout_s = config.SESSION_IDLE_MINUTES * 60 if idle_timeout_s is None else idle_timeout_s
        self._max_per_customer = config.MAX_SESSIONS_PER_CUSTOMER if max_per_customer is None else max_per_customer
        self._max_total = config.MAX_SESSIONS if max_total is None else max_total
        if self._idle_timeout_s <= 0 or self._max_per_customer < 1 or self._max_total < 1:
            raise ValueError("Session limits must be positive")
        self._clock = clock
        self._sessions: dict[str, ConversationSession] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _expired(self, session: ConversationSession, now: float) -> bool:
        return now - session.last_active >= self._idle_timeout_s

    def _end(self, session: ConversationSession, reason: str) -> None:
        # Callers hold self._lock. A turn already running in this session
        # still finishes; later requests find nothing.
        del self._sessions[session.session_id]
        logger.info("Session %s ended: %s", session.session_id, reason)

    def create(self, customer_id: str) -> ConversationSession:
        """Start a session for an already-authenticated customer.

        Raises SessionStoreFullError if the server is at its limit.
        """
        now = self._clock()
        with self._lock:
            for expired in [s for s in self._sessions.values() if self._expired(s, now)]:
                self._end(expired, "idle")

            # At the customer's limit: the new conversation replaces their
            # least recently used ones, so a reconnecting app is never stuck.
            own = sorted(
                (s for s in self._sessions.values() if s.caller.customer_id == customer_id),
                key=lambda s: s.last_active,
            )
            for oldest in own[: max(0, len(own) - self._max_per_customer + 1)]:
                self._end(oldest, "replaced")

            if len(self._sessions) >= self._max_total:
                logger.warning("Session store full (%d conversations)", len(self._sessions))
                raise SessionStoreFullError()

            session = ConversationSession(
                caller=CallerContext(
                    customer_id=customer_id,
                    session_id=f"sess_{uuid4().hex}",
                ),
                last_active=now,
            )
            self._sessions[session.session_id] = session
        return session

    def get_for_customer(
        self,
        session_id: str,
        customer_id: str,
    ) -> ConversationSession | None:
        """Return the session only if it belongs to this customer and hasn't
        expired, and mark it as used.

        Someone else's session looks exactly like a missing one, and is left
        untouched.
        """
        now = self._clock()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.caller.customer_id != customer_id:
                return None
            if self._expired(session, now):
                self._end(session, "idle")
                return None
            session.last_active = now
        return session
