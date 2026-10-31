"""Migrations (M6.4): they build exactly the models' schema, undo cleanly,
adopt a database made before migrations without losing rows, and a server
or script on an old schema says so. Each runs on SQLite, and on Postgres
with COLLECTWISE_TEST_POSTGRES=1."""

import logging
import tempfile
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.db import models
from backend.app.db import session as db
from backend.app.db.base import Base
from backend.app.db.session import SchemaOutOfDate, alembic_config, head_revision, require_current_schema
from backend.tests.databases import POSTGRES, postgres

APP_TABLES = set(Base.metadata.tables)


@pytest.fixture
def empty_url():
    """A database with no tables, on the database kind under test."""
    if POSTGRES:
        return postgres().empty_database()
    return f"sqlite:///{tempfile.mkdtemp(prefix='cw-mig-')}/empty.db"


def engine_for(url):
    engine = create_engine(url)
    if url.startswith("sqlite"):
        from backend.tests.databases import _sqlite_foreign_keys

        _sqlite_foreign_keys(engine)
    return engine


def check_names(engine) -> dict[str, set[str]]:
    inspector = inspect(engine)
    return {table: {c["name"] for c in inspector.get_check_constraints(table)} for table in APP_TABLES}


def event_row(event_type: str) -> dict:
    return {"session_id": "sess_1", "customer_id": "cust_1", "turn": 1, "event_type": event_type,
            "created_at": datetime.now(timezone.utc)}


def insert_event(engine, event_type: str) -> None:
    with sessionmaker(bind=engine)() as session:
        session.add(models.ConversationEventRecord(**event_row(event_type)))
        session.commit()


def test_the_migrations_build_exactly_the_models(empty_url) -> None:
    command.upgrade(alembic_config(empty_url), "head")
    engine = engine_for(empty_url)

    with engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert MigrationContext.configure(connection).get_current_revision() == head_revision()
    expected = {table.name: {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
                for table in Base.metadata.sorted_tables}

    assert differences == []
    assert check_names(engine) == expected  # compare_metadata doesn't look at CHECKs
    engine.dispose()


def test_the_migrations_undo_and_redo(empty_url) -> None:
    config = alembic_config(empty_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    engine = engine_for(empty_url)
    assert APP_TABLES.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()

    command.upgrade(config, "head")

    engine = engine_for(empty_url)
    assert APP_TABLES <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_undoing_0002_keeps_other_events_and_drops_the_new_kind(empty_url) -> None:
    config = alembic_config(empty_url)
    command.upgrade(config, "head")
    engine = engine_for(empty_url)
    insert_event(engine, "turn_started")
    insert_event(engine, "payment_cancelled")
    engine.dispose()

    command.downgrade(config, "0001")  # the old CHECK can't hold a payment_cancelled row

    engine = engine_for(empty_url)
    with engine.connect() as connection:
        kept = connection.execute(text("SELECT event_type FROM conversation_events")).scalars()
        assert list(kept) == ["turn_started"]
    engine.dispose()


def test_a_database_made_before_migrations_is_adopted_and_keeps_its_rows(empty_url) -> None:
    config = alembic_config(empty_url)
    command.upgrade(config, "0001")
    engine = engine_for(empty_url)
    with engine.begin() as connection:  # as create_all left it: tables, no version
        connection.execute(text("DROP TABLE alembic_version"))
    insert_event(engine, "turn_started")
    with pytest.raises(IntegrityError):  # the gap D93 left in old databases
        insert_event(engine, "payment_cancelled")
    engine.dispose()

    command.stamp(config, "0001")
    command.upgrade(config, "head")

    engine = engine_for(empty_url)
    insert_event(engine, "payment_cancelled")
    with engine.connect() as connection:
        kept = connection.execute(text("SELECT event_type FROM conversation_events ORDER BY event_id")).scalars()
        assert list(kept) == ["turn_started", "payment_cancelled"]
    assert check_names(engine)["conversation_events"] == {
        "ck_conversation_events_type", "ck_conversation_events_latency", "ck_conversation_events_turn"}
    engine.dispose()


def test_the_database_enforces_its_checks(empty_url) -> None:
    command.upgrade(alembic_config(empty_url), "head")
    engine = engine_for(empty_url)

    with pytest.raises(IntegrityError):
        insert_event(engine, "made_up_event")
    engine.dispose()


@pytest.mark.skipif(not POSTGRES, reason="SQLite ignores text lengths: Postgres only")
def test_postgres_enforces_text_lengths(empty_url) -> None:
    command.upgrade(alembic_config(empty_url), "head")
    engine = engine_for(empty_url)

    with pytest.raises(DBAPIError):
        with sessionmaker(bind=engine)() as session:
            session.add(models.ConversationEventRecord(**event_row("tool_call"), name="x" * 51))
            session.commit()
    engine.dispose()


# --- an old schema is reported, not worked around --------------------------------------


@pytest.fixture
def old_schema(empty_url):
    command.upgrade(alembic_config(empty_url), "0001")
    engine = engine_for(empty_url)
    yield engine
    engine.dispose()


def test_scripts_refuse_an_old_or_unversioned_schema(old_schema, empty_url) -> None:
    with pytest.raises(SchemaOutOfDate, match="revision 0001; the code needs 0002. Run `alembic upgrade head`"):
        require_current_schema(old_schema)
    with old_schema.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
    with pytest.raises(SchemaOutOfDate, match="no migrations"):
        require_current_schema(old_schema)

    command.stamp(alembic_config(empty_url), "head")
    require_current_schema(old_schema)  # current: no complaint


def test_a_server_on_an_old_schema_says_so_and_is_not_ready(old_schema, client, monkeypatch, caplog) -> None:
    from backend.app import main
    from backend.app.db.session import get_db

    OldSession = sessionmaker(bind=old_schema)

    def old_db():
        with OldSession() as session:
            yield session

    main.app.dependency_overrides[get_db] = old_db
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "schema_out_of_date"}
    assert client.get("/health").status_code == 200  # alive, just not ready

    monkeypatch.setattr(main, "engine", old_schema)
    with caplog.at_level(logging.ERROR, logger="backend.app.main"):
        with TestClient(main.app):  # runs startup
            pass
    assert "Database schema is out of date (0001, need 0002)" in caplog.text
    assert "alembic upgrade head" in caplog.text


def test_the_app_database_in_tests_is_migrated() -> None:
    assert db.schema_is_current(db.engine)
