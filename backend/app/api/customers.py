
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.models import CustomerRecord
from backend.app.db.session import get_db
from backend.app.schemas.customer import CustomerCreate, CustomerResponse
from backend.app.schemas.account import AccountResponse
from backend.app.services.accounts import AccountDetails, list_customer_account_details

router = APIRouter(prefix="/customers", tags=["Customers"])


@router.post(
    "",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_customer(
    payload: CustomerCreate,
    db: Session = Depends(get_db),
) -> CustomerRecord:
    customer = CustomerRecord(
        customer_id=payload.customer_id,
        display_name=payload.display_name,
    )
    db.add(customer)

    try:
        db.commit()
        db.refresh(customer)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Customer ID already exists",
        ) from None

    return customer


@router.get(
    "/{customer_id}",
    response_model=CustomerResponse,
)
def get_customer(
    customer_id: str,
    db: Session = Depends(get_db),
) -> CustomerRecord:
    customer = db.get(CustomerRecord, customer_id)

    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found",
        )

    return customer


@router.get(
    "/{customer_id}/accounts",
    response_model=list[AccountResponse],
)
def list_customer_accounts(
    customer_id: str,
    db: Session = Depends(get_db),
) -> list[AccountDetails]:
    customer = db.get(CustomerRecord, customer_id)

    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found",
        )

    return list_customer_account_details(db, customer_id)