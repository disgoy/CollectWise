from fastapi.testclient import TestClient


def create_test_customer(client: TestClient) -> None:
    response = client.post(
        "/customers",
        json={
            "customer_id": "cust_account_test",
            "display_name": "Synthetic Customer",
        },
    )
    assert response.status_code == 201


def test_create_and_retrieve_account(client: TestClient) -> None:
    create_test_customer(client)

    response = client.post(
        "/accounts",
        json={
            "account_id": "acct_api_001",
            "customer_id": "cust_account_test",
            "balance": "1250.50",
            "currency": "USD",
            "status": "active",
        },
    )

    assert response.status_code == 201
    assert response.json()["balance"] == "1250.50"

    retrieved = client.get("/accounts/acct_api_001")

    assert retrieved.status_code == 200
    assert retrieved.json()["customer_id"] == "cust_account_test"


def test_duplicate_account_id_returns_conflict(
    client: TestClient,
) -> None:
    create_test_customer(client)

    payload = {
        "account_id": "acct_duplicate",
        "customer_id": "cust_account_test",
        "balance": "100.00",
        "currency": "USD",
        "status": "active",
    }

    assert client.post("/accounts", json=payload).status_code == 201
    assert client.post("/accounts", json=payload).status_code == 409


def test_missing_customer_returns_404(client: TestClient) -> None:
    response = client.post(
        "/accounts",
        json={
            "account_id": "acct_orphan_api",
            "customer_id": "missing_customer",
            "balance": "100.00",
            "currency": "USD",
            "status": "active",
        },
    )

    assert response.status_code == 404


def test_negative_balance_returns_422(client: TestClient) -> None:
    create_test_customer(client)

    response = client.post(
        "/accounts",
        json={
            "account_id": "acct_negative_api",
            "customer_id": "cust_account_test",
            "balance": "-10.00",
            "currency": "USD",
            "status": "active",
        },
    )

    assert response.status_code == 422


def test_missing_account_returns_404(client: TestClient) -> None:
    response = client.get("/accounts/does_not_exist")

    assert response.status_code == 404


def test_customer_accounts_endpoint(client: TestClient) -> None:
    create_test_customer(client)

    response = client.post(
        "/accounts",
        json={
            "account_id": "acct_list_001",
            "customer_id": "cust_account_test",
            "balance": "200.00",
            "currency": "USD",
            "status": "active",
        },
    )
    assert response.status_code == 201

    listed = client.get("/customers/cust_account_test/accounts")

    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["account_id"] == "acct_list_001"
