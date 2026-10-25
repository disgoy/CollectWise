import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.models import (
    AccountRecord,
    ServicingAuditEvent,
    ServicingRequestRecord,
)
from backend.app.db.session import get_db
from backend.app.policies.servicing import authorize_request
from backend.app.schemas.domain import Account, AccountStatus, ServicingRequest
from backend.app.schemas.servicing import (
    ServicingDecisionResponse,
    ServicingRequestHistoryResponse,
)

router = APIRouter(prefix="/servicing", tags=["Servicing"])


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


def _stored_response(
    stored: ServicingRequestRecord,
) -> ServicingDecisionResponse:
    """Build the response from the original persisted decision."""
    return ServicingDecisionResponse(
        request_id=stored.request_id,
        decision=stored.decision,
        reason=stored.reason,
    )

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

@router.post(
    "/requests",
    response_model=ServicingDecisionResponse,
)
def evaluate_servicing_request(
    request: ServicingRequest,
    db: Session = Depends(get_db),
) -> ServicingDecisionResponse:
    # 1. Check whether this request ID has already been evaluated.
    stored = db.get(ServicingRequestRecord, request.request_id)

    if stored is not None:
        if not _request_matches(stored, request):
            raise HTTPException(
                status_code=409,
                detail="Request ID already used with different request details",
            )
        return _stored_response(stored)

    # 2. Load the account.
    account_record = db.get(AccountRecord, request.account_id)

    if account_record is None:
        raise HTTPException(
            status_code=404,
            detail="Account not found",
        )

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

        if not _request_matches(stored, request):
            raise HTTPException(
                status_code=409,
                detail="Request ID already used with different request details",
            )

        return _stored_response(stored)

    db.refresh(record)
    return _stored_response(record)


@router.get(
    "/requests/{request_id}",
    response_model=ServicingRequestHistoryResponse,
)
def get_servicing_request_history(
    request_id: str,
    db: Session = Depends(get_db),
) -> ServicingRequestHistoryResponse:
    record = db.get(ServicingRequestRecord, request_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Servicing request not found",
        )

    return ServicingRequestHistoryResponse(
        request_id=record.request_id,
        customer_id=record.customer_id,
        account_id=record.account_id,
        operation=record.operation,
        amount=record.amount,
        decision=record.decision,
        reason=record.reason,
        status=record.status,
        created_at=record.created_at,
    )
