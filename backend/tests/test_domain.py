from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.app.schemas.domain import (
    Account,
    AccountStatus,
    Customer,
    ServicingOperation,
    ServicingRequest,
)


def test_customer_accepts_valid_data() -> None:
    customer = Customer(
        customer_id="cust_demo_001",
        display_name="Alex Example",
    )

    assert customer.customer_id == "cust_demo_001"


def test_account_accepts_valid_synthetic_data() -> None:
    account = Account(
        account_id="acct_demo_001",
        customer_id="cust_demo_001",
        balance=Decimal("1250.50"),
        currency="INR",
        status=AccountStatus.ACTIVE,
    )

    assert account.balance == Decimal("1250.50")


def test_account_rejects_negative_balance() -> None:
    with pytest.raises(ValidationError):
        Account(
            account_id="acct_demo_001",
            customer_id="cust_demo_001",
            balance=Decimal("-10.00"),
            currency="INR",
            status=AccountStatus.ACTIVE,
        )


def test_request_rejects_zero_payment_amount() -> None:
    with pytest.raises(ValidationError):
        ServicingRequest(
            request_id="req_demo_001",
            customer_id="cust_demo_001",
            account_id="acct_demo_001",
            operation=ServicingOperation.REQUEST_PAYMENT,
            amount=Decimal("0.00"),
        )


def test_request_rejects_unexpected_fields() -> None:
    with pytest.raises(ValidationError):
        Customer(
            customer_id="cust_demo_001",
            display_name="Alex Example",
            is_admin=True,
        )
