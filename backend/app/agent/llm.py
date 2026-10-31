from collections.abc import Callable
from typing import Any, Protocol


class LLMUnavailableError(Exception):
    """The model could not be reached or returned an API error.

    LLM clients must translate every provider failure into this, so the agent
    loop can catch it without also hiding bugs in our own code.
    """


class LLMResponse(Protocol):
    # e.g. "end_turn", "tool_use", "refusal", "max_tokens"
    stop_reason: str | None
    # Content blocks in Anthropic's shape. The loop only reads `type`, `text`,
    # `id`, `name` and `input`, and appends the list back unchanged.
    content: list[Any]


class LLMClient(Protocol):
    def create(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """Send the conversation and return the model's next response.

        `tools` are the provider-neutral schemas from tool_schemas(); the
        client converts them to its provider's format. With `on_text`, the
        reply's text is also passed to it piece by piece as it is generated,
        so speech can start early (M5); the return value is the same. Callers
        only pass `on_text` when they want streaming, so clients that never
        stream may leave it out.
        """
        ...
