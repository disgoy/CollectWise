
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.db.base import Base
from backend.app.db import models


@pytest.fixture
def db_session():
    # Use a separate in-memory database for each test.
    test_engine = create_engine("sqlite:///:memory:")

    # Enable SQLite foreign-key enforcement in this test database.
    from sqlalchemy import event

    @event.listens_for(test_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=test_engine)

    TestSession = sessionmaker(
        bind=test_engine,
        autoflush=False,
        expire_on_commit=False,
    )

    with TestSession() as session:
        yield session

    test_engine.dispose()


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
