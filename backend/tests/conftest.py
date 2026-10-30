import os
import tempfile

# Must be set before the app is imported, so the app's own engine never
# points at the real collectwise.db file during tests. Use a temporary file,
# not in-memory "sqlite://": in-memory SQLite gets SingletonThreadPool, which
# breaks when several TestClient startups run create_tables() from different
# threads at once (as the concurrency test does).
_test_db_dir = tempfile.mkdtemp(prefix="collectwise-tests-")
os.environ["COLLECTWISE_DATABASE_URL"] = f"sqlite:///{_test_db_dir}/app.db"
# Tests use their own session-token secret, never the developer's real one.
os.environ["COLLECTWISE_SESSION_SECRET"] = "test-only-session-secret-0123456789abcdef"
# Likewise a test-only back-office key; the `client` fixture sends it.
os.environ["COLLECTWISE_ADMIN_KEY"] = "test-only-admin-key-0123456789abcdefgh"
# Tests must never reach the real model API, whatever the developer's shell has.
os.environ.pop("COLLECTWISE_AGENT_LLM", None)
# Likewise for speech: tests never call Deepgram.
os.environ.pop("COLLECTWISE_SPEECH", None)

from collections.abc import Generator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.app.db import models  # noqa: E402, F401  Registers the SQLAlchemy models
from backend.app.db.base import Base  # noqa: E402
from backend.app.db.session import get_db  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.tests.helpers import ADMIN_HEADERS  # noqa: E402


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    # One in-memory database per test, shared across connections by StaticPool.
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
        # Sends the admin key, so tests of the back-office routes can focus
        # on behaviour; tests without it are in test_admin_auth.py.
        with TestClient(app, headers=ADMIN_HEADERS) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        test_engine.dispose()


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    # A separate in-memory database for tests that use the DB without HTTP.
    test_engine = create_engine("sqlite:///:memory:")

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
