
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from backend.app.policies.servicing import PolicyDecision
from backend.app.schemas.domain import ServicingOperation


class ServicingDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    decision: PolicyDecision
    reason: str


class ServicingRequestHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    customer_id: str
    account_id: str
    operation: ServicingOperation
    amount: Decimal | None
    decision: PolicyDecision
    reason: str
    status: str
    created_at: datetime
