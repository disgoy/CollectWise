"""The check between hearing and the agent (D60, D63): misheard or overlong
words never reach the model. Shared by the voice turn (M4) and the live call
engine (M5). A guarded turn is never a caller turn (D24).
"""

from dataclasses import dataclass

# The same limit as a typed turn.
MAX_TRANSCRIPT_CHARS = 2000

# Fixed replies for turns the agent never sees. They promise nothing.
REPLY_NOT_UNDERSTOOD = "Sorry, I didn't catch that. Could you say it again?"
REPLY_TOO_LONG = "Sorry, that was a lot at once. Could you say it more briefly?"
REPLY_CANT_HEAR = "Sorry, I'm having trouble hearing right now. Please try again in a moment."


@dataclass(frozen=True)
class GuardResult:
    # The fixed reply to give instead of running the agent; None when the
    # words may go to the agent.
    reply: str | None
    outcome: str  # "ok", "not_understood" or "too_long"


PASSED = GuardResult(None, "ok")


def check_transcript(text: str, confidence: float | None, *, min_confidence: float) -> GuardResult:
    """`text` already stripped. A transcript with no confidence is accepted."""
    if not text or (confidence is not None and confidence < min_confidence):
        return GuardResult(REPLY_NOT_UNDERSTOOD, "not_understood")
    if len(text) > MAX_TRANSCRIPT_CHARS:
        return GuardResult(REPLY_TOO_LONG, "too_long")
    return PASSED
