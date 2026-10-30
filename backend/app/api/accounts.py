from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import require_admin
from backend.app.db.session import get_db
from backend.app.schemas.account import AccountCreate, AccountResponse
from backend.app.services.accounts import (
    AccountConflictError,
    AccountDetails,
    AccountLabelTakenError,
    CustomerNotFoundError,
    create_account,
    get_account_details,
)

# Back-office routes: every one needs the admin key (D70).
router = APIRouter(prefix="/accounts", tags=["Accounts"], dependencies=[Depends(require_admin)])


@router.post(
    "",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_account_endpoint(
    payload: AccountCreate,
    db: Session = Depends(get_db),
) -> AccountDetails:
    try:
        return create_account(
            db,
            account_id=payload.account_id,
            customer_id=payload.customer_id,
            balance=payload.balance,
            currency=payload.currency,
            status=payload.status,
            label=payload.label,
        )
    except CustomerNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found",
        ) from None
    except AccountLabelTakenError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This customer already has an account with that label",
        ) from None
    except AccountConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account could not be created because of a conflicting or invalid record",
        ) from None


@router.get("/{account_id}", response_model=AccountResponse)
def get_account(
    account_id: str,
    db: Session = Depends(get_db),
) -> AccountDetails:
    account = get_account_details(db, account_id)

    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )

    return account
