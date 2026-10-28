from pydantic import BaseModel, ConfigDict, Field

from backend.app.services.servicing import (
    ServicingRequestNotFoundError,
    get_customer_servicing_request,
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
