import time
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.app import config
from backend.app.policies.servicing import PolicyDecision
from backend.app.schemas.domain import ServicingOperation, ServicingRequest
from backend.app.services.servicing import (
    AccountNotFoundError,
    evaluate_servicing_request,
    preview_servicing_request,
)
from backend.app.tools.base import (
    PendingPayment,
    ToolContext,
    ToolResult,
    caller_facing_reason,
)


def _now() -> float:
    # A seam for tests.
    return time.monotonic()


def _payment_request(
    ctx: ToolContext,
    request_id: str,
    account_id: str,
    amount: Decimal,
) -> ServicingRequest:
    return ServicingRequest(
        request_id=request_id,
        customer_id=ctx.caller.customer_id,
        account_id=account_id,
        operation=ServicingOperation.REQUEST_PAYMENT,
        amount=amount,
    )


class ProposePaymentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(
        min_length=1,
        max_length=50,
        description="The caller's account to pay from, as returned by list_accounts.",
    )
    amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
        description="Amount in the account's currency, at most 2 decimal places.",
    )


class ConfirmPaymentArgs(BaseModel):
    """No arguments: confirmation submits exactly the pending payment."""

    model_config = ConfigDict(extra="forbid")


class CancelPaymentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def propose_payment(ctx: ToolContext, args: ProposePaymentArgs) -> ToolResult:
    """Check a payment request and hold it until the caller confirms it.

    Nothing is persisted. The agent must read the details back to the caller.
    """
    request = _payment_request(
        ctx,
        f"pay_{uuid4().hex}",
        args.account_id,
        args.amount,
    )

    try:
        preview = preview_servicing_request(ctx.db, request)
    except AccountNotFoundError:
        return ToolResult.failure("account_not_found")

    if preview.decision != PolicyDecision.ALLOW:
        return ToolResult.failure(caller_facing_reason(preview.reason))

    pending = PendingPayment(
        request_id=request.request_id,
        account_id=request.account_id,
        amount=args.amount,
        currency=preview.currency,
        proposed_turn=ctx.state.turn,
        proposed_at=_now(),
    )
    # A new proposal replaces any earlier one, submitted or not.
    ctx.state.pending_payment = pending

    return ToolResult.success(
        account_id=pending.account_id,
        amount=str(pending.amount),
        currency=pending.currency,
        requires_confirmation=True,
    )


def confirm_payment(ctx: ToolContext, args: ConfirmPaymentArgs) -> ToolResult:
    """Submit the pending payment request after the caller has confirmed it.

    The policy runs again, and the request is persisted and audited. A repeated
    confirmation replays the stored decision instead of creating a new one.
    """
    pending = ctx.state.pending_payment

    if pending is None:
        return ToolResult.failure("no_pending_payment")

    # The caller must have spoken since the proposal, so a payment can never
    # be submitted before the caller has heard it.
    if not pending.submitted and ctx.state.turn <= pending.proposed_turn:
        return ToolResult.failure("confirmation_required")

    # ...and recently, so a late "yes" about something else can't submit a
    # proposal the caller has forgotten (D73). Submitted ones only replay.
    if not pending.submitted and (
        ctx.state.turn - pending.proposed_turn > config.PAYMENT_CONFIRM_TURNS
        or _now() - pending.proposed_at >= config.PAYMENT_CONFIRM_MINUTES * 60
    ):
        ctx.state.pending_payment = None
        return ToolResult.failure(
            "payment_expired",
            account_id=pending.account_id,
            amount=str(pending.amount),
            currency=pending.currency,
        )

    try:
        decision = evaluate_servicing_request(
            ctx.db,
            _payment_request(ctx, pending.request_id, pending.account_id, pending.amount),
        )
    except AccountNotFoundError:
        ctx.state.pending_payment = None
        return ToolResult.failure("account_not_found")

    ctx.state.pending_payment = replace(pending, submitted=True)

    if decision.decision != PolicyDecision.ALLOW:
        return ToolResult.failure(caller_facing_reason(decision.reason))

    # No request ID: the model can't read out what it never sees (D55).
    return ToolResult.success(
        account_id=pending.account_id,
        amount=str(pending.amount),
        currency=pending.currency,
        # Recorded for processing; no money moves in this system.
        status="recorded",
        replayed=decision.replayed,
    )


def cancel_payment(ctx: ToolContext, args: CancelPaymentArgs) -> ToolResult:
    """Discard the pending payment if it has not been submitted."""
    pending = ctx.state.pending_payment

    if pending is None:
        return ToolResult.failure("no_pending_payment")

    if pending.submitted:
        return ToolResult.failure("payment_already_submitted")

    ctx.state.pending_payment = None

    return ToolResult.success(
        account_id=pending.account_id,
        amount=str(pending.amount),
        currency=pending.currency,
        status="cancelled",
    )
