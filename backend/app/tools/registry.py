import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.app.tools.accounts import (
    GetAccountSummaryArgs,
    ListAccountsArgs,
    get_account_summary,
    list_accounts,
)
from backend.app.tools.base import ToolContext, ToolResult
from backend.app.tools.handoff import HandoffToHumanArgs, handoff_to_human
from backend.app.tools.payments import (
    CancelPaymentArgs,
    ConfirmPaymentArgs,
    ProposePaymentArgs,
    cancel_payment,
    confirm_payment,
    propose_payment,
)
from backend.app.tools.requests import (
    GetRequestStatusArgs,
    ListRecentPaymentRequestsArgs,
    get_request_status,
    list_recent_payment_requests,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    # Written for the LLM: what the tool does and the rules for using it.
    description: str
    args_model: type[BaseModel]
    function: Callable[[ToolContext, Any], ToolResult]


_SPECS = [
    ToolSpec(
        name="list_accounts",
        description=(
            "List the caller's accounts (account ID, currency, status). Use it "
            "to find out which account the caller means. Does not return balances."
        ),
        args_model=ListAccountsArgs,
        function=list_accounts,
    ),
    ToolSpec(
        name="get_account_summary",
        description=(
            "Get the balance and status of one of the caller's accounts. Every "
            "call is recorded, so only use it when the caller asks about an account."
        ),
        args_model=GetAccountSummaryArgs,
        function=get_account_summary,
    ),
    ToolSpec(
        name="propose_payment",
        description=(
            "Prepare a payment request from one of the caller's accounts. Nothing "
            "is submitted. If it succeeds, read the amount, currency and account "
            "back to the caller, ask them to confirm, and wait for their answer. "
            "Never call confirm_payment in the same turn as propose_payment."
        ),
        args_model=ProposePaymentArgs,
        function=propose_payment,
    ),
    ToolSpec(
        name="confirm_payment",
        description=(
            "Submit the pending payment request exactly as proposed. Only call it "
            "after the caller has clearly agreed in a later message. The request "
            "is recorded for processing: say it was recorded, never that it was paid."
        ),
        args_model=ConfirmPaymentArgs,
        function=confirm_payment,
    ),
    ToolSpec(
        name="cancel_payment",
        description=(
            "Cancel the pending payment request when the caller declines or "
            "changes their mind before confirming."
        ),
        args_model=CancelPaymentArgs,
        function=cancel_payment,
    ),
    ToolSpec(
        name="get_request_status",
        description=(
            "Look up the outcome of one of the caller's servicing requests by a "
            "request ID the caller gives you, for example from the CollectWise app. "
            "For payments made in this conversation, use list_recent_payment_requests."
        ),
        args_model=GetRequestStatusArgs,
        function=get_request_status,
    ),
    ToolSpec(
        name="list_recent_payment_requests",
        description=(
            "List the caller's most recent payment requests, newest first, with "
            "amount, currency, decision and time. Use it when the caller asks "
            "what happened to a payment."
        ),
        args_model=ListRecentPaymentRequestsArgs,
        function=list_recent_payment_requests,
    ),
    ToolSpec(
        name="handoff_to_human",
        description=(
            "Transfer the conversation to a human agent: when the caller asks for "
            "a person, the request is outside these tools, the issue can't be "
            "resolved, or the caller is distressed. No tools can be used afterwards."
        ),
        args_model=HandoffToHumanArgs,
        function=handoff_to_human,
    ),
]

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}


def tool_schemas() -> list[dict[str, Any]]:
    """Provider-neutral tool definitions for the LLM.

    Converting them to a specific provider's format happens in the agent layer.
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.args_model.model_json_schema(),
        }
        for spec in TOOLS.values()
    ]


def execute_tool(
    ctx: ToolContext,
    name: str,
    raw_args: Any,
) -> ToolResult:
    """The only way the agent runs a tool. Never raises at the LLM."""
    spec = TOOLS.get(name)

    if spec is None:
        return ToolResult.failure("unknown_tool")

    # After a handoff the agent must stop acting, whatever the tool (D33).
    if ctx.state.handoff is not None:
        return ToolResult.failure("conversation_handed_off")

    try:
        args = spec.args_model.model_validate(raw_args)
    except ValidationError as exc:
        # Report which fields failed, never their values: they may hold
        # caller data.
        fields = sorted(
            {".".join(str(part) for part in error["loc"]) or "arguments"
             for error in exc.errors()}
        )
        return ToolResult.failure("invalid_arguments", fields=fields)

    try:
        return spec.function(ctx, args)
    except Exception:
        # Undo any half-finished database work so the session stays usable.
        ctx.db.rollback()
        # The traceback stays in our logs; the arguments are not logged.
        logger.exception("Tool %s raised an unexpected error", name)
        return ToolResult.failure("internal_error")
