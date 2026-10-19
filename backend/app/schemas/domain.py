from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class AccountStatus(str, Enum):
    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


class ServicingOperation(str, Enum):
    VIEW_ACCOUNT = "view_account"
    REQUEST_PAYMENT = "request_payment"


class Customer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    customer_id: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: str = Field(min_length=1, max_length=50)
    customer_id: str = Field(min_length=1, max_length=50)
    balance: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    status: AccountStatus


class ServicingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=100)
    customer_id: str = Field(min_length=1, max_length=50)
    account_id: str = Field(min_length=1, max_length=50)
    operation: ServicingOperation
    amount: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=12,
        decimal_places=2,
    )
