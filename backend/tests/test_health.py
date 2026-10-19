from fastapi.testclient import TestClient

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
