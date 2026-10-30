from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import require_admin
from backend.app.db.session import get_db
from backend.app.schemas.domain import ServicingRequest
from backend.app.schemas.servicing import (
    ServicingDecisionResponse,
    ServicingRequestHistoryResponse,
)
from backend.app.services import servicing as servicing_service
from backend.app.services.servicing import (
    AccountNotFoundError,
    RequestIdConflictError,
    ServicingRequestNotFoundError,
)

# Back-office routes: every one needs the admin key (D70).
router = APIRouter(prefix="/servicing", tags=["Servicing"], dependencies=[Depends(require_admin)])


@router.post(
    "/requests",
    response_model=ServicingDecisionResponse,
)
def evaluate_servicing_request(
    request: ServicingRequest,
    db: Session = Depends(get_db),
) -> ServicingDecisionResponse:
    try:
        result = servicing_service.evaluate_servicing_request(db, request)
    except AccountNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Account not found",
        ) from None
    except RequestIdConflictError:
        raise HTTPException(
            status_code=409,
            detail="Request ID already used with different request details",
        ) from None

    return ServicingDecisionResponse(
        request_id=result.request_id,
        decision=result.decision,
        reason=result.reason,
    )


@router.get(
    "/requests/{request_id}",
    response_model=ServicingRequestHistoryResponse,
)
def get_servicing_request_history(
    request_id: str,
    db: Session = Depends(get_db),
) -> ServicingRequestHistoryResponse:
    try:
        history = servicing_service.get_servicing_request(db, request_id)
    except ServicingRequestNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Servicing request not found",
        ) from None

    return ServicingRequestHistoryResponse(
        request_id=history.request_id,
        customer_id=history.customer_id,
        account_id=history.account_id,
        operation=history.operation,
        amount=history.amount,
        decision=history.decision,
        reason=history.reason,
        status=history.status,
        created_at=history.created_at,
    )
