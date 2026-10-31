import os
import tempfile

# Must be set before the app is imported, so the app's own engine never
# points at the real collectwise.db file during tests. Use a temporary file,
# not in-memory "sqlite://": in-memory SQLite gets SingletonThreadPool, which
# breaks when several TestClient startups use it from different threads at
# once (as the concurrency test does). It is migrated below, like a real one.
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
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from backend.app import config  # noqa: E402
from backend.app.api.providers import get_db_factory  # noqa: E402
from backend.app.api.rate_limits import (  # noqa: E402
    SlidingWindowLimiter,
    get_session_start_limiter,
    get_turn_limiter,
)
from backend.app.db import models  # noqa: E402, F401  Registers the SQLAlchemy models
from backend.app.db.session import get_db, upgrade_to_head  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.tests.databases import POSTGRES, make_test_engine, stop_postgres  # noqa: E402
from backend.tests.helpers import ADMIN_HEADERS  # noqa: E402

# The app's own (temporary) database gets the real migrations, every run.
upgrade_to_head(config.DATABASE_URL)


def pytest_sessionfinish(session, exitstatus) -> None:
    stop_postgres()


def pytest_report_header(config) -> str:
    return "test databases: Postgres (throwaway cluster)" if POSTGRES else "test databases: SQLite"


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    # A fresh database per test (see make_test_engine).
    test_engine = make_test_engine()
    TestSession = sessionmaker(
        bind=test_engine,
        autoflush=False,
        expire_on_commit=False,
    )

    def override_get_db() -> Generator[Session, None, None]:
        with TestSession() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    # Work outside a request (a live call) opens sessions on the same database.
    app.dependency_overrides[get_db_factory] = lambda: TestSession
    # Fresh rate limits per test (shipped values), so one test's requests
    # never count against the next.
    turn_limiter = SlidingWindowLimiter(config.TURNS_PER_MINUTE)
    start_limiter = SlidingWindowLimiter(config.SESSION_STARTS_PER_MINUTE)
    app.dependency_overrides[get_turn_limiter] = lambda: turn_limiter
    app.dependency_overrides[get_session_start_limiter] = lambda: start_limiter

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
    # A separate database for tests that use the DB without HTTP.
    test_engine = make_test_engine()
    TestSession = sessionmaker(
        bind=test_engine,
        autoflush=False,
        expire_on_commit=False,
    )

    with TestSession() as session:
        yield session

    test_engine.dispose()
