import json
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.tools.accounts import GetAccountSummaryArgs, get_account_summary
from backend.app.tools.base import ToolContext, ToolResult
from backend.app.tools.payments import (
    ConfirmPaymentArgs,
    ProposePaymentArgs,
    confirm_payment,
    propose_payment,
)
from backend.app.tools.requests import GetRequestStatusArgs, get_request_status
from backend.tests.helpers import context_for, seed_customer


@pytest.fixture
def ctx(db_session: Session) -> ToolContext:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    seed_customer(db_session, "cust_b", [("acct_b_1", "USD", "active")])
    return context_for(db_session, "cust_a")


def status_of(ctx: ToolContext, request_id: str) -> ToolResult:
    return get_request_status(ctx, GetRequestStatusArgs(request_id=request_id))


def record_payment(ctx: ToolContext, amount: str) -> str:
    propose_payment(
        ctx,
        ProposePaymentArgs(account_id="acct_a_1", amount=Decimal(amount)),
    )
    ctx.state.start_caller_turn()
    return confirm_payment(ctx, ConfirmPaymentArgs()).data["request_id"]


def test_status_of_own_payment_request(ctx: ToolContext) -> None:
    request_id = record_payment(ctx, "200.00")

    result = status_of(ctx, request_id)

    assert result.ok is True
    assert result.data["request_id"] == request_id
    assert result.data["account_id"] == "acct_a_1"
    assert result.data["operation"] == "request_payment"
    assert result.data["amount"] == "200.00"
    assert result.data["decision"] == "allow"
    assert result.data["reason"] == "policy_checks_passed"
    assert result.data["status"] == "evaluated"
    assert result.data["created_at"].endswith("+00:00")
    json.dumps(result.data)


def test_status_of_view_request_has_no_amount(ctx: ToolContext) -> None:
    get_account_summary(ctx, GetAccountSummaryArgs(account_id="acct_a_1"))
    [record] = ctx.db.scalars(select(models.ServicingRequestRecord))

    result = status_of(ctx, record.request_id)

    assert result.data["operation"] == "view_account"
    assert result.data["amount"] is None


def test_other_customers_request_looks_missing(ctx: ToolContext) -> None:
    other = context_for(ctx.db, "cust_b")
    propose_payment(
        other,
        ProposePaymentArgs(account_id="acct_b_1", amount=Decimal("50.00")),
    )
    other.state.start_caller_turn()
    other_request_id = confirm_payment(other, ConfirmPaymentArgs()).data["request_id"]

    result = status_of(ctx, other_request_id)

    assert result == ToolResult.failure("request_not_found")


def test_missing_request(ctx: ToolContext) -> None:
    assert status_of(ctx, "pay_does_not_exist") == ToolResult.failure("request_not_found")


def test_own_attempt_on_another_account_does_not_confirm_it_exists(
    ctx: ToolContext,
) -> None:
    # cust_a's own audited attempt to view cust_b's account.
    get_account_summary(ctx, GetAccountSummaryArgs(account_id="acct_b_1"))
    [record] = ctx.db.scalars(select(models.ServicingRequestRecord))
    assert record.reason == "customer_account_mismatch"

    result = status_of(ctx, record.request_id)

    assert result.data["decision"] == "deny"
    assert result.data["reason"] == "account_not_found"


@pytest.mark.parametrize(
    "bad_args",
    [
        {"request_id": ""},
        {"request_id": "x" * 101},
        {"request_id": "pay_1", "customer_id": "cust_b"},
    ],
)
def test_request_status_args_reject_invalid_input(bad_args: dict) -> None:
    with pytest.raises(ValidationError):
        GetRequestStatusArgs(**bad_args)
