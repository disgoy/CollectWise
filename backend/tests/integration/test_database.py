
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import sessionmaker

from backend.app.db import models


def test_customer_can_be_saved_and_retrieved(db_session):
    customer = models.CustomerRecord(
        customer_id="cust_test_001",
        display_name="Synthetic Customer",
    )

    db_session.add(customer)
    db_session.commit()

    saved = db_session.get(models.CustomerRecord, "cust_test_001")

    assert saved is not None
    assert saved.display_name == "Synthetic Customer"


def test_account_can_be_saved_for_existing_customer(db_session):
    customer = models.CustomerRecord(
        customer_id="cust_test_002",
        display_name="Synthetic Customer",
    )
    db_session.add(customer)
    db_session.flush()

    account = models.AccountRecord(
        account_id="acct_test_001",
        customer_id="cust_test_002",
        balance=Decimal("1250.50"),
        currency="USD",
        status="active",
    )
    db_session.add(account)
    db_session.commit()

    saved = db_session.get(models.AccountRecord, "acct_test_001")

    assert saved is not None
    assert saved.customer_id == "cust_test_002"
    assert saved.balance == Decimal("1250.50")


def test_account_without_existing_customer_is_rejected(db_session):
    account = models.AccountRecord(
        account_id="acct_orphan_001",
        customer_id="missing_customer",
        balance=Decimal("100.00"),
        currency="USD",
        status="active",
    )
    db_session.add(account)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


def test_negative_account_balance_is_rejected(db_session):
    customer = models.CustomerRecord(
        customer_id="cust_test_003",
        display_name="Synthetic Customer",
    )
    db_session.add(customer)
    db_session.flush()

    account = models.AccountRecord(
        account_id="acct_negative_001",
        customer_id="cust_test_003",
        balance=Decimal("-1.00"),
        currency="USD",
        status="active",
    )
    db_session.add(account)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


def test_duplicate_servicing_request_id_is_rejected(db_session):
    first_request = models.ServicingRequestRecord(
        request_id="req_duplicate_001",
        customer_id="cust_test_001",
        account_id="acct_test_001",
        operation="view_account",
        amount=None,
        decision="allow",
        reason="policy_checks_passed",
    )

    db_session.add(first_request)
    db_session.commit()

    # Use a separate session to simulate another incoming request.
    DuplicateSession = sessionmaker(bind=db_session.get_bind())

    with DuplicateSession() as duplicate_session:
        duplicate_request = models.ServicingRequestRecord(
            request_id="req_duplicate_001",
            customer_id="cust_test_001",
            account_id="acct_test_001",
            operation="view_account",
            amount=None,
            decision="allow",
            reason="policy_checks_passed",
        )

        duplicate_session.add(duplicate_request)

        with pytest.raises(IntegrityError):
            duplicate_session.commit()


def test_servicing_request_defaults_to_evaluated_status(db_session):
    request = models.ServicingRequestRecord(
        request_id="req_status_default_001",
        customer_id="cust_test_001",
        account_id="acct_test_001",
        operation="view_account",
        amount=None,
        decision="allow",
        reason="policy_checks_passed",
    )

    db_session.add(request)
    db_session.commit()
    db_session.refresh(request)

    assert request.status == "evaluated"


def test_invalid_servicing_request_status_is_rejected(db_session):
    request = models.ServicingRequestRecord(
        request_id="req_status_invalid_001",
        customer_id="cust_test_001",
        account_id="acct_test_001",
        operation="view_account",
        amount=None,
        decision="allow",
        reason="policy_checks_passed",
        status="completed",
    )

    db_session.add(request)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


def test_audit_event_can_be_saved_for_servicing_request(db_session):
    request = models.ServicingRequestRecord(
        request_id="req_audit_001",
        customer_id="cust_test_001",
        account_id="acct_test_001",
        operation="view_account",
        amount=None,
        decision="allow",
        reason="policy_checks_passed",
    )
    db_session.add(request)
    db_session.flush()

    event = models.ServicingAuditEvent(
        request_id="req_audit_001",
        event_type="request_received",
        details='{"source": "api"}',
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    saved = db_session.get(models.ServicingAuditEvent, event.event_id)

    assert saved is not None
    assert saved.request_id == "req_audit_001"
    assert saved.event_type == "request_received"
    assert saved.details == '{"source": "api"}'
    assert saved.created_at is not None


def test_invalid_audit_event_type_is_rejected(db_session):
    request = models.ServicingRequestRecord(
        request_id="req_audit_invalid_001",
        customer_id="cust_test_001",
        account_id="acct_test_001",
        operation="view_account",
        amount=None,
        decision="allow",
        reason="policy_checks_passed",
    )
    db_session.add(request)
    db_session.flush()

    event = models.ServicingAuditEvent(
        request_id="req_audit_invalid_001",
        event_type="unknown_event",
    )
    db_session.add(event)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


def test_audit_event_without_servicing_request_is_rejected(db_session):
    event = models.ServicingAuditEvent(
        request_id="req_does_not_exist",
        event_type="request_received",
    )
    db_session.add(event)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()

def make_servicing_request(request_id: str, **fields) -> models.ServicingRequestRecord:
    return models.ServicingRequestRecord(
        request_id=request_id,
        customer_id="cust_test_001",
        account_id="acct_test_001",
        operation="view_account",
        amount=None,
        decision="allow",
        reason="policy_checks_passed",
        **fields,
    )


def test_timestamps_are_read_back_as_aware_utc(db_session):
    db_session.add(make_servicing_request("req_utc_001"))
    db_session.commit()
    # Force a real read from the database instead of the in-memory object.
    db_session.expire_all()

    saved = db_session.get(models.ServicingRequestRecord, "req_utc_001")

    assert saved.created_at.tzinfo is not None
    assert saved.created_at.utcoffset() == timedelta(0)


def test_non_utc_timestamp_is_stored_as_the_same_instant(db_session):
    india = timezone(timedelta(hours=5, minutes=30))
    local_time = datetime(2026, 10, 29, 19, 20, tzinfo=india)
    db_session.add(make_servicing_request("req_utc_002", created_at=local_time))
    db_session.commit()
    db_session.expire_all()

    saved = db_session.get(models.ServicingRequestRecord, "req_utc_002")

    assert saved.created_at == local_time
    assert saved.created_at == datetime(2026, 10, 29, 13, 50, tzinfo=timezone.utc)


def test_naive_timestamp_is_rejected(db_session):
    db_session.add(
        make_servicing_request("req_utc_003", created_at=datetime(2026, 10, 29, 19, 20))
    )

    with pytest.raises(StatementError):
        db_session.commit()

    db_session.rollback()


def test_rows_written_without_offset_are_read_as_utc(db_session):
    # Rows already in an existing collectwise.db were stored without an offset.
    db_session.connection().exec_driver_sql(
        "INSERT INTO servicing_requests "
        "(request_id, customer_id, account_id, operation, decision, reason, status, created_at) "
        "VALUES ('req_legacy_001', 'cust_test_001', 'acct_test_001', 'view_account', "
        "'allow', 'policy_checks_passed', 'evaluated', '2026-10-24 13:05:46.000000')"
    )
    db_session.commit()

    saved = db_session.get(models.ServicingRequestRecord, "req_legacy_001")

    assert saved.created_at == datetime(2026, 10, 24, 13, 5, 46, tzinfo=timezone.utc)
