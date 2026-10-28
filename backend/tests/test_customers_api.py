from fastapi.testclient import TestClient


def test_create_and_retrieve_customer(client: TestClient) -> None:
    create_response = client.post(
        "/customers",
        json={
            "customer_id": "cust_api_001",
            "display_name": "Synthetic Customer",
        },
    )

    assert create_response.status_code == 201
    assert create_response.json() == {
        "customer_id": "cust_api_001",
        "display_name": "Synthetic Customer",
    }

    get_response = client.get("/customers/cust_api_001")

    assert get_response.status_code == 200
    assert get_response.json()["display_name"] == "Synthetic Customer"


def test_duplicate_customer_id_returns_conflict(client: TestClient) -> None:
    payload = {
        "customer_id": "cust_api_002",
        "display_name": "Synthetic Customer",
    }

    assert client.post("/customers", json=payload).status_code == 201
    response = client.post("/customers", json=payload)

    assert response.status_code == 409


def test_missing_customer_returns_404(client: TestClient) -> None:
    response = client.get("/customers/does_not_exist")

    assert response.status_code == 404


def test_invalid_customer_payload_returns_422(client: TestClient) -> None:
    response = client.post(
        "/customers",
        json={
            "customer_id": "",
            "display_name": "Synthetic Customer",
        },
    )

    assert response.status_code == 422
