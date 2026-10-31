import json
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.db import models
from backend.app.tools import payments
from backend.app.tools.base import ToolContext, ToolResult
from backend.app.tools.payments import (
    CancelPaymentArgs,
    ConfirmPaymentArgs,
    ProposePaymentArgs,
    cancel_payment,
    confirm_payment,
    propose_payment,
)
from backend.app.tools.registry import TOOLS
from backend.tests.helpers import context_for, seed_customer


@pytest.fixture
def ctx(db_session: Session) -> ToolContext:
    # cust_a owns an active account with 500.00 available.
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    return context_for(db_session, "cust_a")


def propose(ctx: ToolContext, amount: str, account_id: str = "acct_a_1") -> ToolResult:
    return propose_payment(
        ctx,
        ProposePaymentArgs(account_id=account_id, amount=Decimal(amount)),
    )


def confirm(ctx: ToolContext) -> ToolResult:
    return confirm_payment(ctx, ConfirmPaymentArgs())


def payment_records(db: Session) -> list[models.ServicingRequestRecord]:
    return list(
        db.scalars(
            select(models.ServicingRequestRecord).where(
                models.ServicingRequestRecord.operation == "request_payment"
            )
        )
    )


def test_propose_then_confirm_records_exactly_the_proposed_payment(
    ctx: ToolContext,
) -> None:
    proposal = propose(ctx, "200.00")

    assert proposal == ToolResult.success(
        account_id="acct_a_1",
        amount="200.00",
        currency="USD",
        requires_confirmation=True,
    )
    assert payment_records(ctx.db) == []

    ctx.state.start_caller_turn()  # the caller says "yes"
    result = confirm(ctx)

    assert result.ok is True
    assert result.data["status"] == "recorded"
    assert result.data["replayed"] is False
    json.dumps(result.data)

    # The model never sees the request ID, so it can't read it out (D55).
    assert "request_id" not in result.data
    assert "pay_" not in json.dumps(result.data)

    [record] = payment_records(ctx.db)
    assert record.customer_id == "cust_a"
    assert record.amount == Decimal("200.00")
    assert record.decision == "allow"


def test_confirm_in_the_same_turn_as_the_proposal_is_refused(
    ctx: ToolContext,
) -> None:
    propose(ctx, "200.00")

    result = confirm(ctx)

    assert result == ToolResult.failure("confirmation_required")
    assert payment_records(ctx.db) == []
    assert ctx.state.pending_payment is not None


def test_confirm_without_a_proposal_is_refused(ctx: ToolContext) -> None:
    ctx.state.start_caller_turn()

    assert confirm(ctx) == ToolResult.failure("no_pending_payment")


def test_repeated_confirm_replays_instead_of_duplicating(ctx: ToolContext) -> None:
    propose(ctx, "200.00")
    ctx.state.start_caller_turn()

    first = confirm(ctx)
    second = confirm(ctx)

    assert second.data == {**first.data, "replayed": True}
    assert len(payment_records(ctx.db)) == 1


@pytest.mark.parametrize(
    ("amount", "reason"),
    [
        ("500.01", "insufficient_funds"),
        ("10000.01", "payment_exceeds_limit"),
    ],
)
def test_denied_proposal_holds_and_persists_nothing(
    ctx: ToolContext,
    amount: str,
    reason: str,
) -> None:
    result = propose(ctx, amount)

    assert result == ToolResult.failure(reason)
    assert ctx.state.pending_payment is None
    assert payment_records(ctx.db) == []


def test_proposal_from_frozen_account_is_denied(db_session: Session) -> None:
    seed_customer(db_session, "cust_f", [("acct_f_1", "USD", "frozen")])
    ctx = context_for(db_session, "cust_f")

    result = propose(ctx, "10.00", account_id="acct_f_1")

    assert result == ToolResult.failure("account_not_active")
    assert ctx.state.pending_payment is None


def test_confirm_rechecks_policy_when_funds_dropped(ctx: ToolContext) -> None:
    propose(ctx, "400.00")

    # The balance changes between the proposal and the confirmation.
    account = ctx.db.get(models.AccountRecord, "acct_a_1")
    account.balance = Decimal("100.00")
    ctx.db.commit()

    ctx.state.start_caller_turn()
    result = confirm(ctx)

    assert result == ToolResult.failure("insufficient_funds")
    [record] = payment_records(ctx.db)
    assert record.decision == "deny"
    assert record.reason == "insufficient_funds"


def test_repeated_confirm_after_a_denial_replays_the_denial(
    ctx: ToolContext,
) -> None:
    propose(ctx, "400.00")
    account = ctx.db.get(models.AccountRecord, "acct_a_1")
    account.balance = Decimal("100.00")
    ctx.db.commit()
    ctx.state.start_caller_turn()

    first = confirm(ctx)
    second = confirm(ctx)

    assert first == ToolResult.failure("insufficient_funds")
    assert second == ToolResult.failure("insufficient_funds")
    assert ctx.state.pending_payment.submitted is True
    assert len(payment_records(ctx.db)) == 1


def test_changed_proposal_must_be_heard_before_it_is_confirmed(
    ctx: ToolContext,
) -> None:
    propose(ctx, "200.00")
    ctx.state.start_caller_turn()  # "Yes... actually, make it 300."

    propose(ctx, "300.00")
    result = confirm(ctx)

    # The caller has not heard 300 yet, so it cannot be confirmed this turn.
    assert result == ToolResult.failure("confirmation_required")
    assert payment_records(ctx.db) == []

    ctx.state.start_caller_turn()  # "Yes, 300."
    result = confirm(ctx)

    assert result.data["amount"] == "300.00"
    [record] = payment_records(ctx.db)
    assert record.amount == Decimal("300.00")


def test_new_proposal_replaces_the_pending_one(ctx: ToolContext) -> None:
    propose(ctx, "100.00")
    propose(ctx, "50.00")
    ctx.state.start_caller_turn()

    result = confirm(ctx)

    assert result.data["amount"] == "50.00"
    [record] = payment_records(ctx.db)
    assert record.amount == Decimal("50.00")


def test_cancel_discards_the_pending_payment(ctx: ToolContext) -> None:
    propose(ctx, "100.00")

    result = cancel_payment(ctx, CancelPaymentArgs())

    assert result.ok is True
    assert result.data["status"] == "cancelled"
    assert ctx.state.pending_payment is None

    ctx.state.start_caller_turn()
    assert confirm(ctx) == ToolResult.failure("no_pending_payment")
    assert payment_records(ctx.db) == []


def test_cancel_after_submission_is_refused(ctx: ToolContext) -> None:
    propose(ctx, "100.00")
    ctx.state.start_caller_turn()
    confirm(ctx)

    result = cancel_payment(ctx, CancelPaymentArgs())

    assert result == ToolResult.failure("payment_already_submitted")


def test_cancel_with_nothing_pending_is_refused(ctx: ToolContext) -> None:
    assert cancel_payment(ctx, CancelPaymentArgs()) == ToolResult.failure(
        "no_pending_payment"
    )


def test_proposal_for_another_customers_account_looks_missing(
    ctx: ToolContext,
) -> None:
    seed_customer(ctx.db, "cust_b", [("acct_b_1", "USD", "active")])

    result = propose(ctx, "10.00", account_id="acct_b_1")

    assert result == ToolResult.failure("account_not_found")
    assert ctx.state.pending_payment is None


def test_proposal_for_missing_account(ctx: ToolContext) -> None:
    result = propose(ctx, "10.00", account_id="acct_missing")

    assert result == ToolResult.failure("account_not_found")


def test_confirm_takes_no_arguments() -> None:
    # The LLM cannot confirm a different amount or account than was proposed.
    assert ConfirmPaymentArgs.model_fields == {}

    with pytest.raises(ValidationError):
        ConfirmPaymentArgs(amount="2000.00")


@pytest.mark.parametrize(
    "bad_args",
    [
        {"account_id": "acct_a_1", "amount": "0"},
        {"account_id": "acct_a_1", "amount": "10.001"},
        {"account_id": "acct_a_1", "amount": "10.00", "customer_id": "cust_b"},
    ],
)
def test_propose_args_reject_invalid_input(bad_args: dict) -> None:
    with pytest.raises(ValidationError):
        ProposePaymentArgs(**bad_args)


# --- expiry (D73) -------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(payments, "_now", fake)
    return fake


def later_turns(ctx: ToolContext, count: int) -> None:
    for _ in range(count):
        ctx.state.start_caller_turn()


EXPIRED_120 = ToolResult.failure("payment_expired", account_id="acct_a_1", amount="120.00", currency="USD")


def test_shipped_window_is_two_turns_and_five_minutes() -> None:
    assert (config.PAYMENT_CONFIRM_TURNS, config.PAYMENT_CONFIRM_MINUTES) == (2, 5)


@pytest.mark.parametrize("turns_later", [1, 2])
def test_confirm_within_two_later_turns_works(ctx, clock, turns_later) -> None:
    propose(ctx, "120.00")
    later_turns(ctx, turns_later)

    assert confirm(ctx).ok
    assert len(payment_records(ctx.db)) == 1


def test_confirm_three_turns_later_has_expired(ctx, clock) -> None:
    propose(ctx, "120.00")
    later_turns(ctx, 3)

    assert confirm(ctx) == EXPIRED_120
    assert payment_records(ctx.db) == []
    assert ctx.state.pending_payment is None
    assert confirm(ctx) == ToolResult.failure("no_pending_payment")


@pytest.mark.parametrize(("seconds", "expired"), [(299.9, False), (300, True)])
def test_five_minutes_after_the_proposal_it_has_expired(ctx, clock, seconds, expired) -> None:
    propose(ctx, "120.00")
    later_turns(ctx, 1)
    clock.now += seconds

    result = confirm(ctx)

    assert (result == EXPIRED_120) is expired
    assert len(payment_records(ctx.db)) == (0 if expired else 1)


def test_an_expired_payment_is_never_checked_against_the_policy(ctx, clock) -> None:
    # Even an amount the policy would now deny reports only that it expired.
    propose(ctx, "120.00")
    ctx.db.get(models.AccountRecord, "acct_a_1").status = "frozen"
    ctx.db.commit()
    later_turns(ctx, 3)

    assert confirm(ctx) == EXPIRED_120
    assert ctx.db.scalars(select(models.ServicingAuditEvent)).all() == []


def test_a_new_proposal_after_expiry_needs_its_own_later_turn(ctx, clock) -> None:
    propose(ctx, "120.00")
    later_turns(ctx, 3)
    confirm(ctx)

    propose(ctx, "120.00")
    assert confirm(ctx) == ToolResult.failure("confirmation_required")
    later_turns(ctx, 1)
    assert confirm(ctx).ok


def test_a_submitted_payment_still_replays_after_the_window(ctx, clock) -> None:
    propose(ctx, "120.00")
    later_turns(ctx, 1)
    first = confirm(ctx)
    later_turns(ctx, 5)
    clock.now += 3600

    again = confirm(ctx)

    assert again.data == {**first.data, "replayed": True}
    assert len(payment_records(ctx.db)) == 1


def test_the_window_is_read_at_call_time(ctx, clock, monkeypatch) -> None:
    propose(ctx, "120.00")
    later_turns(ctx, 1)
    monkeypatch.setattr(config, "PAYMENT_CONFIRM_MINUTES", 0.5)
    clock.now += 30

    assert confirm(ctx) == EXPIRED_120

    propose(ctx, "120.00")
    later_turns(ctx, 1)
    monkeypatch.setattr(config, "PAYMENT_CONFIRM_TURNS", 0)
    assert confirm(ctx) == EXPIRED_120


def test_the_model_is_told_what_to_do_with_an_expired_payment() -> None:
    description = TOOLS["confirm_payment"].description

    assert "payment_expired" in description
    assert "propose it again" in description


# --- an interrupted read-back (D82) ---------------------------------------------


def test_a_proposal_whose_read_back_wasnt_heard_cant_be_confirmed(ctx, clock) -> None:
    from dataclasses import replace

    propose(ctx, "120.00")
    ctx.state.pending_payment = replace(ctx.state.pending_payment, readback_heard=False)
    later_turns(ctx, 1)

    assert confirm(ctx) == ToolResult.failure(
        "readback_not_heard", account_id="acct_a_1", amount="120.00", currency="USD"
    )
    assert payment_records(ctx.db) == []
    assert ctx.db.scalars(select(models.ServicingAuditEvent)).all() == []
    assert ctx.state.pending_payment is None


def test_a_new_proposal_is_heard_by_default(ctx, clock) -> None:
    propose(ctx, "120.00")

    assert ctx.state.pending_payment.readback_heard is True


def test_the_model_is_told_what_to_do_with_an_unheard_read_back() -> None:
    assert "readback_not_heard" in TOOLS["confirm_payment"].description
