
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db import models
from backend.app.db.base import Base
from backend.app.db.session import get_db
from backend.app.main import app
from backend.tests.helpers import ADMIN_HEADERS


@pytest.fixture
def concurrency_client(
    tmp_path,
) -> Generator[TestClient, None, None]:
    database_path = tmp_path / "concurrency.db"
    test_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
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
        with TestClient(app, raise_server_exceptions=False, headers=ADMIN_HEADERS) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        test_engine.dispose()


def seed_account(client: TestClient) -> None:
    customer_response = client.post(
        "/customers",
        json={
            "customer_id": "cust_concurrency_001",
            "display_name": "Synthetic Customer",
        },
    )
    assert customer_response.status_code == 201

    account_response = client.post(
        "/accounts",
        json={
            "account_id": "acct_concurrency_001",
            "customer_id": "cust_concurrency_001",
            "balance": "500.00",
            "currency": "USD",
            "status": "active",
        },
    )
    assert account_response.status_code == 201


def test_concurrent_identical_requests_are_idempotent(
    concurrency_client: TestClient,
) -> None:
    seed_account(concurrency_client)

    payload = {
        "request_id": "req_concurrent_001",
        "customer_id": "cust_concurrency_001",
        "account_id": "acct_concurrency_001",
        "operation": "view_account",
    }

    # Release both workers together to increase the chance of overlap.
    barrier = Barrier(2)

    def submit_request() -> tuple[int, dict]:
        with TestClient(
            app,
            raise_server_exceptions=False,
            headers=ADMIN_HEADERS,
        ) as client:
            barrier.wait(timeout=5)
            response = client.post("/servicing/requests", json=payload)
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(submit_request)
            for _ in range(2)
        ]
        results = [future.result(timeout=15) for future in futures]

    # Both requests should return the same successful decision.
    assert [status for status, _ in results] == [200, 200]
    assert results[0][1] == results[1][1]

    # Inspect the final database state using a fresh session.
    db_factory = app.dependency_overrides[get_db]
    db_generator = db_factory()
    db = next(db_generator)

    try:
        request_count = db.scalar(
            select(func.count()).select_from(
                models.ServicingRequestRecord
            ).where(
                models.ServicingRequestRecord.request_id
                == "req_concurrent_001"
            )
        )
        event_count = db.scalar(
            select(func.count()).select_from(
                models.ServicingAuditEvent
            ).where(
                models.ServicingAuditEvent.request_id
                == "req_concurrent_001"
            )
        )

        assert request_count == 1
        assert event_count == 3
    finally:
        db_generator.close()
