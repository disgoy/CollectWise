"""Prices and usage counting for the cost estimates that the eval, the smoke
scripts and the voice checks print. Estimates only: nothing here affects a
grade or a decision.

Published pay-as-you-go prices in USD. Claude (Sonnet 5.5, Haiku 5.5): per
million tokens, with cache writes at 1.25x input and cache reads at 0.1x.
Deepgram (deepgram.com/pricing, checked 2026-10-30): Nova-3 monolingual
pre-recorded speech-to-text per minute, Aura-2 text-to-speech per 1,000
characters.
"""

import logging
from collections.abc import Mapping

CLAUDE_PRICES = {
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1

DEEPGRAM_STT_PER_MINUTE = 0.0043
DEEPGRAM_TTS_PER_1K_CHARS = 0.030

# The usage fields of a Claude response, as the API names them.
USAGE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def claude_cost(model: str, usage: Mapping[str, int]) -> float:
    """Estimated cost of `usage` (API field names) on `model`. Raises
    ValueError for a model with no price, rather than reporting $0."""
    price = next((p for name, p in CLAUDE_PRICES.items() if model.startswith(name)), None)
    if price is None:
        raise ValueError(f"no price for model {model}")
    per_input_token = price["input"] / 1_000_000
    return (
        usage["input_tokens"] * per_input_token
        + usage["cache_creation_input_tokens"] * per_input_token * CACHE_WRITE_MULTIPLIER
        + usage["cache_read_input_tokens"] * per_input_token * CACHE_READ_MULTIPLIER
        + usage["output_tokens"] * price["output"] / 1_000_000
    )


def deepgram_stt_cost(audio_seconds: float) -> float:
    return audio_seconds / 60 * DEEPGRAM_STT_PER_MINUTE


def deepgram_tts_cost(characters: int) -> float:
    return characters / 1000 * DEEPGRAM_TTS_PER_1K_CHARS


class UsageCollector(logging.Handler):
    """Collects the per-call usage that the Claude client logs.

    Attach it to the "backend.app.agent.claude" logger at INFO level. It reads
    the client's "Claude call:" log line; test_costs.py pins that format.
    """

    def __init__(self) -> None:
        super().__init__()
        # One dict per model call: "stop" plus the USAGE_FIELDS.
        self.calls: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        if not str(record.msg).startswith("Claude call:"):
            return
        stop, input_tokens, cache_read, cache_write, output_tokens = record.args
        self.calls.append({
            "stop": stop,
            "input_tokens": input_tokens or 0,
            "cache_creation_input_tokens": cache_write or 0,
            "cache_read_input_tokens": cache_read or 0,
            "output_tokens": output_tokens or 0,
        })

    @property
    def totals(self) -> dict[str, int]:
        return {key: sum(call[key] for call in self.calls) for key in USAGE_FIELDS}
