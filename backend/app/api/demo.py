"""Demo sign-in routes (D84). They answer 404, as if they didn't exist, unless
COLLECTWISE_DEMO_LOGIN=1; then only for the listed demo customers."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.auth.dependencies import SessionSecretNotConfigured
from backend.app.db.session import get_db
from backend.app.services.demo import NotADemoCustomer, demo_customers, sign_in_demo_customer

_NOT_FOUND = HTTPException(status_code=404, detail="Not Found")


def _require_demo_login() -> None:
    # Read per request, so the switch can't be left on by an import-time value.
    if not config.DEMO_LOGIN:
        raise _NOT_FOUND


router = APIRouter(prefix="/demo", tags=["Demo"], dependencies=[Depends(_require_demo_login)])


class DemoCustomerResponse(BaseModel):
    customer_id: str
    display_name: str


class DemoSignInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: Annotated[str, StringConstraints(min_length=1, max_length=50)]


class DemoSignInResponse(BaseModel):
    token: str
    expires_in: int


@router.get("/customers", response_model=list[DemoCustomerResponse])
def list_demo_customers(db: Session = Depends(get_db)) -> list[DemoCustomerResponse]:
    return [DemoCustomerResponse(customer_id=c.customer_id, display_name=c.display_name) for c in demo_customers(db)]


@router.post("/sign-in", response_model=DemoSignInResponse)
def demo_sign_in(body: DemoSignInRequest, db: Session = Depends(get_db)) -> DemoSignInResponse:
    try:
        token = sign_in_demo_customer(db, body.customer_id)
    except NotADemoCustomer:
        raise _NOT_FOUND from None
    except SessionSecretNotConfigured:
        raise HTTPException(status_code=503, detail="Agent is not configured") from None
    return DemoSignInResponse(token=token, expires_in=config.SESSION_TOKEN_TTL_SECONDS)
