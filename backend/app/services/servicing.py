import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.models import (
    AccountRecord,
    ServicingAuditEvent,
    ServicingRequestRecord,
)
from backend.app.policies.servicing import PolicyDecision, authorize_request
from backend.app.schemas.domain import (
    Account,
    AccountStatus,
    ServicingOperation,
    ServicingRequest,
)


class ServicingError(Exception):
    """Base class for servicing workflow errors. Callers map these to their own
    interface (HTTP status codes, agent tool results, ...)."""


class AccountNotFoundError(ServicingError):
    """The request refers to an account that does not exist."""


class RequestIdConflictError(ServicingError):
    """The request ID was already used with different request details."""


class ServicingRequestNotFoundError(ServicingError):
    """No servicing request exists with the given ID."""


@dataclass(frozen=True)
class ServicingDecision:
    request_id: str
    decision: PolicyDecision
    reason: str
    # True when this call was a retry and returned the stored decision.
    replayed: bool


@dataclass(frozen=True)
class ServicingRequestHistory:
    request_id: str
    customer_id: str
    account_id: str
    operation: ServicingOperation
    amount: Decimal | None
    decision: PolicyDecision
    reason: str
    status: str
    created_at: datetime


def _request_matches(
    stored: ServicingRequestRecord,
    request: ServicingRequest,
) -> bool:
    """Check whether a request ID is being reused with identical details."""
    return (
        stored.customer_id == request.customer_id
        and stored.account_id == request.account_id
        and stored.operation == request.operation.value
        and stored.amount == request.amount
    )


def _stored_decision(
    stored: ServicingRequestRecord,
    *,
    replayed: bool,
) -> ServicingDecision:
    """Build the result from the persisted decision."""
    return ServicingDecision(
        request_id=stored.request_id,
        decision=PolicyDecision(stored.decision),
        reason=stored.reason,
        replayed=replayed,
    )


def _replay_stored_request(
    stored: ServicingRequestRecord,
    request: ServicingRequest,
) -> ServicingDecision:
    """Return the original decision for a retry, or reject a reused ID."""
    if not _request_matches(stored, request):
        raise RequestIdConflictError(request.request_id)

    return _stored_decision(stored, replayed=True)


def _add_audit_event(
    db: Session,
    request_id: str,
    event_type: str,
    details: str | None = None,
) -> None:
    """Add an audit event to the current database transaction."""
    db.add(
        ServicingAuditEvent(
            request_id=request_id,
            event_type=event_type,
            details=details,
        )
    )


def evaluate_servicing_request(
    db: Session,
    request: ServicingRequest,
) -> ServicingDecision:
    """Evaluate a servicing request idempotently and persist the decision.

    Raises AccountNotFoundError or RequestIdConflictError.
    """
    # 1. Check whether this request ID has already been evaluated.
    stored = db.get(ServicingRequestRecord, request.request_id)

    if stored is not None:
        return _replay_stored_request(stored, request)

    # 2. Load the account.
    account_record = db.get(AccountRecord, request.account_id)

    if account_record is None:
        raise AccountNotFoundError(request.account_id)

    account = Account(
        account_id=account_record.account_id,
        customer_id=account_record.customer_id,
        balance=account_record.balance,
        currency=account_record.currency,
        status=AccountStatus(account_record.status),
    )

    # 3. Evaluate the request using deterministic policy rules.
    result = authorize_request(request, account)

    # 4. Persist the request and its audit trail atomically.
    record = ServicingRequestRecord(
        request_id=request.request_id,
        customer_id=request.customer_id,
        account_id=request.account_id,
        operation=request.operation.value,
        amount=request.amount,
        decision=result.decision.value,
        reason=result.reason,
        status="evaluated",
    )
    db.add(record)

    try:
        # Flush the request first so audit events can reference it.
        db.flush()

        _add_audit_event(
            db,
            request.request_id,
            "request_received",
        )
        _add_audit_event(
            db,
            request.request_id,
            "policy_evaluated",
            details=json.dumps(
                {
                    "decision": result.decision.value,
                    "reason": result.reason,
                }
            ),
        )
        _add_audit_event(
            db,
            request.request_id,
            "decision_persisted",
        )

        # One commit saves the request and all three events.
        db.commit()

    except IntegrityError:
        # Roll back the entire transaction if any insert fails.
        db.rollback()

        # A concurrent request may have saved this request ID.
        stored = db.get(ServicingRequestRecord, request.request_id)

        if stored is None:
            raise

        return _replay_stored_request(stored, request)

    db.refresh(record)
    return _stored_decision(record, replayed=False)


def get_servicing_request(
    db: Session,
    request_id: str,
) -> ServicingRequestHistory:
    """Return a stored servicing request.

    Raises ServicingRequestNotFoundError.
    """
    record = db.get(ServicingRequestRecord, request_id)

    if record is None:
        raise ServicingRequestNotFoundError(request_id)

    return ServicingRequestHistory(
        request_id=record.request_id,
        customer_id=record.customer_id,
        account_id=record.account_id,
        operation=ServicingOperation(record.operation),
        amount=record.amount,
        decision=PolicyDecision(record.decision),
        reason=record.reason,
        status=record.status,
        created_at=record.created_at,
    )
