import json
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.tools.base import Handoff, ToolContext, ToolResult
from backend.app.tools.handoff import HandoffToHumanArgs, handoff_to_human
from backend.app.tools.payments import (
    ConfirmPaymentArgs,
    ProposePaymentArgs,
    confirm_payment,
    propose_payment,
)
from backend.tests.helpers import context_for, seed_customer


@pytest.fixture
def ctx(db_session: Session) -> ToolContext:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    return context_for(db_session, "cust_a")


def handoff(ctx: ToolContext, reason: str = "caller_requested") -> ToolResult:
    return handoff_to_human(ctx, HandoffToHumanArgs(reason=reason))


def propose(ctx: ToolContext, amount: str) -> None:
    propose_payment(
        ctx,
        ProposePaymentArgs(account_id="acct_a_1", amount=Decimal(amount)),
    )


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_handoff_is_recorded_in_conversation_state(ctx: ToolContext) -> None:
    ctx.state.start_caller_turn()

    result = handoff(ctx, "out_of_scope")

    assert result == ToolResult.success(
        status="handoff_requested",
        reason="out_of_scope",
        cancelled_payment=None,
    )
    assert ctx.state.handoff == Handoff(reason="out_of_scope", turn=1)
    json.dumps(result.data)


def test_handoff_writes_nothing_to_the_servicing_audit_trail(ctx: ToolContext) -> None:
    handoff(ctx)

    assert count(ctx.db, models.ServicingRequestRecord) == 0
    assert count(ctx.db, models.ServicingAuditEvent) == 0


def test_handoff_cancels_an_unsubmitted_payment(ctx: ToolContext) -> None:
    propose(ctx, "200.00")

    result = handoff(ctx)

    assert result.data["cancelled_payment"] == {
        "account_id": "acct_a_1",
        "amount": "200.00",
        "currency": "USD",
    }
    assert ctx.state.pending_payment is None

    # Even if the caller now says "yes", there is nothing left to submit.
    ctx.state.start_caller_turn()
    assert confirm_payment(ctx, ConfirmPaymentArgs()) == ToolResult.failure(
        "no_pending_payment"
    )
    assert count(ctx.db, models.ServicingRequestRecord) == 0


def test_handoff_leaves_a_submitted_payment_alone(ctx: ToolContext) -> None:
    propose(ctx, "200.00")
    ctx.state.start_caller_turn()
    confirm_payment(ctx, ConfirmPaymentArgs())

    result = handoff(ctx)

    assert result.data["cancelled_payment"] is None
    assert ctx.state.pending_payment.submitted is True
    assert count(ctx.db, models.ServicingRequestRecord) == 1


def test_second_handoff_is_refused_and_keeps_the_first(ctx: ToolContext) -> None:
    handoff(ctx, "caller_requested")

    result = handoff(ctx, "unresolved_issue")

    assert result == ToolResult.failure("conversation_handed_off")
    assert ctx.state.handoff.reason == "caller_requested"


@pytest.mark.parametrize(
    "bad_args",
    [
        {"reason": "the caller wants to dispute a charge"},
        {"reason": "other"},
        {"reason": "caller_requested", "summary": "Caller said their PIN is 1234"},
        {"reason": "caller_requested", "customer_id": "cust_b"},
        {},
    ],
)
def test_handoff_args_accept_only_a_reason_code(bad_args: dict) -> None:
    with pytest.raises(ValidationError):
        HandoffToHumanArgs(**bad_args)
