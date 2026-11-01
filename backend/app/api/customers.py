
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import require_admin
from backend.app.db.models import CustomerRecord
from backend.app.db.session import get_db
from backend.app.schemas.customer import CustomerCreate, CustomerResponse, PhoneCredentialUpdate
from backend.app.schemas.account import AccountResponse
from backend.app.services.accounts import AccountDetails, list_customer_account_details
from backend.app.services.phone_identity import (
    CustomerNotFoundError,
    InvalidPhoneNumberError,
    PhoneNumberInUseError,
    WeakPinError,
    set_phone_and_pin,
)

# Back-office routes: every one needs the admin key (D70).
router = APIRouter(prefix="/customers", tags=["Customers"], dependencies=[Depends(require_admin)])


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


@router.put(
    "/{customer_id}/phone",
    status_code=status.HTTP_204_NO_CONTENT,
)
def set_customer_phone(
    customer_id: str,
    payload: PhoneCredentialUpdate,
    db: Session = Depends(get_db),
) -> None:
    """Set the number a customer calls from and their phone PIN (M7.2). The PIN
    is stored only as a salted hash and is never returned."""
    try:
        set_phone_and_pin(db, customer_id, phone_number=payload.phone_number, pin=payload.pin.get_secret_value())
    except CustomerNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found") from None
    except InvalidPhoneNumberError:
        raise HTTPException(status_code=422, detail="Phone number must be E.164, e.g. +15555550100") from None
    except WeakPinError:
        raise HTTPException(status_code=422, detail="PIN must be 6 digits, not easy to guess") from None
    except PhoneNumberInUseError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phone number already in use") from None
