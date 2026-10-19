
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


def create_test_customer(client: TestClient) -> None:
    response = client.post(
        "/customers",
        json={
            "customer_id": "cust_account_test",
            "display_name": "Synthetic Customer",
        },
    )
    assert response.status_code == 201


def test_create_and_retrieve_account(client: TestClient) -> None:
    create_test_customer(client)

    response = client.post(
        "/accounts",
        json={
            "account_id": "acct_api_001",
            "customer_id": "cust_account_test",
            "balance": "1250.50",
            "currency": "USD",
            "status": "active",
        },
    )

    assert response.status_code == 201
    assert response.json()["balance"] == "1250.50"

    retrieved = client.get("/accounts/acct_api_001")

    assert retrieved.status_code == 200
    assert retrieved.json()["customer_id"] == "cust_account_test"


def test_duplicate_account_id_returns_conflict(
    client: TestClient,
) -> None:
    create_test_customer(client)

    payload = {
        "account_id": "acct_duplicate",
        "customer_id": "cust_account_test",
        "balance": "100.00",
        "currency": "USD",
        "status": "active",
    }

    assert client.post("/accounts", json=payload).status_code == 201
    assert client.post("/accounts", json=payload).status_code == 409


def test_missing_customer_returns_404(client: TestClient) -> None:
    response = client.post(
        "/accounts",
        json={
            "account_id": "acct_orphan_api",
            "customer_id": "missing_customer",
            "balance": "100.00",
            "currency": "USD",
            "status": "active",
        },
    )

    assert response.status_code == 404


def test_negative_balance_returns_422(client: TestClient) -> None:
    create_test_customer(client)

    response = client.post(
        "/accounts",
        json={
            "account_id": "acct_negative_api",
            "customer_id": "cust_account_test",
            "balance": "-10.00",
            "currency": "USD",
            "status": "active",
        },
    )

    assert response.status_code == 422


def test_missing_account_returns_404(client: TestClient) -> None:
    response = client.get("/accounts/does_not_exist")

    assert response.status_code == 404


def test_customer_accounts_endpoint(client: TestClient) -> None:
    create_test_customer(client)

    response = client.post(
        "/accounts",
        json={
            "account_id": "acct_list_001",
            "customer_id": "cust_account_test",
            "balance": "200.00",
            "currency": "USD",
            "status": "active",
        },
    )
    assert response.status_code == 201

    listed = client.get("/customers/cust_account_test/accounts")

    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["account_id"] == "acct_list_001"
