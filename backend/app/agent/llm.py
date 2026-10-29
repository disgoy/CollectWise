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
    ) -> LLMResponse:
        """Send the conversation and return the model's next response.

        `tools` are the provider-neutral schemas from tool_schemas(); the
        client converts them to its provider's format.
        """
        ...
