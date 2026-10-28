from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.policies.servicing import PolicyDecision
from backend.app.schemas.domain import ServicingOperation, ServicingRequest
from backend.app.services.servicing import (
    AccountNotFoundError,
    RequestIdConflictError,
    ServicingRequestNotFoundError,
    evaluate_servicing_request,
    get_servicing_request,
    preview_servicing_request,
)


def seed_account(
    db: Session,
    *,
    customer_id: str = "cust_svc_001",
    account_id: str = "acct_svc_001",
    status: str = "active",
) -> None:
    db.add(models.CustomerRecord(customer_id=customer_id, display_name="Synthetic Customer"))
    # No ORM relationship orders these inserts, so save the customer first.
    db.flush()
    db.add(
        models.AccountRecord(
            account_id=account_id,
            customer_id=customer_id,
            balance=Decimal("500.00"),
            currency="USD",
            status=status,
        )
    )
    db.commit()


def make_request(
    *,
    request_id: str = "req_svc_001",
    customer_id: str = "cust_svc_001",
    account_id: str = "acct_svc_001",
    operation: ServicingOperation = ServicingOperation.VIEW_ACCOUNT,
    amount: Decimal | None = None,
) -> ServicingRequest:
    return ServicingRequest(
        request_id=request_id,
        customer_id=customer_id,
        account_id=account_id,
        operation=operation,
        amount=amount,
    )


def audit_event_types(db: Session, request_id: str) -> list[str]:
    return list(
        db.scalars(
            select(models.ServicingAuditEvent.event_type)
            .where(models.ServicingAuditEvent.request_id == request_id)
            .order_by(models.ServicingAuditEvent.event_id)
        )
    )


def test_allowed_request_is_persisted_with_audit_trail(db_session: Session) -> None:
    seed_account(db_session)

    result = evaluate_servicing_request(db_session, make_request())

    assert result.decision == PolicyDecision.ALLOW
    assert result.reason == "policy_checks_passed"
    assert result.replayed is False

    stored = db_session.get(models.ServicingRequestRecord, "req_svc_001")
    assert stored is not None
    assert stored.status == "evaluated"
    assert audit_event_types(db_session, "req_svc_001") == [
        "request_received",
        "policy_evaluated",
        "decision_persisted",
    ]


def test_denied_request_is_persisted(db_session: Session) -> None:
    seed_account(db_session)

    result = evaluate_servicing_request(
        db_session,
        make_request(customer_id="cust_other_001"),
    )

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "customer_account_mismatch"
    assert db_session.get(models.ServicingRequestRecord, "req_svc_001") is not None


def test_unknown_account_raises_and_persists_nothing(db_session: Session) -> None:
    seed_account(db_session)

    with pytest.raises(AccountNotFoundError):
        evaluate_servicing_request(
            db_session,
            make_request(account_id="acct_missing_001"),
        )

    assert db_session.get(models.ServicingRequestRecord, "req_svc_001") is None


def test_identical_retry_is_replayed_without_new_audit_events(
    db_session: Session,
) -> None:
    seed_account(db_session)
    request = make_request()

    first = evaluate_servicing_request(db_session, request)
    second = evaluate_servicing_request(db_session, request)

    assert second.replayed is True
    assert (second.request_id, second.decision, second.reason) == (
        first.request_id,
        first.decision,
        first.reason,
    )
    assert len(audit_event_types(db_session, "req_svc_001")) == 3


def test_reused_request_id_with_different_details_raises_conflict(
    db_session: Session,
) -> None:
    seed_account(db_session)
    evaluate_servicing_request(db_session, make_request())

    with pytest.raises(RequestIdConflictError):
        evaluate_servicing_request(
            db_session,
            make_request(
                operation=ServicingOperation.REQUEST_PAYMENT,
                amount=Decimal("25.00"),
            ),
        )

    stored = db_session.get(models.ServicingRequestRecord, "req_svc_001")
    assert stored.operation == "view_account"


def test_lost_insert_race_returns_the_winning_decision(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deterministic version of the concurrency test: another worker has
    # already committed this request ID, but our initial lookup misses it.
    seed_account(db_session)
    request = make_request()
    winner = evaluate_servicing_request(db_session, request)

    # Forget loaded objects so our insert reaches the database constraint.
    db_session.expunge_all()

    real_get = db_session.get
    missed = {"done": False}

    def stale_first_lookup(entity, ident, **kwargs):
        if entity is models.ServicingRequestRecord and not missed["done"]:
            missed["done"] = True
            return None
        return real_get(entity, ident, **kwargs)

    monkeypatch.setattr(db_session, "get", stale_first_lookup)

    result = evaluate_servicing_request(db_session, request)

    assert missed["done"] is True
    assert result.replayed is True
    assert result.decision == winner.decision
    assert len(audit_event_types(db_session, "req_svc_001")) == 3


def test_audit_failure_rolls_back_request_and_events(db_session: Session) -> None:
    seed_account(db_session)
    db_session.connection().exec_driver_sql(
        """
        CREATE TRIGGER reject_policy_event
        BEFORE INSERT ON servicing_audit_events
        WHEN NEW.event_type = 'policy_evaluated'
        BEGIN
            SELECT RAISE(ABORT, 'simulated audit insert failure');
        END;
        """
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        evaluate_servicing_request(db_session, make_request())

    assert db_session.get(models.ServicingRequestRecord, "req_svc_001") is None
    assert audit_event_types(db_session, "req_svc_001") == []


def test_get_servicing_request_returns_typed_history(db_session: Session) -> None:
    seed_account(db_session)
    evaluate_servicing_request(
        db_session,
        make_request(
            operation=ServicingOperation.REQUEST_PAYMENT,
            amount=Decimal("25.00"),
        ),
    )

    history = get_servicing_request(db_session, "req_svc_001")

    assert history.operation == ServicingOperation.REQUEST_PAYMENT
    assert history.amount == Decimal("25.00")
    assert history.decision == PolicyDecision.ALLOW
    assert history.status == "evaluated"


def test_get_unknown_servicing_request_raises(db_session: Session) -> None:
    with pytest.raises(ServicingRequestNotFoundError):
        get_servicing_request(db_session, "req_missing_001")


def test_preview_runs_policy_without_persisting(db_session: Session) -> None:
    seed_account(db_session)

    preview = preview_servicing_request(
        db_session,
        make_request(
            operation=ServicingOperation.REQUEST_PAYMENT,
            amount=Decimal("600.00"),
        ),
    )

    assert preview.decision == PolicyDecision.DENY
    assert preview.reason == "insufficient_funds"
    assert preview.currency == "USD"
    assert db_session.get(models.ServicingRequestRecord, "req_svc_001") is None
    assert audit_event_types(db_session, "req_svc_001") == []


def test_preview_of_unknown_account_raises(db_session: Session) -> None:
    with pytest.raises(AccountNotFoundError):
        preview_servicing_request(
            db_session,
            make_request(account_id="acct_missing_001"),
        )
