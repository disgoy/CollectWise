from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.app.services.accounts import list_customer_accounts, view_account
from backend.app.services.servicing import AccountNotFoundError
from backend.app.tools.base import ToolContext, ToolResult


class ListAccountsArgs(BaseModel):
    """list_accounts takes no arguments; the caller comes from the context."""

    model_config = ConfigDict(extra="forbid")


def list_accounts(ctx: ToolContext, args: ListAccountsArgs) -> ToolResult:
    """List the caller's accounts so the agent can tell which one they mean."""
    accounts = list_customer_accounts(ctx.db, ctx.caller.customer_id)

    return ToolResult.success(
        accounts=[
            {
                "account_id": account.account_id,
                "currency": account.currency,
                "status": account.status.value,
            }
            for account in accounts
        ]
    )


class GetAccountSummaryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(min_length=1, max_length=50)


def get_account_summary(
    ctx: ToolContext,
    args: GetAccountSummaryArgs,
) -> ToolResult:
    """Return the balance and status of one of the caller's accounts.

    Every call is a new, audited view_account request with a server-generated
    request ID.
    """
    try:
        view = view_account(
            ctx.db,
            request_id=f"view_{uuid4().hex}",
            customer_id=ctx.caller.customer_id,
            account_id=args.account_id,
        )
    except AccountNotFoundError:
        return ToolResult.failure("account_not_found")

    if view.summary is None:
        # Someone else's account looks exactly like a missing one, so the
        # caller cannot probe which account IDs exist. The attempt is audited.
        if view.decision.reason == "customer_account_mismatch":
            return ToolResult.failure("account_not_found")
        return ToolResult.failure(view.decision.reason)

    summary = view.summary
    return ToolResult.success(
        account_id=summary.account_id,
        balance=str(summary.balance),
        currency=summary.currency,
        status=summary.status.value,
    )
