import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.schemas.domain import AccountStatus, normalize_account_label
from backend.app.services.accounts import (
    AccountConflictError,
    AccountLabelTakenError,
    CustomerNotFoundError,
    create_account,
)
from backend.tests.helpers import seed_customer


@pytest.mark.parametrize(
    ("raw", "label"),
    [("savings", "savings"), ("  My   Savings ", "my savings"), ("Joint 2", "joint 2"), ("x" * 30, "x" * 30)],
)
def test_labels_are_normalized(raw, label) -> None:
    assert normalize_account_label(raw) == label


@pytest.mark.parametrize("raw", ["", "   ", "sav-ings", "savings!", "x" * 31, "acct_alex_main", "épargne"])
def test_invalid_labels_are_rejected(raw) -> None:
    with pytest.raises(ValueError):
        normalize_account_label(raw)


# --- service ---------------------------------------------------------------------


def make(db: Session, account_id: str, customer_id: str = "cust_a", label: str | None = None):
    return create_account(
        db, account_id=account_id, customer_id=customer_id, balance=100,
        currency="USD", status=AccountStatus.ACTIVE, label=label,
    )


@pytest.fixture
def customers(db_session: Session) -> Session:
    seed_customer(db_session, "cust_a", [])
    seed_customer(db_session, "cust_b", [])
    return db_session


def test_create_with_a_label(customers) -> None:
    details = make(customers, "acct_a_1", label=" Main ")

    assert details.label == "main"
    record = customers.get(models.AccountLabelRecord, "acct_a_1")
    assert (record.customer_id, record.label) == ("cust_a", "main")


def test_create_without_a_label(customers) -> None:
    assert make(customers, "acct_a_1").label is None
    assert customers.get(models.AccountLabelRecord, "acct_a_1") is None


def test_a_label_is_unique_per_customer(customers) -> None:
    make(customers, "acct_a_1", label="savings")

    with pytest.raises(AccountLabelTakenError):
        make(customers, "acct_a_2", label="Savings")
    assert customers.get(models.AccountRecord, "acct_a_2") is None  # nothing half-created


def test_customers_may_share_a_label(customers) -> None:
    make(customers, "acct_a_1", "cust_a", label="main")

    assert make(customers, "acct_b_1", "cust_b", label="main").label == "main"


def test_duplicate_account_id_is_a_conflict(customers) -> None:
    make(customers, "acct_a_1", label="main")

    with pytest.raises(AccountConflictError):
        make(customers, "acct_a_1", label="other")
    assert customers.get(models.AccountLabelRecord, "acct_a_1").label == "main"


def test_unknown_customer(customers) -> None:
    with pytest.raises(CustomerNotFoundError):
        make(customers, "acct_x", "cust_missing", label="main")


def test_service_validates_labels_itself(customers) -> None:
    # Callers other than the API (scripts, the eval) get the same rule.
    with pytest.raises(ValueError):
        make(customers, "acct_a_1", label="no_underscores")


@pytest.mark.parametrize(
    "rows",
    [
        [("acct_a_1", "cust_a", "Main")],  # not lower case
        [("acct_a_1", "cust_a", "main"), ("acct_a_2", "cust_a", "main")],  # duplicate name
    ],
    ids=["upper-case", "duplicate"],
)
def test_database_enforces_label_rules(customers, rows) -> None:
    # Backstop for writes that skip the service, and for concurrent requests.
    for account_id in ("acct_a_1", "acct_a_2"):
        make(customers, account_id)
    for account_id, customer_id, label in rows:
        customers.add(models.AccountLabelRecord(account_id=account_id, customer_id=customer_id, label=label))

    with pytest.raises(IntegrityError):
        customers.commit()
    customers.rollback()


# --- API ---------------------------------------------------------------------------


def post_account(client: TestClient, account_id: str, customer_id: str = "cust_api", **extra):
    return client.post(
        "/accounts",
        json={"account_id": account_id, "customer_id": customer_id, "balance": "10.00", "currency": "USD", **extra},
    )


@pytest.fixture
def api(client: TestClient) -> TestClient:
    for customer_id in ("cust_api", "cust_other"):
        assert client.post(
            "/customers", json={"customer_id": customer_id, "display_name": "Synthetic Customer"},
        ).status_code == 201
    return client


def test_api_create_and_read_back_a_label(api) -> None:
    created = post_account(api, "acct_api_1", label="  Joint  Savings ")

    assert created.status_code == 201
    assert created.json()["label"] == "joint savings"
    assert api.get("/accounts/acct_api_1").json()["label"] == "joint savings"
    assert api.get("/customers/cust_api/accounts").json()[0]["label"] == "joint savings"


def test_api_label_is_optional(api) -> None:
    created = post_account(api, "acct_api_1")

    assert created.status_code == 201
    assert created.json()["label"] is None


def test_api_duplicate_label_is_409(api) -> None:
    assert post_account(api, "acct_api_1", label="main").status_code == 201

    response = post_account(api, "acct_api_2", label="MAIN")

    assert response.status_code == 409
    assert response.json() == {"detail": "This customer already has an account with that label"}
    assert api.get("/accounts/acct_api_2").status_code == 404


def test_api_same_label_for_another_customer(api) -> None:
    assert post_account(api, "acct_api_1", label="main").status_code == 201

    assert post_account(api, "acct_other_1", "cust_other", label="main").status_code == 201


@pytest.mark.parametrize("label", ["", "has_underscore", "x" * 31])
def test_api_invalid_label_is_422(api, label) -> None:
    assert post_account(api, "acct_api_1", label=label).status_code == 422
