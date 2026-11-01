"""The check between hearing and the agent (D60, D63): misheard or overlong
words never reach the model. Shared by the voice turn (M4) and the live call
engine (M5). A guarded turn is never a caller turn (D24).
"""

from dataclasses import dataclass

from backend.app.agent.languages import ENGLISH, Replies

# The same limit as a typed turn.
MAX_TRANSCRIPT_CHARS = 2000

# Fixed replies for turns the agent never sees, in each language
# (agent/languages.py). They promise nothing. The English ones by name.
REPLY_NOT_UNDERSTOOD = ENGLISH.replies.not_understood
REPLY_TOO_LONG = ENGLISH.replies.too_long
REPLY_CANT_HEAR = ENGLISH.replies.cant_hear


@dataclass(frozen=True)
class GuardResult:
    # The fixed reply to give instead of running the agent; None when the
    # words may go to the agent.
    reply: str | None
    outcome: str  # "ok", "not_understood" or "too_long"


PASSED = GuardResult(None, "ok")


def check_transcript(text: str, confidence: float | None, *, min_confidence: float,
                     replies: Replies = ENGLISH.replies) -> GuardResult:
    """`text` already stripped. A transcript with no confidence is accepted.
    `replies`: the conversation's language."""
    if not text or (confidence is not None and confidence < min_confidence):
        return GuardResult(replies.not_understood, "not_understood")
    if len(text) > MAX_TRANSCRIPT_CHARS:
        return GuardResult(replies.too_long, "too_long")
    return PASSED
