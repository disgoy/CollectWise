from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.tools.base import ConversationState, Handoff, ToolContext, ToolResult


# A fixed reason code only. No free-text summary: the LLM could copy the
# caller's words or account details into it, or carry a prompt injection to the
# human agent. (A comment, not a docstring: Pydantic sends docstrings to the
# model as the schema description.)
class HandoffToHumanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "caller_requested",
        "out_of_scope",
        "unresolved_issue",
        "caller_distressed",
    ] = Field(description="Why the conversation is being handed to a human.")


def hand_off(state: ConversationState, reason: str) -> dict | None:
    """Hand the conversation to a human. A stub: no real queue yet.

    Used by the tool below and by code-driven handoffs (the turn limit, D72),
    whose reasons the model is never offered. A payment that was proposed but
    not submitted is cancelled, so nothing is left waiting for a confirmation
    the agent will no longer handle; it is returned. Submitted payments are
    recorded decisions and are left alone. This is conversation state, never a
    servicing audit event. The caller checks there was no handoff already.
    """
    cancelled_payment = None
    pending = state.pending_payment

    if pending is not None and not pending.submitted:
        cancelled_payment = {
            "account_id": pending.account_id,
            "amount": str(pending.amount),
            "currency": pending.currency,
        }
        state.pending_payment = None

    state.handoff = Handoff(reason=reason, turn=state.turn)
    return cancelled_payment


def handoff_to_human(ctx: ToolContext, args: HandoffToHumanArgs) -> ToolResult:
    """Hand the conversation to a human."""
    if ctx.state.handoff is not None:
        return ToolResult.failure("conversation_handed_off")

    cancelled_payment = hand_off(ctx.state, args.reason)

    return ToolResult.success(
        status="handoff_requested",
        reason=args.reason,
        cancelled_payment=cancelled_payment,
    )
