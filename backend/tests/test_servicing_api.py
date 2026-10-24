
from collections.abc import Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.base import Base
from backend.app.db.session import get_db
from backend.app.main import app
from sqlalchemy import func, select

@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(test_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(
        bind=test_engine,
        autoflush=False,
        expire_on_commit=False,
    )

    def override_get_db() -> Generator[Session, None, None]:
        with TestSession() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        test_engine.dispose()


def seed_account(
    client: TestClient,
    *,
    customer_id: str = "cust_service_001",
    account_id: str = "acct_service_001",
    account_status: str = "active",
) -> None:
    customer_response = client.post(
        "/customers",
        json={
            "customer_id": customer_id,
            "display_name": "Synthetic Customer",
        },
    )
    assert customer_response.status_code == 201

    account_response = client.post(
        "/accounts",
        json={
            "account_id": account_id,
            "customer_id": customer_id,
            "balance": "500.00",
            "currency": "USD",
            "status": account_status,
        },
    )
    assert account_response.status_code == 201


def test_valid_account_view_is_allowed(client: TestClient) -> None:
    seed_account(client)

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_view_001",
            "customer_id": "cust_service_001",
            "account_id": "acct_service_001",
            "operation": "view_account",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "allow"
    assert response.json()["reason"] == "policy_checks_passed"


def test_request_for_another_customer_is_denied(
    client: TestClient,
) -> None:
    seed_account(client)

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_wrong_customer",
            "customer_id": "cust_someone_else",
            "account_id": "acct_service_001",
            "operation": "view_account",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "customer_account_mismatch"


def test_frozen_account_request_is_denied(
    client: TestClient,
) -> None:
    seed_account(client, account_status="frozen")

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_frozen_001",
            "customer_id": "cust_service_001",
            "account_id": "acct_service_001",
            "operation": "view_account",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "account_not_active"


def test_payment_without_amount_is_denied(
    client: TestClient,
) -> None:
    seed_account(client)

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_payment_no_amount",
            "customer_id": "cust_service_001",
            "account_id": "acct_service_001",
            "operation": "request_payment",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "payment_amount_required"


def test_valid_payment_request_is_only_evaluated(
    client: TestClient,
) -> None:
    seed_account(client)

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_payment_001",
            "customer_id": "cust_service_001",
            "account_id": "acct_service_001",
            "operation": "request_payment",
            "amount": "25.00",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "allow"

    account_response = client.get("/accounts/acct_service_001")
    assert account_response.status_code == 200
    assert account_response.json()["balance"] == "500.00"


def test_unknown_account_returns_404(client: TestClient) -> None:
    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_missing_account",
            "customer_id": "cust_service_001",
            "account_id": "acct_does_not_exist",
            "operation": "view_account",
        },
    )

    assert response.status_code == 404


def test_identical_retry_returns_stored_decision(
    client: TestClient,
) -> None:
    seed_account(client)

    payload = {
        "request_id": "req_idempotent_001",
        "customer_id": "cust_service_001",
        "account_id": "acct_service_001",
        "operation": "view_account",
    }

    first_response = client.post("/servicing/requests", json=payload)
    second_response = client.post("/servicing/requests", json=payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()


def test_reused_request_id_with_different_payload_returns_conflict(
    client: TestClient,
) -> None:
    seed_account(client)

    payload = {
        "request_id": "req_idempotent_002",
        "customer_id": "cust_service_001",
        "account_id": "acct_service_001",
        "operation": "view_account",
    }

    first_response = client.post("/servicing/requests", json=payload)

    changed_payload = {
        **payload,
        "operation": "request_payment",
        "amount": "10.00",
    }
    second_response = client.post(
        "/servicing/requests",
        json=changed_payload,
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json()["detail"] == (
        "Request ID already used with different request details"
    )



def test_denied_decision_is_stable_on_retry(
    client: TestClient,
) -> None:
    seed_account(client)

    payload = {
        "request_id": "req_idempotent_003",
        "customer_id": "cust_someone_else",
        "account_id": "acct_service_001",
        "operation": "view_account",
    }

    first_response = client.post("/servicing/requests", json=payload)
    second_response = client.post("/servicing/requests", json=payload)

    assert first_response.status_code == 200
    assert first_response.json()["decision"] == "deny"
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()

    # A denied policy decision is still a completed evaluation.
    from sqlalchemy import select
    from backend.app.db.models import ServicingRequestRecord

    # Access the same test database through the app's DB dependency.
    from backend.app.db.session import get_db

    db_generator = app.dependency_overrides[get_db]()
    db = next(db_generator)
    try:
        record = db.scalar(
            select(ServicingRequestRecord).where(
                ServicingRequestRecord.request_id == "req_idempotent_003"
            )
        )
        assert record is not None
        assert record.decision == "deny"
        assert record.status == "evaluated"
    finally:
        db_generator.close()



def test_identical_retry_creates_only_one_database_record(
    client: TestClient,
) -> None:
    seed_account(client)

    payload = {
        "request_id": "req_idempotent_single_record",
        "customer_id": "cust_service_001",
        "account_id": "acct_service_001",
        "operation": "view_account",
    }

    first_response = client.post("/servicing/requests", json=payload)
    second_response = client.post("/servicing/requests", json=payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    # Query the same test database used by the API.
    db_override = app.dependency_overrides[get_db]
    db_generator = db_override()
    db = next(db_generator)

    try:
        count = db.scalar(
            select(func.count()).select_from(
                models.ServicingRequestRecord
            ).where(
                models.ServicingRequestRecord.request_id
                == "req_idempotent_single_record"
            )
        )
        assert count == 1
    finally:
        db.close()
        db_generator.close()



def test_servicing_request_history_returns_stored_record(
    client: TestClient,
) -> None:
    seed_account(client)

    payload = {
        "request_id": "req_history_001",
        "customer_id": "cust_service_001",
        "account_id": "acct_service_001",
        "operation": "view_account",
    }

    create_response = client.post("/servicing/requests", json=payload)
    assert create_response.status_code == 200

    history_response = client.get(
        "/servicing/requests/req_history_001"
    )

    assert history_response.status_code == 200
    history = history_response.json()
    assert history["request_id"] == "req_history_001"
    assert history["customer_id"] == "cust_service_001"
    assert history["account_id"] == "acct_service_001"
    assert history["operation"] == "view_account"
    assert history["decision"] == "allow"
    assert history["reason"] == "policy_checks_passed"
    assert history["status"] == "evaluated"
    assert history["created_at"]


def test_servicing_request_history_returns_404_for_unknown_request(
    client: TestClient,
) -> None:
    response = client.get(
        "/servicing/requests/req_history_missing"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Servicing request not found"



def test_denied_request_history_preserves_original_decision(
    client: TestClient,
) -> None:
    seed_account(client)

    payload = {
        "request_id": "req_history_denied_001",
        "customer_id": "cust_someone_else",
        "account_id": "acct_service_001",
        "operation": "view_account",
    }

    create_response = client.post("/servicing/requests", json=payload)
    assert create_response.status_code == 200
    assert create_response.json()["decision"] == "deny"

    history_response = client.get(
        "/servicing/requests/req_history_denied_001"
    )

    assert history_response.status_code == 200
    history = history_response.json()
    assert history["decision"] == "deny"
    assert history["reason"] == "customer_account_mismatch"
    assert history["status"] == "evaluated"
