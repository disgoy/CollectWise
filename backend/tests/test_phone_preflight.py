"""The phone call preflight (M7.5): it says what's missing by name, never a
setting's value or the phone number, and through the tunnel it proves the
webhook answers with its signature check on and nothing else does."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app import config
from backend.app.db.models import PhoneCredentialRecord
from backend.app.services.phone_identity import set_phone_and_pin
from backend.tests.helpers import seed_customer
from backend.tests.test_live_report import _Borrowed
from scripts import phone_preflight

BASE = "https://cw-demo.ngrok-free.app"
TOKEN = "marker-twilio-token-7f3a"
ANTHROPIC = "marker-anthropic-key-91c2"
DEEPGRAM = "marker-deepgram-key-44d0"
NUMBER = "+15555550142"
KEYS = {"ANTHROPIC_API_KEY": ANTHROPIC, "DEEPGRAM_API_KEY": DEEPGRAM}
# What a correctly set up server answers through the tunnel.
READY = {("POST", "/telephony/twilio/voice"): 403, ("GET", "/docs"): 404, ("GET", "/live"): 404}


@pytest.fixture
def ready(db_session, monkeypatch):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", BASE)
    seed_customer(db_session, "cust_demo", [])
    set_phone_and_pin(db_session, "cust_demo", phone_number=NUMBER, pin="482916")
    return db_session


def tunnel(answers: dict):
    asked = []

    def fetch(method: str, url: str):
        assert url.startswith(BASE + "/")
        asked.append((method, url[len(BASE):]))
        return answers.get((method, url[len(BASE):]))
    fetch.asked = asked
    return fetch


def run(db, capsys, answers=READY, environ=KEYS) -> tuple[int, str]:
    code = phone_preflight.main([], session_factory=lambda: _Borrowed(db), fetch=tunnel(answers), environ=environ)
    return code, capsys.readouterr().out


def test_ready_prints_the_webhook_to_paste_and_no_secrets(ready, capsys) -> None:
    code, out = run(ready, capsys)

    assert code == 0
    assert "FIX" not in out
    assert f"{BASE}/telephony/twilio/voice" in out
    for secret in (TOKEN, ANTHROPIC, DEEPGRAM, config.SESSION_TOKEN_SECRET, NUMBER, NUMBER[2:]):
        assert secret not in out


@pytest.mark.parametrize(("setting", "value", "named"), [
    ("TWILIO_AUTH_TOKEN", None, "TWILIO_AUTH_TOKEN"),
    ("SESSION_TOKEN_SECRET", "too-short", "COLLECTWISE_SESSION_SECRET"),
    ("PUBLIC_BASE_URL", "http://cw-demo.ngrok-free.app", "COLLECTWISE_PUBLIC_BASE_URL"),
    ("PUBLIC_BASE_URL", "https://cw-demo.ngrok-free.app/telephony", "COLLECTWISE_PUBLIC_BASE_URL"),
])
def test_a_missing_or_wrong_setting_is_named(ready, capsys, monkeypatch, setting, value, named) -> None:
    monkeypatch.setattr(config, setting, value)

    code, out = run(ready, capsys)

    assert code == 1
    assert f"FIX   {named}" in out
    assert "too-short" not in out


@pytest.mark.parametrize(("length", "ok"), [(31, False), (32, True)])
def test_the_session_secret_needs_what_the_tokens_need(ready, capsys, monkeypatch, length, ok) -> None:
    monkeypatch.setattr(config, "SESSION_TOKEN_SECRET", "s" * length)

    code, out = run(ready, capsys)

    assert (code == 0) is ok
    assert ("FIX   COLLECTWISE_SESSION_SECRET" in out) is not ok


@pytest.mark.parametrize("missing", ["ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY"])
def test_a_missing_api_key_is_named(ready, capsys, missing) -> None:
    code, out = run(ready, capsys, environ={**KEYS, missing: ""})

    assert code == 1
    assert f"FIX   {missing} is set" in out


def test_without_a_public_address_the_tunnel_isnt_tried(ready, capsys, monkeypatch) -> None:
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", None)
    fetch = tunnel(READY)

    code = phone_preflight.main([], session_factory=lambda: _Borrowed(ready), fetch=fetch, environ=KEYS)

    assert code == 1
    assert fetch.asked == []
    assert "the tunnel wasn't checked" in capsys.readouterr().out


def test_a_demo_customer_without_phone_or_pin_is_told_to_seed(db_session, capsys, monkeypatch) -> None:
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", BASE)
    seed_customer(db_session, "cust_demo", [])

    code, out = run(db_session, capsys)

    assert code == 1
    assert "FIX   the demo customer has a phone number: run: python -m scripts.seed_demo --phone" in out
    assert "FIX   the demo customer has a phone PIN" in out


def test_a_locked_pin_is_reported_and_an_expired_lock_is_not(ready, capsys) -> None:
    credential = ready.get(PhoneCredentialRecord, "cust_demo")
    credential.locked_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    ready.commit()
    code, out = run(ready, capsys)
    assert code == 1
    assert "FIX   the phone PIN isn't locked" in out

    credential.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    ready.commit()
    code, out = run(ready, capsys)
    assert code == 0
    assert "ok    the phone PIN isn't locked" in out


@pytest.mark.parametrize(("answers", "says"), [
    ({}, "nothing answered: is ngrok running"),
    ({**READY, ("POST", "/telephony/twilio/voice"): 503}, "isn't set up for phone calls"),
    ({**READY, ("POST", "/telephony/twilio/voice"): 404}, "something else answered"),
    ({**READY, ("POST", "/telephony/twilio/voice"): 200}, "unexpected status 200"),
    ({**READY, ("GET", "/docs"): 200}, "FIX   /docs is hidden on the public address"),
    ({**READY, ("GET", "/live"): 200}, "FIX   /live is hidden on the public address"),
])
def test_each_tunnel_problem_is_explained(ready, capsys, answers, says) -> None:
    code, out = run(ready, capsys, answers=answers)

    assert code == 1
    assert says in out
    assert "Ready." not in out


def test_an_old_schema_says_to_upgrade(ready, capsys, monkeypatch) -> None:
    monkeypatch.setattr(phone_preflight, "schema_is_current", lambda bind: False)

    code, out = run(ready, capsys)

    assert code == 1
    assert "FIX   the database schema is current: run: alembic upgrade head" in out


def test_fetch_returns_the_status_or_none_when_nothing_answers() -> None:
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            seen.append((self.command, self.headers.get("ngrok-skip-browser-warning")))
            self.send_response(403 if self.command == "POST" else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _answer

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/telephony/twilio/voice"
        assert phone_preflight.fetch("POST", url) == 403
        assert phone_preflight.fetch("GET", url) == 200
    finally:
        server.shutdown()
        server.server_close()
    assert seen == [("POST", "1"), ("GET", "1")]

    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    assert phone_preflight.fetch("GET", f"http://127.0.0.1:{port}/") is None
