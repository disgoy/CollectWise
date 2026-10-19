from decimal import Decimal

from backend.app.policies.servicing import (
    PolicyDecision,
    authorize_request,
)
from backend.app.schemas.domain import (
    Account,
    AccountStatus,
    ServicingOperation,
    ServicingRequest,
)


def make_account(
    status: AccountStatus = AccountStatus.ACTIVE,
) -> Account:
    return Account(
        account_id="acct_demo_001",
        customer_id="cust_demo_001",
        balance=Decimal("1250.50"),
        currency="INR",
        status=status,
    )


def make_request(
    operation: ServicingOperation = ServicingOperation.VIEW_ACCOUNT,
    customer_id: str = "cust_demo_001",
    account_id: str = "acct_demo_001",
    amount: Decimal | None = None,
) -> ServicingRequest:
    return ServicingRequest(
        request_id="req_demo_001",
        customer_id=customer_id,
        account_id=account_id,
        operation=operation,
        amount=amount,
    )


def test_authorizes_valid_account_view() -> None:
    result = authorize_request(make_request(), make_account())

    assert result.decision == PolicyDecision.ALLOW


def test_denies_account_owned_by_another_customer() -> None:
    request = make_request(customer_id="cust_other_001")

    result = authorize_request(request, make_account())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "customer_account_mismatch"


def test_denies_mismatched_account_id() -> None:
    request = make_request(account_id="acct_other_001")

    result = authorize_request(request, make_account())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "account_id_mismatch"


def test_denies_frozen_account() -> None:
    result = authorize_request(
        make_request(),
        make_account(AccountStatus.FROZEN),
    )

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "account_not_active"


def test_denies_payment_without_amount() -> None:
    request = make_request(
        operation=ServicingOperation.REQUEST_PAYMENT,
    )

    result = authorize_request(request, make_account())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "payment_amount_required"


def test_allows_payment_with_positive_amount() -> None:
    request = make_request(
        operation=ServicingOperation.REQUEST_PAYMENT,
        amount=Decimal("100.00"),
    )

    result = authorize_request(request, make_account())

    assert result.decision == PolicyDecision.ALLOW
