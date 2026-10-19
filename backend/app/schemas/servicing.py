
from pydantic import BaseModel, ConfigDict, Field

from backend.app.policies.servicing import PolicyDecision


class ServicingDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    decision: PolicyDecision
    reason: str
