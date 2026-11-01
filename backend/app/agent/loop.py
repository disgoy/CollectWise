import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy.orm import Session

from opentelemetry.trace import Span, Status, StatusCode

from backend.app import config, telemetry
from backend.app.agent.languages import ENGLISH, language
from backend.app.agent.llm import LLMClient, LLMUnavailableError
from backend.app.agent.readback import states_amount
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

# Fixed replies for when the model can't give a usable answer, in each
# language (agent/languages.py). The English ones by name, for callers and tests.
REPLY_CANNOT_HELP = ENGLISH.replies.cannot_help
REPLY_UNAVAILABLE = ENGLISH.replies.unavailable
REPLY_STUCK = ENGLISH.replies.stuck
REPLY_EMPTY = ENGLISH.replies.empty
REPLY_HANDED_OFF = ENGLISH.replies.handed_off
REPLY_TURN_LIMIT = ENGLISH.replies.turn_limit


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


def _marking_first_text(on_text: Callable[[str], None], span: Span) -> Callable[[str], None]:
    """Pass streamed text on, marking on the span when the first piece came."""
    seen = False

    def passing(piece: str) -> None:
        nonlocal seen
        if not seen:
            seen = True
            span.add_event("first_text")
        on_text(piece)

    return passing


def run_turn(
    session: ConversationSession,
    text: str,
    *,
    llm: LLMClient,
    db: Session,
    system: str | None = None,
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

    The system prompt and fixed replies are the conversation's language's
    (M7.6); `system` overrides the prompt (the eval harness records it).
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
            with telemetry.span("agent.turn", session_id=session.session_id) as turn_span:
                return _run_turn(session, text, llm=llm, db=db,
                                 system=language(session.language).system_prompt if system is None else system,
                                 record=record,
                                 turn_started=turn_started, on_text=on_text, span=turn_span)
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
    span: Span,
) -> TurnResult:
    state = session.state
    replies = language(session.language).replies
    tool_calls: list[ToolCallRecord] = []
    model_calls = 0

    def finish(reply: str, outcome: str) -> TurnResult:
        record("turn_completed", outcome=outcome, latency_ms=_elapsed_ms(turn_started))
        telemetry.annotate(span, turn=state.turn, outcome=outcome, model_calls=model_calls,
                           tool_calls=len(tool_calls))
        if outcome in UNHEARD_OUTCOMES:
            session.note_unheard(reply)
            # A payment proposed in this turn was never read back: the caller
            # heard only this fixed reply, so a later "yes" mustn't submit it.
            # (It can't be submitted yet: confirming needs a later turn.)
            pending = state.pending_payment
            if pending is not None and pending.proposed_turn == state.turn:
                state.pending_payment = None
                record("payment_cancelled", outcome="not_read_back")
                span.add_event("payment_cancelled", telemetry.attributes(reason="not_read_back"))
        elif outcome == "reply":
            # The model's own read-back must state the amount it proposed this
            # turn, in digits; otherwise confirming fails and it reads it back
            # again (M7.6 C).
            pending = state.pending_payment
            if (pending is not None and not pending.submitted and pending.proposed_turn == state.turn
                    and not states_amount(reply, pending.amount)):
                state.pending_payment = replace(pending, readback_stated=False)
                logger.info("Read-back without the amount in digits in session %s", session.session_id)
                span.add_event("readback_missing")
        return TurnResult(
            reply=reply,
            handed_off=state.handoff is not None,
            tool_calls=tool_calls,
            fixed_reply=outcome != "reply",
        )

    # After a handoff the agent no longer converses (D33).
    if state.handoff is not None:
        return finish(replies.handed_off, "already_handed_off")

    # A long conversation goes to a person, which also bounds its history
    # and cost (D72). Decided before counting the turn or calling the model.
    if state.turn >= config.MAX_CALLER_TURNS:
        hand_off(state, "turn_limit")
        logger.info("Turn limit reached in session %s", session.session_id)
        return finish(replies.turn_limit, "turn_limit")

    # The only place a caller turn is counted (D24).
    state.start_caller_turn()
    record("turn_started")
    session.messages.append({"role": "user", "content": _with_note(session, text)})

    ctx = ToolContext(db=db, caller=session.caller, state=state)
    tools = tool_schemas()

    def run_tool(name: str, raw_args: Any) -> ToolResult:
        started = time.perf_counter()
        with telemetry.span("agent.tool", tool=name) as tool_span:
            outcome = execute_tool(ctx, name, raw_args)
            telemetry.annotate(tool_span, ok=outcome.ok, error=outcome.error)
            if outcome.error == "internal_error":
                tool_span.set_status(Status(StatusCode.ERROR))
        # Services commit their own writes, so what is still open here is a
        # read transaction. End it, so the connection goes back to the pool
        # for the model call that follows instead of being held for seconds
        # (M6.5, LOAD_TEST.md). Rollback, not commit: anything unsaved now
        # would be a half-done write, which must not be kept.
        ctx.db.rollback()
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
        model_calls += 1
        with telemetry.span("agent.model_call", streamed=on_text is not None) as call_span:
            try:
                # on_text only when streaming, so non-streaming clients needn't take it.
                stream = {} if on_text is None else {"on_text": _marking_first_text(on_text, call_span)}
                response = llm.create(system=system, tools=tools, messages=session.messages, **stream)
            except LLMUnavailableError:
                call_span.set_status(Status(StatusCode.ERROR))
                telemetry.annotate(call_span, error="unavailable")
                span.set_status(Status(StatusCode.ERROR))
                record("model_call", outcome="unavailable", latency_ms=_elapsed_ms(started))
                logger.warning("LLM unavailable in session %s", session.session_id)
                return finish(replies.unavailable, "unavailable")
            telemetry.annotate(call_span, stop_reason=response.stop_reason, **_token_counts(response))
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
                return finish(replies.empty, "empty")
            return finish(reply, "reply")

        # Refusal, max_tokens, or tool_use without any tool block.
        logger.warning(
            "Unusable model response (%s) in session %s",
            response.stop_reason,
            session.session_id,
        )
        return finish(replies.cannot_help, "cannot_help")

    # The model kept calling tools without answering: stop and hand off
    # rather than leave the caller stuck.
    logger.warning("Model call limit reached in session %s", session.session_id)
    run_tool("handoff_to_human", {"reason": "unresolved_issue"})
    return finish(replies.stuck, "stuck")
