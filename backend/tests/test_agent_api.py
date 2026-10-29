import time

import pytest
from fastapi.testclient import TestClient

from backend.app import config
from backend.app.agent.loop import REPLY_HANDED_OFF
from backend.app.agent.sessions import SessionStore
from backend.app.api.agent import get_llm, get_session_store
from backend.app.auth import dev_token
from backend.app.auth.session_tokens import issue_session_token, verify_session_token
from backend.app.main import app
from backend.tests.fake_llm import FakeLLM, say, use

OTHER_SECRET = b"some-other-secret-that-is-32-bytes!!"


def token_for(customer_id: str, *, ttl: int = 600, secret: bytes | None = None, now: float | None = None) -> str:
    return issue_session_token(
        customer_id,
        secret=secret or config.SESSION_TOKEN_SECRET.encode("utf-8"),
        ttl_seconds=ttl,
        now=now,
    )


def auth(customer_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(customer_id)}"}


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM([])


@pytest.fixture
def agent_client(client: TestClient, llm: FakeLLM) -> TestClient:
    # `client` provides an isolated database and clears overrides afterwards.
    store = SessionStore()  # a fresh store per test
    app.dependency_overrides[get_llm] = lambda: llm
    app.dependency_overrides[get_session_store] = lambda: store

    for customer_id in ("cust_a", "cust_b"):
        assert client.post(
            "/customers",
            json={"customer_id": customer_id, "display_name": "Synthetic Customer"},
        ).status_code == 201
    assert client.post(
        "/accounts",
        json={
            "account_id": "acct_a_1",
            "customer_id": "cust_a",
            "balance": "500.00",
            "currency": "USD",
        },
    ).status_code == 201
    return client


def new_session(client: TestClient, customer_id: str = "cust_a") -> str:
    response = client.post("/agent/sessions", headers=auth(customer_id))
    assert response.status_code == 201
    return response.json()["session_id"]


def send(client: TestClient, session_id: str, text: str, customer_id: str = "cust_a"):
    return client.post(
        f"/agent/sessions/{session_id}/turns",
        json={"text": text},
        headers=auth(customer_id),
    )


def test_create_session(agent_client) -> None:
    response = agent_client.post("/agent/sessions", headers=auth("cust_a"))

    assert response.status_code == 201
    assert response.json()["session_id"].startswith("sess_")


def test_create_session_for_unknown_customer(agent_client) -> None:
    response = agent_client.post("/agent/sessions", headers=auth("cust_unknown"))

    assert response.status_code == 404


def test_turn_uses_tools_and_returns_only_the_reply(agent_client, llm) -> None:
    session_id = new_session(agent_client)
    llm.script += [use(("t1", "list_accounts", {})), say("You have one USD account.")]

    response = send(agent_client, session_id, "What accounts do I have?")

    assert response.status_code == 200
    assert response.json() == {"reply": "You have one USD account.", "handed_off": False}


def test_payment_over_two_http_turns(agent_client, llm) -> None:
    session_id = new_session(agent_client)
    llm.script += [
        use(("t1", "propose_payment", {"account_id": "acct_a_1", "amount": 200})),
        say("Shall I request a payment of 200.00 USD?"),
        use(("t2", "confirm_payment", {})),
        say("Your payment request is recorded."),
    ]

    send(agent_client, session_id, "Pay 200")
    response = send(agent_client, session_id, "Yes")

    assert response.json()["reply"] == "Your payment request is recorded."
    # The tool result the model saw carries the request ID; check it via the API.
    confirm_result = llm.requests[-1]["messages"][-1]["content"][0]["content"]
    assert '"status": "recorded"' in confirm_result


def test_handoff_ends_the_conversation(agent_client, llm) -> None:
    session_id = new_session(agent_client)
    llm.script += [
        use(("t1", "handoff_to_human", {"reason": "caller_requested"})),
        say("I'm transferring you now."),
    ]

    first = send(agent_client, session_id, "I want a person")
    second = send(agent_client, session_id, "Hello?")

    assert first.json() == {"reply": "I'm transferring you now.", "handed_off": True}
    assert second.json() == {"reply": REPLY_HANDED_OFF, "handed_off": True}


def test_other_customers_session_looks_missing(agent_client, llm) -> None:
    session_id = new_session(agent_client, "cust_a")

    response = send(agent_client, session_id, "hi", customer_id="cust_b")

    assert response.status_code == 404
    assert response.json() == {"detail": "Session not found"}
    assert llm.requests == []


def test_unknown_session(agent_client) -> None:
    assert send(agent_client, "sess_missing", "hi").status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": f"Bearer {token_for('cust_a', ttl=60, now=time.time() - 120)}"},
        {"Authorization": f"Bearer {token_for('cust_a', secret=OTHER_SECRET)}"},
    ],
    ids=["missing", "basic", "empty", "garbage", "expired", "other-secret"],
)
def test_bad_tokens_get_one_generic_401(agent_client, headers) -> None:
    for response in (
        agent_client.post("/agent/sessions", headers=headers),
        agent_client.post("/agent/sessions/sess_x/turns", json={"text": "hi"}, headers=headers),
    ):
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or expired session token"}
        assert response.headers["www-authenticate"] == "Bearer"


def test_tampered_token_is_rejected(agent_client) -> None:
    payload, signature = token_for("cust_a").split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]

    response = agent_client.post(
        "/agent/sessions",
        headers={"Authorization": f"Bearer {payload}.{flipped}"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [{}, {"text": ""}, {"text": "   "}, {"text": "x" * 2001}, {"text": "hi", "customer_id": "cust_b"}],
    ids=["missing", "empty", "blank", "too-long", "customer-id"],
)
def test_turn_body_accepts_only_text(agent_client, llm, body) -> None:
    session_id = new_session(agent_client)

    response = agent_client.post(
        f"/agent/sessions/{session_id}/turns", json=body, headers=auth("cust_a")
    )

    assert response.status_code == 422
    assert llm.requests == []


@pytest.mark.parametrize("secret", [None, "", "too-short"])
def test_missing_or_weak_secret_means_not_configured(
    agent_client, monkeypatch, secret
) -> None:
    headers = auth("cust_a")
    monkeypatch.setattr(config, "SESSION_TOKEN_SECRET", secret)

    response = agent_client.post("/agent/sessions", headers=headers)

    assert response.status_code == 503
    assert response.json() == {"detail": "Agent is not configured"}


def test_turn_without_a_configured_llm(agent_client) -> None:
    session_id = new_session(agent_client)
    del app.dependency_overrides[get_llm]

    response = send(agent_client, session_id, "hi")

    assert response.status_code == 503


def test_dev_token_helper(capsys) -> None:
    assert dev_token.main(["dev_token", "cust_a"]) == 0
    token = capsys.readouterr().out.strip()

    assert verify_session_token(token, secret=config.SESSION_TOKEN_SECRET.encode("utf-8")) == "cust_a"


def test_dev_token_helper_refuses_without_secret_or_args(monkeypatch, capsys) -> None:
    assert dev_token.main(["dev_token"]) == 2
    monkeypatch.setattr(config, "SESSION_TOKEN_SECRET", None)
    assert dev_token.main(["dev_token", "cust_a"]) == 1
    assert capsys.readouterr().out == ""
