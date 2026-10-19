
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.db.models import AccountRecord
from backend.app.db.session import get_db
from backend.app.policies.servicing import authorize_request
from backend.app.schemas.domain import Account, AccountStatus, ServicingRequest
from backend.app.schemas.servicing import ServicingDecisionResponse

router = APIRouter(prefix="/servicing", tags=["Servicing"])


@router.post(
    "/requests",
    response_model=ServicingDecisionResponse,
)
def evaluate_servicing_request(
    request: ServicingRequest,
    db: Session = Depends(get_db),
) -> ServicingDecisionResponse:
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

    result = authorize_request(request, account)

    return ServicingDecisionResponse(
        request_id=request.request_id,
        decision=result.decision,
        reason=result.reason,
    )
