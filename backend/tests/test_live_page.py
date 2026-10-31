"""The live-call demo page and demo sign-in (M5.6, D84)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.app import config
from backend.app.api.live_page import STATIC_DIR, content_security_policy
from backend.app.auth.session_tokens import verify_session_token

REPO = Path(__file__).resolve().parents[2]


# --- demo sign-in --------------------------------------------------------------------


@pytest.fixture
def demo(client, monkeypatch):
    monkeypatch.setattr(config, "DEMO_LOGIN", True)
    monkeypatch.setattr(config, "DEMO_CUSTOMERS", ("cust_demo", "cust_demo_2"))
    for customer_id, name in (("cust_demo", "Demo Customer"), ("cust_real", "Real Customer")):
        assert client.post("/customers", json={"customer_id": customer_id, "display_name": name}).status_code == 201
    return client


def test_demo_sign_in_is_off_by_default_and_then_looks_absent(client) -> None:
    assert config.DEMO_LOGIN is False
    assert client.get("/demo/customers").status_code == 404
    assert client.post("/demo/sign-in", json={"customer_id": "cust_demo"}).status_code == 404


def test_the_switch_needs_exactly_1(monkeypatch) -> None:
    import importlib

    for value, on in (("1", True), ("true", False), ("0", False), ("", False)):
        monkeypatch.setenv("COLLECTWISE_DEMO_LOGIN", value)
        assert importlib.reload(config).DEMO_LOGIN is on
    monkeypatch.delenv("COLLECTWISE_DEMO_LOGIN")
    monkeypatch.setenv("COLLECTWISE_DEMO_CUSTOMERS", " cust_a , ,cust_b")
    assert importlib.reload(config).DEMO_CUSTOMERS == ("cust_a", "cust_b")
    monkeypatch.delenv("COLLECTWISE_DEMO_CUSTOMERS")
    assert importlib.reload(config).DEMO_CUSTOMERS == ("cust_demo",)


def test_only_listed_demo_customers_that_exist_are_offered(demo) -> None:
    assert demo.get("/demo/customers").json() == [{"customer_id": "cust_demo", "display_name": "Demo Customer"}]


def test_signing_in_as_a_demo_customer_gives_a_normal_token(demo) -> None:
    response = demo.post("/demo/sign-in", json={"customer_id": "cust_demo"})

    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == config.SESSION_TOKEN_TTL_SECONDS
    assert verify_session_token(body["token"], secret=config.SESSION_TOKEN_SECRET.encode("utf-8")) == "cust_demo"
    # It works on the agent routes like any other token.
    started = demo.post("/agent/sessions", headers={"Authorization": f"Bearer {body['token']}"})
    assert started.status_code == 201


@pytest.mark.parametrize("customer_id", ["cust_real", "cust_demo_2", "cust_nobody"],
                         ids=["real-customer", "listed-but-missing", "unknown"])
def test_nobody_else_can_be_signed_in(demo, customer_id) -> None:
    assert demo.post("/demo/sign-in", json={"customer_id": customer_id}).status_code == 404


def test_demo_sign_in_without_a_session_secret(demo, monkeypatch) -> None:
    monkeypatch.setattr(config, "SESSION_TOKEN_SECRET", None)

    assert demo.post("/demo/sign-in", json={"customer_id": "cust_demo"}).status_code == 503


def test_demo_sign_in_takes_only_a_customer_id(demo) -> None:
    assert demo.post("/demo/sign-in", json={"customer_id": "cust_demo", "admin": True}).status_code == 422


def test_the_server_warns_loudly_when_demo_sign_in_is_on(monkeypatch, caplog) -> None:
    from fastapi.testclient import TestClient

    from backend.app.main import app

    monkeypatch.setattr(config, "DEMO_LOGIN", True)
    with caplog.at_level("WARNING", logger="backend.app.main"), TestClient(app):
        pass
    assert "Demo sign-in is ON" in caplog.text


# --- the page ------------------------------------------------------------------------


def test_the_page_is_served_with_a_strict_policy(client) -> None:
    response = client.get("/live")

    assert response.status_code == 200
    assert "<title>CollectWise live call</title>" in response.text
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "script-src 'self'" in policy
    assert "connect-src 'self' ws://testserver wss://testserver" in policy
    assert "frame-ancestors 'none'" in policy
    assert response.headers["cache-control"] == "no-store"


def test_an_odd_host_header_is_not_echoed_into_the_policy() -> None:
    assert "evil" not in content_security_policy("evil.com; script-src *")
    assert "connect-src 'self';" in content_security_policy("evil.com; script-src *")


PAGE_FILES = ["app.mjs", "audio.mjs", "playback.mjs", "timeline.mjs", "capture-worklet.js", "live.css"]


@pytest.mark.parametrize("name", PAGE_FILES)
def test_the_page_files_are_served(client, name) -> None:
    assert client.get(f"/live/static/{name}").status_code == 200


def test_the_page_loads_nothing_from_anywhere_else() -> None:
    for path in STATIC_DIR.iterdir():
        text = path.read_text()
        assert not re.search(r"https?://", text), f"{path.name} references an outside URL"
        if path.suffix == ".html":
            assert "<script>" not in text and "style=" not in text, "inline code would be blocked by the policy"


def test_the_page_never_stores_the_token() -> None:
    script = (STATIC_DIR / "app.mjs").read_text()
    assert "localStorage" not in script and "sessionStorage" not in script and "document.cookie" not in script


# --- the page's logic, under Node's test runner ----------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="Node isn't installed")
def test_the_page_logic_passes_its_node_tests() -> None:
    result = subprocess.run(
        ["node", "--test", "backend/tests/js/"], cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout[-3000:]


def test_the_page_has_a_call_clock_and_a_copy_button(client) -> None:
    page = client.get("/live").text

    assert 'id="clock"' in page
    assert 'id="copy"' in page
