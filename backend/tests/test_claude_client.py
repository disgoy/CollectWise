from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent.claude import FALLBACK_BETA, ClaudeClient
from backend.app.agent.llm import LLMUnavailableError
from backend.app.agent.loop import run_turn
from backend.app.agent.prompts import SYSTEM_PROMPT
from backend.app.agent.sessions import SessionStore
from backend.app.api import agent as agent_api
from backend.app.tools.registry import tool_schemas
from backend.tests.helpers import seed_customer

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def sdk_response(stop_reason: str, *blocks: Any) -> SimpleNamespace:
    """Shaped like an SDK message: attributes, not dicts."""
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=list(blocks),
        usage=SimpleNamespace(
            input_tokens=10, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, output_tokens=5,
        ),
    )


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def thinking() -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def tool_use(block_id: str, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=args)


class FakeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeAnthropic:
    """Stands in for anthropic.Anthropic: records requests, sends nothing."""

    def __init__(self, *outcomes: Any) -> None:
        self.messages = FakeMessages(list(outcomes))
        self.beta = SimpleNamespace(messages=FakeMessages(list(outcomes)))


def claude(sdk: FakeAnthropic, **kwargs: Any) -> ClaudeClient:
    return ClaudeClient(sdk, model="claude-sonnet-5-5", effort="medium", **kwargs)  # type: ignore[arg-type]


def call(client: ClaudeClient, messages: list | None = None) -> Any:
    return client.create(
        system=SYSTEM_PROMPT,
        tools=tool_schemas(),
        messages=messages or [{"role": "user", "content": "hi"}],
    )


def status_error(cls: type, status: int) -> Exception:
    return cls("error", response=httpx2.Response(status, request=REQUEST), body=None)


def test_request_shape_with_refusal_fallback() -> None:
    sdk = FakeAnthropic(sdk_response("end_turn", text("hello")))
    messages = [{"role": "user", "content": "hi"}]

    call(claude(sdk), messages)

    [sent] = sdk.beta.messages.calls
    assert sdk.messages.calls == []
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["max_tokens"] == 16000
    assert sent["betas"] == [FALLBACK_BETA]
    assert sent["fallbacks"] == "default"
    assert sent["output_config"] == {"effort": "medium"}
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    assert sent["messages"] is messages


def test_tools_are_sent_unchanged_and_not_strict() -> None:
    sdk = FakeAnthropic(sdk_response("end_turn", text("hello")))

    call(claude(sdk))

    tools = sdk.beta.messages.calls[0]["tools"]
    assert tools == [
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in tool_schemas()
    ]
    assert all("strict" not in tool for tool in tools)


def test_no_disabled_parameters_are_sent() -> None:
    sdk = FakeAnthropic(sdk_response("end_turn", text("hello")))

    call(claude(sdk))

    sent = sdk.beta.messages.calls[0]
    # Removed or rejected on this model / API version.
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert forbidden not in sent


def test_without_fallbacks_uses_the_plain_endpoint() -> None:
    sdk = FakeAnthropic(sdk_response("end_turn", text("hello")))

    call(claude(sdk, use_fallbacks=False))

    [sent] = sdk.messages.calls
    assert "betas" not in sent and "fallbacks" not in sent


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.APITimeoutError(request=REQUEST),
        status_error(anthropic.RateLimitError, 429),
        status_error(anthropic.InternalServerError, 500),
        status_error(anthropic.APIStatusError, 529),
    ],
    ids=["connection", "timeout", "rate-limit", "server-error", "overloaded"],
)
def test_transient_failures_become_llm_unavailable(error: Exception) -> None:
    with pytest.raises(LLMUnavailableError):
        call(claude(FakeAnthropic(error)))


@pytest.mark.parametrize(
    "error",
    [
        status_error(anthropic.BadRequestError, 400),
        status_error(anthropic.AuthenticationError, 401),
        status_error(anthropic.PermissionDeniedError, 403),
        status_error(anthropic.NotFoundError, 404),
    ],
    ids=["bad-request", "auth", "permission", "not-found"],
)
def test_our_mistakes_are_not_disguised_as_outages(error: Exception) -> None:
    with pytest.raises(type(error)):
        call(claude(FakeAnthropic(error)))


def test_full_turn_with_sdk_shaped_responses(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    session = SessionStore().create("cust_a")
    first = sdk_response("tool_use", thinking(), tool_use("toolu_1", "list_accounts", {}))
    second = sdk_response("end_turn", thinking(), text("You have one USD account."))
    sdk = FakeAnthropic(first, second)

    result = run_turn(session, "my accounts?", llm=claude(sdk), db=db_session, system=SYSTEM_PROMPT)

    assert result.reply == "You have one USD account."
    # The assistant content, thinking block included, went back unchanged.
    assert session.messages[1] == {"role": "assistant", "content": first.content}
    second_request = sdk.beta.messages.calls[1]["messages"]
    assert second_request[1]["content"][0] is first.content[0]
    assert second_request[2]["content"][0]["tool_use_id"] == "toolu_1"


def test_get_llm_is_off_unless_enabled(monkeypatch) -> None:
    monkeypatch.setattr(config, "AGENT_LLM", None)

    with pytest.raises(HTTPException) as raised:
        agent_api.get_llm()
    assert raised.value.status_code == 503


def test_get_llm_builds_the_claude_client_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(config, "AGENT_LLM", "anthropic")
    monkeypatch.setattr(config, "AGENT_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(config, "AGENT_EFFORT", "low")
    agent_api._claude_client.cache_clear()
    try:
        # Building the client sends no request.
        llm = agent_api.get_llm()
        assert isinstance(llm, ClaudeClient)
        assert agent_api.get_llm() is llm
        assert llm._effort == "low"
    finally:
        agent_api._claude_client.cache_clear()
