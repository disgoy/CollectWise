import logging

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from backend.app.db.session import get_db
from backend.app.main import app


client = TestClient(app)


def test_health_endpoint_returns_healthy_status() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "collectwise-api",
    }


def test_unknown_endpoint_returns_404() -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404


# --- readiness (H5) -------------------------------------------------------------

SECRET_IN_ERROR = "postgresql://app:hunter2@db.internal/collectwise"


class BrokenSession:
    """A database session whose every query fails, as when the database is down."""

    def execute(self, *_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception(f"could not connect to {SECRET_IN_ERROR}"))

    def close(self) -> None:
        pass


def test_ready_when_the_database_answers(client) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_not_ready_when_the_database_fails_and_nothing_leaks(client, caplog) -> None:
    app.dependency_overrides[get_db] = lambda: BrokenSession()

    with caplog.at_level(logging.ERROR, logger="backend.app.main"):
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "OperationalError" in caplog.text
    assert "hunter2" not in caplog.text
    assert "hunter2" not in response.text


def test_liveness_never_touches_the_database(client) -> None:
    app.dependency_overrides[get_db] = lambda: BrokenSession()

    assert client.get("/health").status_code == 200
