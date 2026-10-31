"""The back-office routes (/customers, /accounts, /servicing) need the admin
key, and every route in the app is either back-office, agent or /health (D70).

The route inventory comes from the published API schema, a public interface:
FastAPI 0.148 keeps included routers in private structures. A route hidden
from the schema (include_in_schema=False) would escape it, so a separate test
checks that no route on the app or on our routers is hidden. A hidden route
on a router this file doesn't list would still escape.
"""

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app import config
from backend.app.auth.dependencies import ADMIN_KEY_HEADER
from backend.app.auth.session_tokens import issue_session_token
from backend.app.db import models
from backend.app.db.session import get_db
from backend.app.api import accounts, agent, customers, demo, live, live_page, servicing, voice
from backend.app.main import app

BACK_OFFICE_PREFIXES = ("/customers", "/accounts", "/servicing")
ROUTER_MODULES = (accounts, agent, customers, demo, live, live_page, servicing, voice)
# WebSocket routes aren't in the OpenAPI schema, so they are listed here; each
# signs in by its first message and has its own tests (test_live_api.py, D83).
WEBSOCKET_ROUTES = {"/agent/sessions/{session_id}/live"}
# The only routes anyone may call: liveness and readiness (H5), and demo
# sign-in, which answers 404 unless switched on (D84; tests in test_demo.py).
OPEN_ROUTES = {
    ("GET", "/health"), ("GET", "/health/ready"),
    ("GET", "/demo/customers"), ("POST", "/demo/sign-in"),
}
# Routes deliberately left out of the schema: the live-call page (D84).
HIDDEN_ROUTES = {"/live"}

# Every back-office route, with a body that would succeed with the key.
ROUTES = [
    ("POST", "/customers", {"customer_id": "cust_new", "display_name": "Synthetic Customer"}),
    ("GET", "/customers/cust_victim", None),
    ("GET", "/customers/cust_victim/accounts", None),
    ("POST", "/accounts", {
        "account_id": "acct_new", "customer_id": "cust_victim",
        "balance": "1.00", "currency": "USD", "status": "active",
    }),
    ("GET", "/accounts/acct_victim", None),
    ("POST", "/servicing/requests", {
        "request_id": "req_anon", "customer_id": "cust_victim", "account_id": "acct_victim",
        "operation": "request_payment", "amount": "500.00",
    }),
    ("GET", "/servicing/requests/req_victim", None),
]
ROUTE_IDS = [f"{method} {path}" for method, path, _ in ROUTES]


# Every operation in the app: (method, path, the schema's operation object).
OPERATIONS = [
    (method.upper(), path, operation)
    for path, methods in app.openapi()["paths"].items()
    for method, operation in methods.items()
]


@pytest.fixture
def victim(client: TestClient) -> TestClient:
    """`client` sends the admin key; seed a customer for the anonymous tests."""
    assert client.post(
        "/customers", json={"customer_id": "cust_victim", "display_name": "Synthetic Customer"}
    ).status_code == 201
    assert client.post("/accounts", json={
        "account_id": "acct_victim", "customer_id": "cust_victim",
        "balance": "900.00", "currency": "USD", "status": "active",
    }).status_code == 201
    assert client.post("/servicing/requests", json={
        "request_id": "req_victim", "customer_id": "cust_victim", "account_id": "acct_victim",
        "operation": "view_account",
    }).status_code == 200
    return client


@pytest.fixture
def anonymous(victim: TestClient) -> TestClient:
    # Same database (the `client` fixture's override), no admin key.
    return TestClient(app)


def call(client: TestClient, method: str, path: str, body, headers=None):
    return client.request(method, path, json=body, headers=headers)


def row_counts() -> dict[str, int]:
    db_generator = app.dependency_overrides[get_db]()
    db = next(db_generator)
    try:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (
                models.CustomerRecord,
                models.AccountRecord,
                models.ServicingRequestRecord,
                models.ServicingAuditEvent,
            )
        }
    finally:
        db_generator.close()


def test_every_route_is_back_office_agent_or_health() -> None:
    back_office = []
    for method, path, operation in OPERATIONS:
        schemes = {name for requirement in operation.get("security", []) for name in requirement}
        if path.startswith(BACK_OFFICE_PREFIXES):
            back_office.append((method, path))
            assert schemes == {"AdminKey"}, path
        elif path.startswith("/agent/"):
            assert schemes == {"HTTPBearer"}, path
        else:
            assert (method, path) in OPEN_ROUTES, f"unprotected route {method} {path}"

    # The ROUTES table covers every back-office route.
    assert len(back_office) == len(ROUTES)


def test_no_route_is_hidden_from_the_schema() -> None:
    routes = [*app.routes, *(r for module in ROUTER_MODULES for r in module.router.routes)]
    api_routes = [route for route in routes if isinstance(route, APIRoute)]

    hidden = {route.path for route in api_routes if not route.include_in_schema}
    assert hidden == HIDDEN_ROUTES
    assert len(api_routes) - len(hidden) == len(OPERATIONS)


@pytest.mark.parametrize(
    ("method", "path"),
    [(method, path) for method, path, _ in OPERATIONS if (method, path) not in OPEN_ROUTES],
    ids=[f"{method} {path}" for method, path, _ in OPERATIONS if (method, path) not in OPEN_ROUTES],
)
def test_every_route_refuses_anonymous_callers(anonymous, method, path) -> None:
    # The schema says what each route declares; this checks what it does.
    concrete = path.replace("{", "").replace("}", "")  # e.g. /accounts/account_id

    response = anonymous.request(method, concrete, json={})

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_back_office_routes_work_with_the_key(victim, method, path, body) -> None:
    assert call(victim, method, path, body).status_code in (200, 201)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_back_office_routes_refuse_without_the_key(anonymous, method, path, body) -> None:
    before = row_counts()

    response = call(anonymous, method, path, body)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing admin key"}
    assert row_counts() == before


def _session_token() -> str:
    return issue_session_token(
        "cust_victim", secret=config.SESSION_TOKEN_SECRET.encode("utf-8"), ttl_seconds=600
    )


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: {ADMIN_KEY_HEADER: "wrong"}, id="wrong key"),
        pytest.param(lambda: {ADMIN_KEY_HEADER: ""}, id="empty key"),
        pytest.param(lambda: {ADMIN_KEY_HEADER: config.ADMIN_API_KEY[:-1] + "!"}, id="last character wrong"),
        pytest.param(lambda: {ADMIN_KEY_HEADER: config.ADMIN_API_KEY[:-1]}, id="key too short by one"),
        pytest.param(lambda: {ADMIN_KEY_HEADER: config.ADMIN_API_KEY + "x"}, id="key plus extra"),
        pytest.param(lambda: {"X-Admin-Key-Other": config.ADMIN_API_KEY}, id="wrong header name"),
        pytest.param(lambda: {"Authorization": f"Bearer {config.ADMIN_API_KEY}"}, id="key as bearer"),
        pytest.param(lambda: {"Authorization": f"Bearer {_session_token()}"}, id="customer session token"),
        pytest.param(lambda: {ADMIN_KEY_HEADER: _session_token()}, id="session token as key"),
    ],
)
def test_wrong_credentials_are_refused(anonymous, headers) -> None:
    before = row_counts()
    method, path, body = ROUTES[5]  # recording a payment request for someone else

    response = call(anonymous, method, path, body, headers=headers())

    assert response.status_code == 401
    assert row_counts() == before


def test_the_key_is_checked_before_anything_else(anonymous) -> None:
    # No validation errors or "not found" for anonymous callers: they learn
    # nothing about the schema or which IDs exist.
    assert anonymous.post("/servicing/requests", json={"nonsense": True}).status_code == 401
    assert anonymous.get("/accounts/acct_does_not_exist").status_code == 401
    assert anonymous.get("/accounts/acct_victim").status_code == 401


@pytest.mark.parametrize("key", [None, "", "x" * 31])
def test_missing_or_short_key_means_not_configured(victim, monkeypatch, key) -> None:
    # The client still sends the old key; a missing or weak server key must
    # refuse everyone, never fall back to open.
    monkeypatch.setattr(config, "ADMIN_API_KEY", key)

    response = victim.get("/accounts/acct_victim")

    assert response.status_code == 503
    assert response.json() == {"detail": "Admin API is not configured"}


def test_a_32_byte_key_is_enough(victim, monkeypatch) -> None:
    key = "k" * 32
    monkeypatch.setattr(config, "ADMIN_API_KEY", key)

    response = victim.get("/accounts/acct_victim", headers={ADMIN_KEY_HEADER: key})

    assert response.status_code == 200


def test_the_admin_key_does_not_open_the_agent(anonymous) -> None:
    for headers in (
        {ADMIN_KEY_HEADER: config.ADMIN_API_KEY},
        {"Authorization": f"Bearer {config.ADMIN_API_KEY}"},
    ):
        assert anonymous.post("/agent/sessions", headers=headers).status_code == 401


def test_health_checks_need_no_key(anonymous) -> None:
    assert anonymous.get("/health").status_code == 200
    assert anonymous.get("/health/ready").status_code == 200


def test_every_websocket_route_is_known_and_signs_in() -> None:
    # The schema-based inventory above can't see WebSocket routes.
    routes = [*app.routes, *(r for module in ROUTER_MODULES for r in module.router.routes)]
    websocket_paths = {r.path for r in routes if isinstance(r, APIWebSocketRoute)}

    assert websocket_paths == WEBSOCKET_ROUTES


def test_the_live_route_refuses_a_caller_who_doesnt_sign_in(client) -> None:
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/agent/sessions/sess_x/live") as ws:
        ws.send_text('{"type": "start", "token": "not-a-token"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4401
