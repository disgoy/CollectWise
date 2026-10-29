import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.app.agent.llm import LLMClient, LLMUnavailableError
from backend.app.agent.sessions import ConversationSession
from backend.app.services.conversation_events import (
    ConversationEvent,
    record_conversation_events,
)
from backend.app.tools.base import ToolContext, ToolResult
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
) -> TurnResult:
    """Run one caller turn: model calls and tool calls until a reply."""
    with session.lock:
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
                             turn_started=turn_started)
        finally:
            # Written after the turn in a separate commit, so servicing
            # transactions and tool rollbacks never touch them (D48).
            record_conversation_events(db, events)


def _run_turn(
    session: ConversationSession,
    text: str,
    *,
    llm: LLMClient,
    db: Session,
    system: str,
    record: Callable[..., None],
    turn_started: float,
) -> TurnResult:
    state = session.state
    tool_calls: list[ToolCallRecord] = []

    def finish(reply: str, outcome: str) -> TurnResult:
        record("turn_completed", outcome=outcome, latency_ms=_elapsed_ms(turn_started))
        return TurnResult(
            reply=reply,
            handed_off=state.handoff is not None,
            tool_calls=tool_calls,
        )

    # After a handoff the agent no longer converses (D33).
    if state.handoff is not None:
        return finish(REPLY_HANDED_OFF, "already_handed_off")

    # The only place a caller turn is counted (D24).
    state.start_caller_turn()
    record("turn_started")
    session.messages.append({"role": "user", "content": text})

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
            response = llm.create(system=system, tools=tools, messages=session.messages)
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
