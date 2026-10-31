from decimal import Decimal

from sqlalchemy.orm import Session

from backend.app import config
from backend.app.auth.dependencies import ADMIN_KEY_HEADER
from backend.app.db import models
from backend.app.tools.base import CallerContext, ToolContext

# The back-office key from conftest's environment (D70).
ADMIN_HEADERS = {ADMIN_KEY_HEADER: config.ADMIN_API_KEY}


def seed_customer(
    db: Session,
    customer_id: str,
    accounts: list[tuple[str, str, str]],
    *,
    balance: Decimal = Decimal("500.00"),
) -> None:
    """Seed a customer with (account_id, currency, status) accounts."""
    db.add(models.CustomerRecord(customer_id=customer_id, display_name="Synthetic Customer"))
    # No ORM relationship orders these inserts, so save the customer first.
    db.flush()

    for account_id, currency, status in accounts:
        db.add(
            models.AccountRecord(
                account_id=account_id,
                customer_id=customer_id,
                balance=balance,
                currency=currency,
                status=status,
            )
        )
    db.commit()


def context_for(db: Session, customer_id: str) -> ToolContext:
    return ToolContext(
        db=db,
        caller=CallerContext(customer_id=customer_id, session_id="sess_test_001"),
    )


def reject_policy_audit_events(connection) -> None:
    """Make every 'policy_evaluated' audit insert fail, as a broken database
    would, on SQLite or Postgres. Either way the driver reports an integrity
    error."""
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("""
            CREATE FUNCTION reject_policy_event() RETURNS trigger AS $$
            BEGIN
                IF NEW.event_type = 'policy_evaluated' THEN
                    RAISE EXCEPTION 'simulated audit insert failure' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END $$ LANGUAGE plpgsql;
            CREATE TRIGGER reject_policy_event BEFORE INSERT ON servicing_audit_events
                FOR EACH ROW EXECUTE FUNCTION reject_policy_event();
        """)
    else:
        connection.exec_driver_sql("""
            CREATE TRIGGER reject_policy_event
            BEFORE INSERT ON servicing_audit_events
            WHEN NEW.event_type = 'policy_evaluated'
            BEGIN
                SELECT RAISE(ABORT, 'simulated audit insert failure');
            END;
        """)
