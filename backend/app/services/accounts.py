from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.models import AccountLabelRecord, AccountRecord, CustomerRecord
from backend.app.policies.servicing import PolicyDecision
from backend.app.schemas.domain import (
    AccountStatus,
    ServicingOperation,
    ServicingRequest,
    normalize_account_label,
)
from backend.app.services.servicing import (
    ServicingDecision,
    evaluate_servicing_request,
)


class CustomerNotFoundError(Exception):
    pass


class AccountConflictError(Exception):
    """The account ID is taken, or the record conflicts with another."""


class AccountLabelTakenError(AccountConflictError):
    """Another of this customer's accounts already has that label."""


@dataclass(frozen=True)
class AccountOverview:
    """Identifying details of an account, without its balance."""

    account_id: str
    currency: str
    status: AccountStatus
    # Spoken name such as "savings" (D58); None if the account has none.
    label: str | None


@dataclass(frozen=True)
class AccountDetails:
    """An account as the accounts API shows it."""

    account_id: str
    customer_id: str
    balance: Decimal
    currency: str
    status: AccountStatus
    label: str | None


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
    return [
        AccountOverview(
            account_id=details.account_id,
            currency=details.currency,
            status=details.status,
            label=details.label,
        )
        for details in list_customer_account_details(db, customer_id)
    ]


def _with_labels():
    return select(AccountRecord, AccountLabelRecord.label).outerjoin(
        AccountLabelRecord, AccountLabelRecord.account_id == AccountRecord.account_id
    )


def _details(record: AccountRecord, label: str | None) -> AccountDetails:
    return AccountDetails(
        account_id=record.account_id,
        customer_id=record.customer_id,
        balance=record.balance,
        currency=record.currency,
        status=AccountStatus(record.status),
        label=label,
    )


def list_customer_account_details(db: Session, customer_id: str) -> list[AccountDetails]:
    """Return the customer's accounts, with labels, ordered by account ID."""
    rows = db.execute(
        _with_labels()
        .where(AccountRecord.customer_id == customer_id)
        .order_by(AccountRecord.account_id)
    )
    return [_details(record, label) for record, label in rows]


def get_account_details(db: Session, account_id: str) -> AccountDetails | None:
    row = db.execute(_with_labels().where(AccountRecord.account_id == account_id)).first()
    return None if row is None else _details(*row)


def create_account(
    db: Session,
    *,
    account_id: str,
    customer_id: str,
    balance: Decimal,
    currency: str,
    status: AccountStatus,
    label: str | None = None,
) -> AccountDetails:
    """Create an account, optionally with a spoken label.

    Raises CustomerNotFoundError, AccountLabelTakenError or
    AccountConflictError.
    """
    if db.get(CustomerRecord, customer_id) is None:
        raise CustomerNotFoundError(customer_id)

    if label is not None:
        label = normalize_account_label(label)
        taken = db.scalar(
            select(AccountLabelRecord.account_id).where(
                AccountLabelRecord.customer_id == customer_id,
                AccountLabelRecord.label == label,
            )
        )
        if taken is not None:
            raise AccountLabelTakenError(label)

    account = AccountRecord(
        account_id=account_id,
        customer_id=customer_id,
        balance=balance,
        currency=currency,
        status=status.value,
    )
    db.add(account)
    try:
        # The account first, so the label's foreign key can refer to it.
        db.flush()
        if label is not None:
            db.add(AccountLabelRecord(account_id=account_id, customer_id=customer_id, label=label))
        db.commit()
    except IntegrityError:
        # A duplicate ID, or a label taken by a concurrent request.
        db.rollback()
        raise AccountConflictError(account_id) from None

    return _details(account, label)


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
