from decimal import Decimal

from backend.app.policies.servicing import (
    MAX_PAYMENT_REQUEST_AMOUNT,
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
    balance: Decimal = Decimal("1250.50"),
) -> Account:
    return Account(
        account_id="acct_demo_001",
        customer_id="cust_demo_001",
        balance=balance,
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


def test_allows_owner_to_view_frozen_account() -> None:
    result = authorize_request(
        make_request(),
        make_account(AccountStatus.FROZEN),
    )

    assert result.decision == PolicyDecision.ALLOW


def test_denies_payment_from_frozen_account() -> None:
    result = authorize_request(
        make_request(
            operation=ServicingOperation.REQUEST_PAYMENT,
            amount=Decimal("10.00"),
        ),
        make_account(AccountStatus.FROZEN),
    )

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "account_not_active"


def test_denies_viewing_closed_account() -> None:
    result = authorize_request(
        make_request(),
        make_account(AccountStatus.CLOSED),
    )

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "account_not_active"


def test_denies_payment_from_closed_account() -> None:
    result = authorize_request(
        make_request(
            operation=ServicingOperation.REQUEST_PAYMENT,
            amount=Decimal("10.00"),
        ),
        make_account(AccountStatus.CLOSED),
    )

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "account_not_active"


def test_non_owner_cannot_view_frozen_account() -> None:
    result = authorize_request(
        make_request(customer_id="cust_other_001"),
        make_account(AccountStatus.FROZEN),
    )

    assert result.reason == "customer_account_mismatch"


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


def make_payment(amount: Decimal, **kwargs) -> ServicingRequest:
    return make_request(
        operation=ServicingOperation.REQUEST_PAYMENT,
        amount=amount,
        **kwargs,
    )


def test_denies_account_view_with_amount() -> None:
    request = make_request(amount=Decimal("10.00"))

    result = authorize_request(request, make_account())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "amount_not_allowed"


def test_allows_payment_equal_to_balance() -> None:
    result = authorize_request(make_payment(Decimal("1250.50")), make_account())

    assert result.decision == PolicyDecision.ALLOW


def test_denies_payment_one_cent_over_balance() -> None:
    result = authorize_request(make_payment(Decimal("1250.51")), make_account())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "insufficient_funds"


def test_allows_payment_equal_to_limit() -> None:
    account = make_account(balance=Decimal("50000.00"))

    result = authorize_request(make_payment(MAX_PAYMENT_REQUEST_AMOUNT), account)

    assert result.decision == PolicyDecision.ALLOW


def test_denies_payment_one_cent_over_limit() -> None:
    # The balance covers the amount, so only the limit can deny it.
    account = make_account(balance=Decimal("50000.00"))

    result = authorize_request(
        make_payment(MAX_PAYMENT_REQUEST_AMOUNT + Decimal("0.01")),
        account,
    )

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "payment_exceeds_limit"


def test_limit_is_checked_before_funds() -> None:
    # Over both the limit and the balance: the fixed limit is reported.
    result = authorize_request(make_payment(Decimal("20000.00")), make_account())

    assert result.reason == "payment_exceeds_limit"


def test_non_owner_never_learns_about_funds() -> None:
    # Ownership is checked first, so the reason reveals nothing about balance.
    request = make_payment(Decimal("99999.00"), customer_id="cust_other_001")

    result = authorize_request(request, make_account())

    assert result.reason == "customer_account_mismatch"


def test_inactive_account_is_reported_before_funds() -> None:
    result = authorize_request(
        make_payment(Decimal("99999.00")),
        make_account(AccountStatus.FROZEN),
    )

    assert result.reason == "account_not_active"
