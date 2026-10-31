"""LLMClient backed by the Anthropic Messages API (anthropic SDK 2.x)."""

import logging
from collections.abc import Callable
from typing import Any

import anthropic

from backend.app import config
from backend.app.agent.llm import LLMResponse, LLMUnavailableError

logger = logging.getLogger(__name__)

# Server-side refusal fallback, "default" form (Claude API only). For Claude
# Sonnet 5.5 it retries cyber and frontier_llm declines on another model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Thinking counts toward max_tokens even though its text isn't returned.
DEFAULT_MAX_TOKENS = 16000

# Reaching the API at all should be quick; the read timeout is config's.
CONNECT_TIMEOUT_S = 5.0

# Blocks a declined attempt can leave before a `fallback` block in a streamed
# reply. They must not be sent back to the API, and a tool call from the
# declined attempt must never run. Text before the boundary stays.
_DECLINED_ATTEMPT_BLOCKS = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


def _without_declined_attempt(content: list[Any]) -> list[Any]:
    """Drop what a mid-stream fallback leaves behind (docs: "Echoing fallback
    turns back"). Non-streamed replies never contain it."""
    types = [getattr(block, "type", None) for block in content]
    if "fallback" not in types:
        return content
    boundary = len(types) - 1 - types[::-1].index("fallback")
    return [
        block
        for index, block in enumerate(content)
        if index >= boundary or types[index] not in _DECLINED_ATTEMPT_BLOCKS
    ]


def is_transient_error(exc: anthropic.APIError) -> bool:
    """Failures worth a friendly "try again" reply. Anything else (400, 401,
    403, 404 ...) means our request or configuration is wrong and must raise.
    Public: the eval's judge retries on the same errors."""
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.RateLimitError)):
        return True  # includes APITimeoutError
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


class ClaudeClient:
    def __init__(
        self,
        client: anthropic.Anthropic,
        *,
        model: str,
        effort: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        use_fallbacks: bool = True,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._use_fallbacks = use_fallbacks

    def create(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            # Tools render before system, so this breakpoint caches both.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            # Schemas are sent as-is, without strict: true. Strict mode rejects
            # minLength/maxLength/minimum/maximum, and execute_tool validates
            # every argument anyway (D35).
            "tools": [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["input_schema"],
                }
                for tool in tools
            ],
            "messages": messages,
            # Thinking is adaptive by default on this model; effort sets depth.
            "output_config": {"effort": self._effort},
            # Automatic breakpoint on the latest block: each request in a
            # tool loop reuses the conversation prefix written by the last.
            "cache_control": {"type": "ephemeral"},
        }

        if self._use_fallbacks:
            messages_api = self._client.beta.messages
            params |= {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        else:
            messages_api = self._client.messages

        try:
            if on_text is None:
                response = messages_api.create(**params)
            else:
                # The same request, streamed: text goes out as it arrives and
                # the final message is returned as usual. An error can come
                # after some text was already passed on.
                with messages_api.stream(**params) as stream:
                    for text in stream.text_stream:
                        on_text(text)
                    response = stream.get_final_message()
                response.content = _without_declined_attempt(response.content)
        except anthropic.APIError as exc:
            if not is_transient_error(exc):
                raise
            logger.warning("Claude API unavailable: %s", type(exc).__name__)
            raise LLMUnavailableError(type(exc).__name__) from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "Claude call: stop=%s input=%s cache_read=%s cache_write=%s output=%s",
                response.stop_reason,
                getattr(usage, "input_tokens", None),
                getattr(usage, "cache_read_input_tokens", None),
                getattr(usage, "cache_creation_input_tokens", None),
                getattr(usage, "output_tokens", None),
            )
        return response


def build_claude_client(*, model: str, effort: str) -> ClaudeClient:
    """Credentials resolve the SDK's usual way (ANTHROPIC_API_KEY, or an
    `ant auth login` profile). Constructing the client makes no request.
    Timeouts and retries come from config (M6.1); a timeout surfaces as
    LLMUnavailableError like any other outage."""
    client = anthropic.Anthropic(
        timeout=anthropic.Timeout(config.AGENT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        max_retries=config.AGENT_MAX_RETRIES,
    )
    return ClaudeClient(client, model=model, effort=effort)
