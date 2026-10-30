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
