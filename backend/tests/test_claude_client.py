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
from backend.app.api import providers
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


class MidStream:
    """A stream that sends `pieces` of text, then fails with `error`."""

    def __init__(self, pieces: list[str], error: Exception) -> None:
        self.pieces = pieces
        self.error = error


class FakeStream:
    """Shaped like the SDK's message stream manager and stream."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    def __enter__(self) -> "FakeStream":
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    @property
    def text_stream(self):
        if isinstance(self.outcome, MidStream):
            yield from self.outcome.pieces
            raise self.outcome.error
        for block in self.outcome.content:
            if getattr(block, "type", None) == "text":
                middle = len(block.text) // 2
                yield block.text[:middle]
                yield block.text[middle:]

    def get_final_message(self) -> Any:
        return self.outcome


class FakeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stream(self, **kwargs: Any) -> FakeStream:
        self.stream_calls.append(kwargs)
        return FakeStream(self.outcomes.pop(0))


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
        providers.get_llm()
    assert raised.value.status_code == 503


def test_get_llm_builds_the_claude_client_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(config, "AGENT_LLM", "anthropic")
    monkeypatch.setattr(config, "AGENT_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(config, "AGENT_EFFORT", "low")
    providers._claude_client.cache_clear()
    try:
        # Building the client sends no request.
        llm = providers.get_llm()
        assert isinstance(llm, ClaudeClient)
        assert providers.get_llm() is llm
        assert llm._effort == "low"
    finally:
        providers._claude_client.cache_clear()


# --- streaming (M5.1) -------------------------------------------------------


def stream_call(client: ClaudeClient, pieces: list[str], messages: list | None = None) -> Any:
    return client.create(
        system=SYSTEM_PROMPT,
        tools=tool_schemas(),
        messages=messages or [{"role": "user", "content": "hi"}],
        on_text=pieces.append,
    )


def test_streaming_sends_exactly_the_same_request() -> None:
    messages = [{"role": "user", "content": "hi"}]
    plain = FakeAnthropic(sdk_response("end_turn", text("hello")))
    streamed = FakeAnthropic(sdk_response("end_turn", text("hello")))

    call(claude(plain), messages)
    stream_call(claude(streamed), [], messages)

    assert streamed.beta.messages.stream_calls == plain.beta.messages.calls
    assert streamed.beta.messages.calls == []


def test_streaming_without_fallbacks_uses_the_plain_endpoint() -> None:
    sdk = FakeAnthropic(sdk_response("end_turn", text("hello")))

    stream_call(claude(sdk, use_fallbacks=False), [])

    [sent] = sdk.messages.stream_calls
    assert "betas" not in sent and "fallbacks" not in sent


def test_streamed_text_goes_out_in_order_and_the_final_message_is_returned() -> None:
    final = sdk_response("end_turn", thinking(), text("Your balance is 500.00 USD."), text(" Anything else?"))
    pieces: list[str] = []

    response = stream_call(claude(FakeAnthropic(final)), pieces)

    assert response is final
    assert "".join(pieces) == "Your balance is 500.00 USD. Anything else?"
    assert len(pieces) == 4


def test_streaming_still_logs_usage(caplog) -> None:
    with caplog.at_level("INFO", logger="backend.app.agent.claude"):
        stream_call(claude(FakeAnthropic(sdk_response("end_turn", text("hi")))), [])

    assert "Claude call: stop=end_turn input=10" in caplog.text


@pytest.mark.parametrize(
    "outcome",
    [
        anthropic.APIConnectionError(request=REQUEST),
        MidStream(["Your bal"], anthropic.APIConnectionError(request=REQUEST)),
        MidStream(["Your bal"], status_error(anthropic.InternalServerError, 500)),
    ],
    ids=["before-text", "mid-stream", "mid-stream-500"],
)
def test_a_stream_that_fails_is_an_outage(outcome) -> None:
    pieces: list[str] = []

    with pytest.raises(LLMUnavailableError):
        stream_call(claude(FakeAnthropic(outcome)), pieces)

    # Text already passed on stays passed on; the caller of create decides.
    assert pieces == (["Your bal"] if isinstance(outcome, MidStream) else [])


def test_our_mistakes_mid_stream_are_not_disguised() -> None:
    error = status_error(anthropic.BadRequestError, 400)

    with pytest.raises(anthropic.BadRequestError):
        stream_call(claude(FakeAnthropic(MidStream(["x"], error))), [])


def fallback() -> SimpleNamespace:
    return SimpleNamespace(type="fallback")


def test_a_mid_stream_fallback_drops_the_declined_attempts_blocks() -> None:
    declined_tool = tool_use("toolu_x", "propose_payment", {"account_id": "acct_a_1", "amount": "50"})
    kept = [text("Let me "), fallback(), thinking(), text("check that.")]
    final = sdk_response("end_turn", thinking(), kept[0], declined_tool, kept[1], kept[2], kept[3])

    response = stream_call(claude(FakeAnthropic(final)), [])

    assert response.content == kept


def test_only_blocks_before_the_last_fallback_are_dropped() -> None:
    # Two fallbacks in a row: everything from the last boundary on is the
    # attempt that answered, so its thinking and tool call stay.
    first_boundary = fallback()
    answering = [fallback(), thinking(), tool_use("toolu_2", "list_accounts", {})]
    final = sdk_response("tool_use", thinking(), first_boundary, thinking(), *answering)

    response = stream_call(claude(FakeAnthropic(final)), [])

    assert response.content == [first_boundary, *answering]


def test_without_a_fallback_nothing_is_dropped() -> None:
    blocks = [thinking(), text("Sure."), tool_use("toolu_1", "list_accounts", {})]

    response = stream_call(claude(FakeAnthropic(sdk_response("tool_use", *blocks))), [])

    assert response.content == blocks


def test_a_declined_attempts_tool_call_never_runs(db_session: Session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    session = SessionStore().create("cust_a")
    declined_tool = tool_use("toolu_x", "propose_payment", {"account_id": "acct_a_1", "amount": "50.00"})
    final = sdk_response("end_turn", declined_tool, fallback(), text("I can help with your accounts."))
    pieces: list[str] = []

    result = run_turn(
        session, "pay 50", llm=claude(FakeAnthropic(final)), db=db_session,
        system=SYSTEM_PROMPT, on_text=pieces.append,
    )

    assert result.tool_calls == []
    assert session.state.pending_payment is None
    assert result.reply == "I can help with your accounts."
