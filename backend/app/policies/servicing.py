from dataclasses import dataclass
from enum import Enum

from backend.app.schemas.domain import (
    Account,
    AccountStatus,
    ServicingOperation,
    ServicingRequest,
)


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str


def authorize_request(
    request: ServicingRequest,
    account: Account,
) -> PolicyResult:
    if request.customer_id != account.customer_id:
        return PolicyResult(
            PolicyDecision.DENY,
            "customer_account_mismatch",
        )

    if request.account_id != account.account_id:
        return PolicyResult(
            PolicyDecision.DENY,
            "account_id_mismatch",
        )

    if account.status != AccountStatus.ACTIVE:
        return PolicyResult(
            PolicyDecision.DENY,
            "account_not_active",
        )

    if request.operation == ServicingOperation.VIEW_ACCOUNT:
        return PolicyResult(
            PolicyDecision.ALLOW,
            "policy_checks_passed",
        )

    if request.operation == ServicingOperation.REQUEST_PAYMENT:
        if request.amount is None:
            return PolicyResult(
                PolicyDecision.DENY,
                "payment_amount_required",
            )

        return PolicyResult(
            PolicyDecision.ALLOW,
            "policy_checks_passed",
        )

    return PolicyResult(
        PolicyDecision.DENY,
        "unsupported_operation",
    )
