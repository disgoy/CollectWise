import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.db import models
from backend.app.db.session import get_db
from backend.app.main import app


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


def test_payment_over_available_funds_is_denied_and_stored(
    client: TestClient,
) -> None:
    seed_account(client)

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_payment_insufficient",
            "customer_id": "cust_service_001",
            "account_id": "acct_service_001",
            "operation": "request_payment",
            "amount": "500.01",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "insufficient_funds"

    history = client.get("/servicing/requests/req_payment_insufficient")
    assert history.json()["decision"] == "deny"
    assert history.json()["reason"] == "insufficient_funds"


def test_payment_over_limit_is_denied(client: TestClient) -> None:
    seed_account(client)

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_payment_over_limit",
            "customer_id": "cust_service_001",
            "account_id": "acct_service_001",
            "operation": "request_payment",
            "amount": "10000.01",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "payment_exceeds_limit"


def test_account_view_with_amount_is_denied(client: TestClient) -> None:
    seed_account(client)

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_view_with_amount",
            "customer_id": "cust_service_001",
            "account_id": "acct_service_001",
            "operation": "view_account",
            "amount": "10.00",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "amount_not_allowed"


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


def test_successful_request_creates_audit_events(
    client: TestClient,
) -> None:
    seed_account(client)

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_audit_api_001",
            "customer_id": "cust_service_001",
            "account_id": "acct_service_001",
            "operation": "view_account",
        },
    )

    assert response.status_code == 200

    # Read audit events from the same test database.
    db_generator = app.dependency_overrides[get_db]()
    db = next(db_generator)

    try:
        events = db.scalars(
            select(models.ServicingAuditEvent)
            .where(
                models.ServicingAuditEvent.request_id
                == "req_audit_api_001"
            )
            .order_by(models.ServicingAuditEvent.event_id)
        ).all()

        assert [event.event_type for event in events] == [
            "request_received",
            "policy_evaluated",
            "decision_persisted",
        ]
        audit_details = json.loads(events[1].details)

        assert audit_details == {
            "decision": "allow",
            "reason": "policy_checks_passed",
        }

    finally:
        db_generator.close()

def test_identical_retry_does_not_duplicate_audit_events(
    client: TestClient,
) -> None:
    seed_account(client)

    payload = {
        "request_id": "req_audit_retry_001",
        "customer_id": "cust_service_001",
        "account_id": "acct_service_001",
        "operation": "view_account",
    }

    first_response = client.post("/servicing/requests", json=payload)
    second_response = client.post("/servicing/requests", json=payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()

    db_generator = app.dependency_overrides[get_db]()
    db = next(db_generator)

    try:
        event_count = db.scalar(
            select(func.count())
            .select_from(models.ServicingAuditEvent)
            .where(
                models.ServicingAuditEvent.request_id
                == "req_audit_retry_001"
            )
        )

        assert event_count == 3
    finally:
        db_generator.close()

def test_denied_request_creates_audit_events(
    client: TestClient,
) -> None:
    seed_account(client)

    response = client.post(
        "/servicing/requests",
        json={
            "request_id": "req_audit_denied_001",
            "customer_id": "cust_someone_else",
            "account_id": "acct_service_001",
            "operation": "view_account",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "customer_account_mismatch"

    db_generator = app.dependency_overrides[get_db]()
    db = next(db_generator)

    try:
        events = db.scalars(
            select(models.ServicingAuditEvent)
            .where(
                models.ServicingAuditEvent.request_id
                == "req_audit_denied_001"
            )
            .order_by(models.ServicingAuditEvent.event_id)
        ).all()

        assert [event.event_type for event in events] == [
            "request_received",
            "policy_evaluated",
            "decision_persisted",
        ]

        assert '"decision": "deny"' in events[1].details
        assert '"reason": "customer_account_mismatch"' in events[1].details
    finally:
        db_generator.close()

def test_audit_event_failure_rolls_back_servicing_request(
    client: TestClient,
) -> None:
    seed_account(client)

    # Instead of modifying the shared model or schema, we'll use a
    # database trigger to reject the policy_evaluated event.
    db_generator = app.dependency_overrides[get_db]()
    db = next(db_generator)

    try:
        db.connection().exec_driver_sql(
            """
            CREATE TRIGGER reject_policy_event
            BEFORE INSERT ON servicing_audit_events
            WHEN NEW.event_type = 'policy_evaluated'
            BEGIN
                SELECT RAISE(ABORT, 'simulated audit insert failure');
            END;
            """
        )
        db.commit()
    finally:
        db_generator.close()

    # This test expects a server error, so return it as a 500 response
    # instead of re-raising it. The DB override from the fixture still applies.
    lenient_client = TestClient(app, raise_server_exceptions=False)

    response = lenient_client.post(
        "/servicing/requests",
        json={
            "request_id": "req_audit_rollback_001",
            "customer_id": "cust_service_001",
            "account_id": "acct_service_001",
            "operation": "view_account",
        },
    )

    # The API should not report success when the audit transaction fails.
    assert response.status_code >= 500

    db_generator = app.dependency_overrides[get_db]()
    db = next(db_generator)

    try:
        assert db.get(
            models.ServicingRequestRecord,
            "req_audit_rollback_001",
        ) is None

        events = db.scalars(
            select(models.ServicingAuditEvent).where(
                models.ServicingAuditEvent.request_id
                == "req_audit_rollback_001"
            )
        ).all()

        assert events == []
    finally:
        db_generator.close()