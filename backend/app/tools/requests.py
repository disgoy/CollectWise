from pydantic import BaseModel, ConfigDict, Field

from backend.app.services.servicing import (
    ServicingRequestNotFoundError,
    get_customer_servicing_request,
    list_customer_payment_requests,
)
from backend.app.tools.base import ToolContext, ToolResult, caller_facing_reason


class GetRequestStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        max_length=100,
        description="A request ID seen earlier in this conversation, e.g. from confirm_payment.",
    )


def get_request_status(ctx: ToolContext, args: GetRequestStatusArgs) -> ToolResult:
    """Return the stored outcome of one of the caller's servicing requests."""
    try:
        history = get_customer_servicing_request(
            ctx.db,
            ctx.caller.customer_id,
            args.request_id,
        )
    except ServicingRequestNotFoundError:
        return ToolResult.failure("request_not_found")

    return ToolResult.success(
        request_id=history.request_id,
        account_id=history.account_id,
        operation=history.operation.value,
        amount=None if history.amount is None else str(history.amount),
        decision=history.decision.value,
        reason=caller_facing_reason(history.reason),
        status=history.status,
        created_at=history.created_at.isoformat(),
    )


class ListRecentPaymentRequestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=3,
        ge=1,
        le=5,
        description="How many of the most recent payment requests to return (1-5).",
    )


def list_recent_payment_requests(
    ctx: ToolContext,
    args: ListRecentPaymentRequestsArgs,
) -> ToolResult:
    """List the caller's most recent payment requests, newest first.

    Lets the agent answer "what happened to my payment?" without the caller
    having to say a request ID.
    """
    requests = list_customer_payment_requests(
        ctx.db,
        ctx.caller.customer_id,
        args.limit,
    )

    return ToolResult.success(
        payment_requests=[
            {
                "request_id": request.request_id,
                "account_id": request.account_id,
                "amount": None if request.amount is None else str(request.amount),
                "currency": request.currency,
                "decision": request.decision.value,
                "reason": caller_facing_reason(request.reason),
                "status": request.status,
                "created_at": request.created_at.isoformat(),
            }
            for request in requests
        ]
    )
