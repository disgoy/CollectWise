from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.app.tools.base import Handoff, ToolContext, ToolResult


class HandoffToHumanArgs(BaseModel):
    """A fixed reason code only. No free-text summary: the LLM could copy the
    caller's words or account details into it, or carry a prompt injection to
    the human agent."""

    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "caller_requested",
        "out_of_scope",
        "unresolved_issue",
        "caller_distressed",
    ]


def handoff_to_human(ctx: ToolContext, args: HandoffToHumanArgs) -> ToolResult:
    """Hand the conversation to a human. A stub: no real queue yet.

    A payment that was proposed but not submitted is cancelled, so nothing is
    left waiting for a confirmation the agent will no longer handle. Submitted
    payments are recorded decisions and are left alone. This is conversation
    state, never a servicing audit event.
    """
    if ctx.state.handoff is not None:
        return ToolResult.failure("conversation_handed_off")

    cancelled_payment = None
    pending = ctx.state.pending_payment

    if pending is not None and not pending.submitted:
        cancelled_payment = {
            "account_id": pending.account_id,
            "amount": str(pending.amount),
            "currency": pending.currency,
        }
        ctx.state.pending_payment = None

    ctx.state.handoff = Handoff(reason=args.reason, turn=ctx.state.turn)

    return ToolResult.success(
        status="handoff_requested",
        reason=args.reason,
        cancelled_payment=cancelled_payment,
    )
