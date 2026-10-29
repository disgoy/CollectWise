import json
from datetime import datetime, timezone
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
from backend.app.tools.requests import (
    GetRequestStatusArgs,
    ListRecentPaymentRequestsArgs,
    get_request_status,
    list_recent_payment_requests,
)
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


def add_request(
    db: Session,
    request_id: str,
    *,
    customer_id: str = "cust_a",
    account_id: str = "acct_a_1",
    operation: str = "request_payment",
    amount: str | None = "10.00",
    decision: str = "allow",
    reason: str = "policy_checks_passed",
    minute: int = 0,
) -> None:
    """Insert a stored request directly, with a controlled creation time."""
    db.add(
        models.ServicingRequestRecord(
            request_id=request_id,
            customer_id=customer_id,
            account_id=account_id,
            operation=operation,
            amount=None if amount is None else Decimal(amount),
            decision=decision,
            reason=reason,
            created_at=datetime(2026, 10, 29, 10, minute, tzinfo=timezone.utc),
        )
    )
    db.commit()


def recent(ctx: ToolContext, **args) -> list[dict]:
    result = list_recent_payment_requests(ctx, ListRecentPaymentRequestsArgs(**args))
    assert result.ok is True
    json.dumps(result.data)
    return result.data["payment_requests"]


def test_recent_payments_newest_first_with_currency(ctx: ToolContext) -> None:
    add_request(ctx.db, "pay_old", amount="10.00", minute=1)
    add_request(ctx.db, "pay_new", amount="20.00", minute=3)
    add_request(ctx.db, "pay_mid", amount="15.00", minute=2)

    payments = recent(ctx)

    assert [p["request_id"] for p in payments] == ["pay_new", "pay_mid", "pay_old"]
    assert payments[0] == {
        "request_id": "pay_new",
        "account_id": "acct_a_1",
        "amount": "20.00",
        "currency": "USD",
        "decision": "allow",
        "reason": "policy_checks_passed",
        "status": "evaluated",
        "created_at": "2026-10-29T10:03:00+00:00",
    }


def test_recent_payments_respects_the_limit(ctx: ToolContext) -> None:
    for minute in range(6):
        add_request(ctx.db, f"pay_{minute}", minute=minute)

    assert len(recent(ctx)) == 3
    assert [p["request_id"] for p in recent(ctx, limit=2)] == ["pay_5", "pay_4"]


def test_recent_payments_exclude_views_and_other_customers(ctx: ToolContext) -> None:
    add_request(ctx.db, "view_1", operation="view_account", amount=None, minute=5)
    add_request(ctx.db, "pay_b", customer_id="cust_b", account_id="acct_b_1", minute=4)
    add_request(ctx.db, "pay_a", minute=1)

    assert [p["request_id"] for p in recent(ctx)] == ["pay_a"]


def test_recent_payments_reveal_nothing_about_other_accounts(ctx: ToolContext) -> None:
    # An old mismatched request by cust_a against cust_b's account.
    add_request(
        ctx.db,
        "pay_mismatch",
        account_id="acct_b_1",
        decision="deny",
        reason="customer_account_mismatch",
    )

    [payment] = recent(ctx)

    assert payment["currency"] is None
    assert payment["reason"] == "account_not_found"


def test_recent_payments_empty(ctx: ToolContext) -> None:
    assert recent(ctx) == []


@pytest.mark.parametrize(
    "bad_args",
    [{"limit": 0}, {"limit": 6}, {"limit": 3, "customer_id": "cust_b"}],
)
def test_recent_payments_args_reject_invalid_input(bad_args: dict) -> None:
    with pytest.raises(ValidationError):
        ListRecentPaymentRequestsArgs(**bad_args)
