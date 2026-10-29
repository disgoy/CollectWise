"""LLMClient backed by the Anthropic Messages API (anthropic SDK 2.x)."""

import logging
from typing import Any

import anthropic

from backend.app.agent.llm import LLMResponse, LLMUnavailableError

logger = logging.getLogger(__name__)

# Server-side refusal fallback, "default" form (Claude API only). For Claude
# Sonnet 5.5 it retries cyber and frontier_llm declines on another model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Thinking counts toward max_tokens even though its text isn't returned.
DEFAULT_MAX_TOKENS = 16000


def _is_transient(exc: anthropic.APIError) -> bool:
    """Failures worth a friendly "try again" reply. Anything else (400, 401,
    403, 404 ...) means our request or configuration is wrong and must raise."""
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

        try:
            if self._use_fallbacks:
                response = self._client.beta.messages.create(
                    **params,
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
            else:
                response = self._client.messages.create(**params)
        except anthropic.APIError as exc:
            if not _is_transient(exc):
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
    `ant auth login` profile). Constructing the client makes no request."""
    return ClaudeClient(anthropic.Anthropic(), model=model, effort=effort)
