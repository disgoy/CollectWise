"""The public address answers only Twilio (M7.5): through a tunnel, nothing but
the phone webhook and its media stream is reachable, while the same routes
on the machine's own address work as before."""

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import config
from backend.app.main import app
from backend.app.public_host import _hostname
from backend.tests.helpers import ADMIN_HEADERS
from backend.tests.test_telephony import (  # noqa: F401  (phone is a fixture)
    BASE,
    STREAM_URL,
    phone,
    ring,
    sign,
    start_frame,
    stream_pass,
)

NOT_FOUND = {"detail": "Not Found"}
# The test client's WebSockets ignore its base URL's host, so they name it.
PUBLIC_HOST = {"host": "cw.example.test"}


@pytest.fixture
def public(phone):
    """A client reaching the app the way a tunnel does: under the public host,
    with the admin key (which must not help)."""
    return TestClient(app, base_url=BASE, headers=ADMIN_HEADERS)


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
    ("GET", "/live"),
    ("GET", "/evidence"),
    ("GET", "/health"),
    ("GET", "/demo/customers"),
    ("POST", "/demo/sign-in"),
    ("GET", "/customers/cust_a/accounts"),
    ("POST", "/agent/sessions"),
    ("GET", "/telephony/twilio/voice"),
    ("POST", "/telephony/twilio/voice/"),
    ("POST", "/telephony/twilio/stream"),
])
def test_the_public_address_hides_everything_but_the_webhook(public, monkeypatch, method, path) -> None:
    monkeypatch.setattr(config, "DEMO_LOGIN", True)

    response = public.request(method, path, json={"customer_id": "cust_demo"})

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_the_same_routes_still_work_on_the_local_address(phone, client, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEMO_LOGIN", True)

    assert client.get("/docs").status_code == 200
    assert client.get("/live").status_code == 200
    assert client.get("/demo/customers").status_code == 200
    assert client.get("/customers/cust_a/accounts").status_code == 200


@pytest.mark.parametrize("path", ["/telephony/watch", "/agent/sessions/sess_x/live", "/telephony/twilio/voice"])
def test_the_public_address_refuses_other_websockets(public, path) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with public.websocket_connect(path, headers=PUBLIC_HOST) as ws:
            ws.receive_text()

    # Refused before any route ran: those close with their own codes (4401, 4408...).
    assert closed.value.code == 1000


def test_the_watch_socket_still_opens_locally(phone, client) -> None:
    with client.websocket_connect("/telephony/watch") as ws:
        ws.send_text(json.dumps({"type": "start", "token": "not-a-token"}))  # reached the route: it judges the sign-in
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4401


def test_twilio_reaches_the_webhook_and_the_stream_on_the_public_address(public) -> None:
    answer = ring(public)
    assert answer.status_code == 200
    signed = {**PUBLIC_HOST, "x-twilio-signature": sign(STREAM_URL, {})}
    with public.websocket_connect("/telephony/twilio/stream", headers=signed) as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(start_frame("CA0001", stream_pass(answer.text)))
        assert json.loads(ws.receive_text())["event"] == "media"  # the greeting
        ws.send_text(json.dumps({"event": "stop"}))


@pytest.mark.parametrize("forwarded", ["cw.example.test", "proxy.example, CW.Example.TEST:443"])
def test_a_forwarded_public_host_counts_as_public(phone, client, forwarded) -> None:
    response = client.get("/docs", headers={"X-Forwarded-Host": forwarded})

    assert response.status_code == 404


def test_a_forwarded_other_host_changes_nothing(phone, client) -> None:
    assert client.get("/docs", headers={"X-Forwarded-Host": "elsewhere.example.test"}).status_code == 200


def test_the_public_host_matches_with_any_port(phone) -> None:
    with_port = TestClient(app, base_url="https://cw.example.test:8443")

    assert with_port.get("/docs").status_code == 404


def test_without_a_public_address_nothing_is_hidden(public, monkeypatch) -> None:
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", None)

    assert public.get("/docs").status_code == 200


@pytest.mark.parametrize(("value", "expected"), [
    ("cw.example.test", "cw.example.test"),
    ("CW.Example.Test:443", "cw.example.test"),
    (" cw.example.test ", "cw.example.test"),
    ("[::1]:8000", "::1"),
    ("[::1]", "::1"),
])
def test_hostnames_are_compared_without_port_or_case(value, expected) -> None:
    assert _hostname(value) == expected
