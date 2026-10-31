import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent.llm import LLMClient, LLMUnavailableError
from backend.app.agent.sessions import ConversationSession
from backend.app.services.conversation_events import (
    ConversationEvent,
    record_conversation_events,
)
from backend.app.tools.base import ToolContext, ToolResult
from backend.app.tools.handoff import hand_off
from backend.app.tools.registry import execute_tool, tool_schemas

logger = logging.getLogger(__name__)

# Model calls allowed in one caller turn before we stop and hand off.
MAX_MODEL_CALLS = 6

# Fixed replies for when the model can't give a usable answer. They promise
# nothing and state no account facts.
REPLY_CANNOT_HELP = "Sorry, I can't help with that here. Is there anything else I can do for you?"
REPLY_UNAVAILABLE = "Sorry, I'm having trouble right now. Please try again in a moment."
REPLY_STUCK = "Sorry, I wasn't able to finish that. I'm connecting you with a member of our team."
REPLY_EMPTY = "Sorry, could you say that again?"
REPLY_HANDED_OFF = "You're being connected to a member of our team. Please hold."
REPLY_TURN_LIMIT = (
    "We've covered a lot in this call, so I'm connecting you with a member of our team "
    "who can help you further. Please hold."
)


# Fixed replies the model should hear about on the next turn (D74). The rest
# end the conversation, so the model never speaks again.
UNHEARD_OUTCOMES = {"unavailable", "empty", "cannot_help"}
NOTE_START = "[Note from CollectWise, not the caller: "
NOTE_UNHEARD = "since your last reply the caller was sent these automatic messages: "
NOTE_PREFIX = NOTE_START + NOTE_UNHEARD
NOTE_INTERRUPTED = 'the caller interrupted your last reply and heard only: "{heard}"'
NOTE_INTERRUPTED_EARLY = "the caller interrupted your last reply before hearing any of it"


def _with_note(session: ConversationSession, text: str) -> str:
    """Prefix the caller's words with what they heard that the model doesn't
    know about (fixed replies, D74; how much of an interrupted reply, D82),
    and clear it. One string, so history stays append-only and roles
    alternate."""
    heard = session.heard_before_interruption
    if heard is None and not session.unheard_by_model:
        return text
    parts = []
    if heard is not None:
        parts.append(NOTE_INTERRUPTED.format(heard=heard) if heard else NOTE_INTERRUPTED_EARLY)
    if session.unheard_by_model:
        parts.append(NOTE_UNHEARD + "; ".join(f'"{reply}"' for reply in session.unheard_by_model))
    session.heard_before_interruption = None
    session.unheard_by_model.clear()
    return f"{NOTE_START}{'; '.join(parts)}]\n\n{text}"


class SessionBusyError(Exception):
    """The conversation's previous turn was still running after the wait."""


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    ok: bool
    error: str | None


@dataclass(frozen=True)
class TurnResult:
    reply: str
    handed_off: bool
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    # True when `reply` is one of the fixed replies above rather than the
    # model's words, so a live call knows to speak it (M5.3).
    fixed_reply: bool = False


def _tool_result_content(result: ToolResult) -> str:
    return json.dumps({"ok": result.ok, "data": result.data, "error": result.error})


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _token_counts(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        key: getattr(usage, key, None)
        for key in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    }


def _reply_text(content: list[Any]) -> str:
    return "\n".join(
        block.text for block in content if getattr(block, "type", None) == "text"
    ).strip()


def run_turn(
    session: ConversationSession,
    text: str,
    *,
    llm: LLMClient,
    db: Session,
    system: str,
    on_text: Callable[[str], None] | None = None,
    lock_timeout: float | None = None,
) -> TurnResult:
    """Run one caller turn: model calls and tool calls until a reply.

    With `on_text`, the model's text from every call in the turn (including a
    "let me check" before a tool call) is passed on as it is generated (M5).
    Fixed replies are never streamed: the caller speaks `TurnResult.reply`
    when nothing was. The result is the same either way.

    Turns in one conversation run one at a time. With `lock_timeout`, a turn
    that can't start within that many seconds raises SessionBusyError instead
    of waiting for the previous one (M6.1); it is not counted.
    """
    if not session.lock.acquire(timeout=-1 if lock_timeout is None else lock_timeout):
        raise SessionBusyError()
    try:
        state = session.state
        turn_started = time.perf_counter()
        events: list[ConversationEvent] = []

        def record(event_type: str, **fields: Any) -> None:
            events.append(
                ConversationEvent(
                    session_id=session.session_id,
                    customer_id=session.caller.customer_id,
                    turn=state.turn,
                    event_type=event_type,
                    **fields,
                )
            )

        try:
            return _run_turn(session, text, llm=llm, db=db, system=system, record=record,
                             turn_started=turn_started, on_text=on_text)
        finally:
            # Written after the turn in a separate commit, so servicing
            # transactions and tool rollbacks never touch them (D48).
            record_conversation_events(db, events)
    finally:
        session.lock.release()


def _run_turn(
    session: ConversationSession,
    text: str,
    *,
    llm: LLMClient,
    db: Session,
    system: str,
    record: Callable[..., None],
    turn_started: float,
    on_text: Callable[[str], None] | None,
) -> TurnResult:
    state = session.state
    tool_calls: list[ToolCallRecord] = []

    def finish(reply: str, outcome: str) -> TurnResult:
        record("turn_completed", outcome=outcome, latency_ms=_elapsed_ms(turn_started))
        if outcome in UNHEARD_OUTCOMES:
            session.note_unheard(reply)
            # A payment proposed in this turn was never read back: the caller
            # heard only this fixed reply, so a later "yes" mustn't submit it.
            # (It can't be submitted yet: confirming needs a later turn.)
            pending = state.pending_payment
            if pending is not None and pending.proposed_turn == state.turn:
                state.pending_payment = None
                record("payment_cancelled", outcome="not_read_back")
        return TurnResult(
            reply=reply,
            handed_off=state.handoff is not None,
            tool_calls=tool_calls,
            fixed_reply=outcome != "reply",
        )

    # After a handoff the agent no longer converses (D33).
    if state.handoff is not None:
        return finish(REPLY_HANDED_OFF, "already_handed_off")

    # A long conversation goes to a person, which also bounds its history
    # and cost (D72). Decided before counting the turn or calling the model.
    if state.turn >= config.MAX_CALLER_TURNS:
        hand_off(state, "turn_limit")
        logger.info("Turn limit reached in session %s", session.session_id)
        return finish(REPLY_TURN_LIMIT, "turn_limit")

    # The only place a caller turn is counted (D24).
    state.start_caller_turn()
    record("turn_started")
    session.messages.append({"role": "user", "content": _with_note(session, text)})

    ctx = ToolContext(db=db, caller=session.caller, state=state)
    tools = tool_schemas()

    def run_tool(name: str, raw_args: Any) -> ToolResult:
        started = time.perf_counter()
        outcome = execute_tool(ctx, name, raw_args)
        record(
            "tool_call",
            name=name,
            outcome="ok" if outcome.ok else outcome.error,
            latency_ms=_elapsed_ms(started),
        )
        tool_calls.append(ToolCallRecord(name, outcome.ok, outcome.error))
        return outcome

    for _ in range(MAX_MODEL_CALLS):
        started = time.perf_counter()
        try:
            # on_text only when streaming, so non-streaming clients needn't take it.
            stream = {} if on_text is None else {"on_text": on_text}
            response = llm.create(system=system, tools=tools, messages=session.messages, **stream)
        except LLMUnavailableError:
            record("model_call", outcome="unavailable", latency_ms=_elapsed_ms(started))
            logger.warning("LLM unavailable in session %s", session.session_id)
            return finish(REPLY_UNAVAILABLE, "unavailable")
        record(
            "model_call",
            outcome=response.stop_reason,
            latency_ms=_elapsed_ms(started),
            details=_token_counts(response),
        )

        # Append the full content unchanged, including any thinking blocks.
        session.messages.append({"role": "assistant", "content": response.content})

        tool_uses = [
            block for block in response.content
            if getattr(block, "type", None) == "tool_use"
        ]

        if response.stop_reason == "tool_use" and tool_uses:
            tool_results = []
            for block in tool_uses:
                outcome = run_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _tool_result_content(outcome),
                        "is_error": not outcome.ok,
                    }
                )
            # All results go back in a single message.
            session.messages.append({"role": "user", "content": tool_results})
            continue

        if response.stop_reason == "end_turn":
            reply = _reply_text(response.content)
            if not reply:
                return finish(REPLY_EMPTY, "empty")
            return finish(reply, "reply")

        # Refusal, max_tokens, or tool_use without any tool block.
        logger.warning(
            "Unusable model response (%s) in session %s",
            response.stop_reason,
            session.session_id,
        )
        return finish(REPLY_CANNOT_HELP, "cannot_help")

    # The model kept calling tools without answering: stop and hand off
    # rather than leave the caller stuck.
    logger.warning("Model call limit reached in session %s", session.session_id)
    run_tool("handoff_to_human", {"reason": "unresolved_issue"})
    return finish(REPLY_STUCK, "stuck")
