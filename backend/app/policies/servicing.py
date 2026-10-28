from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from backend.app.schemas.domain import (
    Account,
    AccountStatus,
    ServicingOperation,
    ServicingRequest,
)


# Ceiling for a single payment request, independent of the account balance.
# It guards against misheard or misparsed amounts. Applies to every currency.
MAX_PAYMENT_REQUEST_AMOUNT = Decimal("10000.00")


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

    # Closed accounts allow nothing. Frozen accounts stay viewable by their
    # owner but cannot move money (checked in the payment branch).
    if account.status == AccountStatus.CLOSED:
        return PolicyResult(
            PolicyDecision.DENY,
            "account_not_active",
        )

    if request.operation == ServicingOperation.VIEW_ACCOUNT:
        if request.amount is not None:
            return PolicyResult(
                PolicyDecision.DENY,
                "amount_not_allowed",
            )

        return PolicyResult(
            PolicyDecision.ALLOW,
            "policy_checks_passed",
        )

    if request.operation == ServicingOperation.REQUEST_PAYMENT:
        if account.status != AccountStatus.ACTIVE:
            return PolicyResult(
                PolicyDecision.DENY,
                "account_not_active",
            )

        if request.amount is None:
            return PolicyResult(
                PolicyDecision.DENY,
                "payment_amount_required",
            )

        if request.amount > MAX_PAYMENT_REQUEST_AMOUNT:
            return PolicyResult(
                PolicyDecision.DENY,
                "payment_exceeds_limit",
            )

        # Balance is the funds available in the account.
        if request.amount > account.balance:
            return PolicyResult(
                PolicyDecision.DENY,
                "insufficient_funds",
            )

        return PolicyResult(
            PolicyDecision.ALLOW,
            "policy_checks_passed",
        )

    return PolicyResult(
        PolicyDecision.DENY,
        "unsupported_operation",
    )
