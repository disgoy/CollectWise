from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import AccountRecord
from backend.app.policies.servicing import PolicyDecision
from backend.app.schemas.domain import (
    AccountStatus,
    ServicingOperation,
    ServicingRequest,
)
from backend.app.services.servicing import (
    ServicingDecision,
    evaluate_servicing_request,
)


@dataclass(frozen=True)
class AccountOverview:
    """Identifying details of an account, without its balance."""

    account_id: str
    currency: str
    status: AccountStatus


@dataclass(frozen=True)
class AccountSummary:
    """Account details including the balance (funds available)."""

    account_id: str
    balance: Decimal
    currency: str
    status: AccountStatus


@dataclass(frozen=True)
class AccountView:
    decision: ServicingDecision
    # Set only when the policy allowed the view.
    summary: AccountSummary | None


def list_customer_accounts(
    db: Session,
    customer_id: str,
) -> list[AccountOverview]:
    """Return the customer's accounts ordered by account ID."""
    records = db.scalars(
        select(AccountRecord)
        .where(AccountRecord.customer_id == customer_id)
        .order_by(AccountRecord.account_id)
    )

    return [
        AccountOverview(
            account_id=record.account_id,
            currency=record.currency,
            status=AccountStatus(record.status),
        )
        for record in records
    ]


def view_account(
    db: Session,
    *,
    request_id: str,
    customer_id: str,
    account_id: str,
) -> AccountView:
    """Evaluate a view_account request and read the balance only if allowed.

    The request and its decision are persisted and audited like any other
    servicing request. Raises AccountNotFoundError.
    """
    decision = evaluate_servicing_request(
        db,
        ServicingRequest(
            request_id=request_id,
            customer_id=customer_id,
            account_id=account_id,
            operation=ServicingOperation.VIEW_ACCOUNT,
        ),
    )

    if decision.decision != PolicyDecision.ALLOW:
        return AccountView(decision=decision, summary=None)

    record = db.get(AccountRecord, account_id)

    return AccountView(
        decision=decision,
        summary=AccountSummary(
            account_id=record.account_id,
            balance=record.balance,
            currency=record.currency,
            status=AccountStatus(record.status),
        ),
    )
