
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.db import models  # Registers the SQLAlchemy models


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


def test_create_and_retrieve_customer(client: TestClient) -> None:
    create_response = client.post(
        "/customers",
        json={
            "customer_id": "cust_api_001",
            "display_name": "Synthetic Customer",
        },
    )

    assert create_response.status_code == 201
    assert create_response.json() == {
        "customer_id": "cust_api_001",
        "display_name": "Synthetic Customer",
    }

    get_response = client.get("/customers/cust_api_001")

    assert get_response.status_code == 200
    assert get_response.json()["display_name"] == "Synthetic Customer"


def test_duplicate_customer_id_returns_conflict(client: TestClient) -> None:
    payload = {
        "customer_id": "cust_api_002",
        "display_name": "Synthetic Customer",
    }

    assert client.post("/customers", json=payload).status_code == 201
    response = client.post("/customers", json=payload)

    assert response.status_code == 409


def test_missing_customer_returns_404(client: TestClient) -> None:
    response = client.get("/customers/does_not_exist")

    assert response.status_code == 404


def test_invalid_customer_payload_returns_422(client: TestClient) -> None:
    response = client.post(
        "/customers",
        json={
            "customer_id": "",
            "display_name": "Synthetic Customer",
        },
    )

    assert response.status_code == 422
