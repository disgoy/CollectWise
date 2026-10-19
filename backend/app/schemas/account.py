
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.domain import AccountStatus


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(min_length=1, max_length=50)
    customer_id: str = Field(min_length=1, max_length=50)
    balance: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    status: AccountStatus = AccountStatus.ACTIVE


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: str
    customer_id: str
    balance: Decimal
    currency: str
    status: AccountStatus
