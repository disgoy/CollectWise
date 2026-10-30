"""The shared cost estimates (evals/costs.py): prices, and the usage collector
that reads the Claude client's log line."""

import logging
from types import SimpleNamespace

import pytest

from backend.app.agent.claude import ClaudeClient
from evals.agent_conversations.run_eval import cost_usd
from evals.costs import (
    UsageCollector,
    claude_cost,
    deepgram_stt_cost,
    deepgram_tts_cost,
)


class FakeSDK:
    """Answers each create() with the next usage; sends nothing."""

    def __init__(self, *usages: dict) -> None:
        self._usages = list(usages)
        create = SimpleNamespace(create=self._create)
        self.messages = create
        self.beta = SimpleNamespace(messages=create)

    def _create(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="hi")],
            usage=SimpleNamespace(**self._usages.pop(0)),
        )


@pytest.fixture
def collector():
    usage = UsageCollector()
    claude_logger = logging.getLogger("backend.app.agent.claude")
    previous = claude_logger.level
    claude_logger.setLevel(logging.INFO)
    claude_logger.addHandler(usage)
    yield usage
    claude_logger.removeHandler(usage)
    claude_logger.setLevel(previous)


def test_collector_reads_the_claude_clients_usage_line(collector) -> None:
    # Distinct numbers per field, so a swapped field can't go unnoticed.
    sdk = FakeSDK(
        {"input_tokens": 11, "cache_creation_input_tokens": 22, "cache_read_input_tokens": 33, "output_tokens": 44},
        {"input_tokens": 1, "cache_creation_input_tokens": None, "cache_read_input_tokens": 3, "output_tokens": 4},
        # The API may omit any of them.
        {"input_tokens": None, "cache_creation_input_tokens": None,
         "cache_read_input_tokens": None, "output_tokens": None},
    )
    client = ClaudeClient(sdk, model="claude-sonnet-5-5", effort="medium")  # type: ignore[arg-type]

    for _ in range(3):
        client.create(system="s", tools=[], messages=[{"role": "user", "content": "hi"}])

    assert collector.calls == [
        {"stop": "end_turn", "input_tokens": 11, "cache_creation_input_tokens": 22,
         "cache_read_input_tokens": 33, "output_tokens": 44},
        {"stop": "end_turn", "input_tokens": 1, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 3, "output_tokens": 4},
        {"stop": "end_turn", "input_tokens": 0, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 0, "output_tokens": 0},
    ]
    assert collector.totals == {
        "input_tokens": 12, "cache_creation_input_tokens": 22,
        "cache_read_input_tokens": 36, "output_tokens": 48,
    }


def test_collector_ignores_other_log_lines(collector) -> None:
    logging.getLogger("backend.app.agent.claude").warning("Claude API unavailable: %s", "APITimeoutError")

    assert collector.calls == []


MILLION = {
    "input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
    "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000,
}


@pytest.mark.parametrize(
    ("field", "sonnet", "haiku"),
    [
        ("input_tokens", 2.00, 0.10),
        ("cache_creation_input_tokens", 2.50, 0.125),
        ("cache_read_input_tokens", 0.20, 0.01),
        ("output_tokens", 10.00, 0.50),
    ],
)
def test_claude_cost_per_million_tokens(field, sonnet, haiku) -> None:
    usage = {key: 0 for key in MILLION} | {field: 1_000_000}

    assert claude_cost("claude-sonnet-5-5", usage) == pytest.approx(sonnet)
    assert claude_cost("claude-haiku-5-5", usage) == pytest.approx(haiku)


def test_claude_cost_matches_dated_model_names_and_refuses_unknown_ones() -> None:
    assert claude_cost("claude-sonnet-5-5-20261001", MILLION) == pytest.approx(14.70)
    with pytest.raises(ValueError):
        claude_cost("claude-unknown-1", MILLION)


def test_deepgram_costs() -> None:
    assert deepgram_stt_cost(60) == pytest.approx(0.0043)
    assert deepgram_tts_cost(1000) == pytest.approx(0.030)


def test_the_eval_uses_the_shared_prices() -> None:
    assert cost_usd("claude-haiku-5-5", MILLION) == pytest.approx(claude_cost("claude-haiku-5-5", MILLION))
    assert cost_usd(None, MILLION) == 0.0
    assert cost_usd("claude-sonnet-5-5", None) == 0.0
