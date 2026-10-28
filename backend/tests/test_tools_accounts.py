import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.tools.accounts import (
    GetAccountSummaryArgs,
    ListAccountsArgs,
    get_account_summary,
    list_accounts,
)
from backend.app.tools.base import ToolResult
from backend.tests.helpers import context_for, seed_customer


def test_tool_result_success_and_failure() -> None:
    assert ToolResult.success(value=1) == ToolResult(ok=True, data={"value": 1})
    assert ToolResult.failure("not_allowed") == ToolResult(ok=False, error="not_allowed")


def test_list_accounts_returns_only_the_callers_accounts(db_session: Session) -> None:
    seed_customer(
        db_session,
        "cust_a",
        [("acct_a_2", "USD", "frozen"), ("acct_a_1", "INR", "active")],
    )
    seed_customer(db_session, "cust_b", [("acct_b_1", "USD", "active")])

    result = list_accounts(context_for(db_session, "cust_a"), ListAccountsArgs())

    assert result.ok is True
    assert result.data["accounts"] == [
        {"account_id": "acct_a_1", "currency": "INR", "status": "active"},
        {"account_id": "acct_a_2", "currency": "USD", "status": "frozen"},
    ]


def test_list_accounts_does_not_expose_balances(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])

    result = list_accounts(context_for(db_session, "cust_a"), ListAccountsArgs())

    assert "balance" not in result.data["accounts"][0]


def test_list_accounts_for_customer_without_accounts(db_session: Session) -> None:
    seed_customer(db_session, "cust_empty", [])

    result = list_accounts(context_for(db_session, "cust_empty"), ListAccountsArgs())

    assert result == ToolResult.success(accounts=[])


def test_list_accounts_result_is_json_safe(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])

    result = list_accounts(context_for(db_session, "cust_a"), ListAccountsArgs())

    json.dumps(result.data)


def test_list_accounts_args_reject_a_customer_id() -> None:
    # The LLM cannot choose whose accounts to list.
    with pytest.raises(ValidationError):
        ListAccountsArgs(customer_id="cust_b")


def stored_requests(db: Session) -> list[models.ServicingRequestRecord]:
    return list(db.scalars(select(models.ServicingRequestRecord)))


def summary_for(db: Session, customer_id: str, account_id: str) -> ToolResult:
    return get_account_summary(
        context_for(db, customer_id),
        GetAccountSummaryArgs(account_id=account_id),
    )


def test_summary_of_own_active_account_includes_balance(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])

    result = summary_for(db_session, "cust_a", "acct_a_1")

    assert result == ToolResult.success(
        account_id="acct_a_1",
        balance="500.00",
        currency="USD",
        status="active",
    )
    json.dumps(result.data)


def test_summary_view_is_audited_as_the_caller(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])

    summary_for(db_session, "cust_a", "acct_a_1")

    [record] = stored_requests(db_session)
    assert record.customer_id == "cust_a"
    assert record.operation == "view_account"
    assert record.decision == "allow"
    assert record.request_id.startswith("view_")


def test_each_summary_call_is_a_separate_audited_request(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])

    summary_for(db_session, "cust_a", "acct_a_1")
    summary_for(db_session, "cust_a", "acct_a_1")

    assert len({r.request_id for r in stored_requests(db_session)}) == 2


def test_summary_of_own_frozen_account_is_allowed(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "frozen")])

    result = summary_for(db_session, "cust_a", "acct_a_1")

    assert result.ok is True
    assert result.data["status"] == "frozen"


def test_summary_of_closed_account_is_refused(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "closed")])

    result = summary_for(db_session, "cust_a", "acct_a_1")

    assert result == ToolResult.failure("account_not_active")


def test_other_customers_account_looks_missing_but_is_audited(
    db_session: Session,
) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    seed_customer(db_session, "cust_b", [("acct_b_1", "USD", "active")])

    result = summary_for(db_session, "cust_a", "acct_b_1")

    assert result == ToolResult.failure("account_not_found")
    assert "balance" not in result.data

    [record] = stored_requests(db_session)
    assert record.customer_id == "cust_a"
    assert record.decision == "deny"
    assert record.reason == "customer_account_mismatch"


def test_summary_of_missing_account_persists_nothing(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])

    result = summary_for(db_session, "cust_a", "acct_missing")

    assert result == ToolResult.failure("account_not_found")
    assert stored_requests(db_session) == []


def test_summary_args_reject_a_customer_id() -> None:
    with pytest.raises(ValidationError):
        GetAccountSummaryArgs(account_id="acct_b_1", customer_id="cust_b")
