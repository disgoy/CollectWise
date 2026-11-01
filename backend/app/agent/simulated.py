"""A simulated model for load tests (M6.5): free, and timed like the real one.

Its timings are the medians measured on live calls (6.2's report): a call
that decides to use a tool takes ~1.6 s, an answering call ~1.1 s. Two of
every three caller turns look something up (list_accounts), like real calls.
Its replies are fixed and state no account facts. It refuses to exist unless
COLLECTWISE_LOAD_TEST=1, so it can't stand in for the real agent by mistake.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.app import config

DECIDE_S = 1.6
ANSWER_S = 1.1
REPLY_WITH_LOOKUP = "I've checked your accounts. Is there anything else I can help with?"
REPLY_WITHOUT_LOOKUP = "I can help with balances, payment requests and their status. What would you like to do?"


class LoadTestOnly(RuntimeError):
    """A simulated service was asked for outside a load test."""


def require_load_test() -> None:
    if not config.LOAD_TEST:
        raise LoadTestOnly("Simulated services need COLLECTWISE_LOAD_TEST=1")


def looks_up(caller_turn: int) -> bool:
    """Whether the simulated agent uses a tool on this caller turn (1-based)."""
    return caller_turn % 3 != 0


@dataclass
class _Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class _Response:
    stop_reason: str
    content: list[Any]


class SimulatedLLM:
    def __init__(self, speed: float = 1.0) -> None:
        require_load_test()
        if speed <= 0:
            raise ValueError("speed must be positive")
        self._speed = speed

    def _wait(self, seconds: float) -> None:
        time.sleep(seconds / self._speed)

    def create(self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]],
               on_text: Callable[[str], None] | None = None) -> _Response:
        last = messages[-1]
        caller_turns = sum(1 for m in messages if m["role"] == "user" and isinstance(m["content"], str))
        if isinstance(last["content"], str) and looks_up(caller_turns):
            self._wait(DECIDE_S)
            return _Response("tool_use", [_Block("tool_use", id=f"sim_{caller_turns}", name="list_accounts")])
        self._wait(ANSWER_S)
        reply = REPLY_WITHOUT_LOOKUP if isinstance(last["content"], str) else REPLY_WITH_LOOKUP
        if on_text is not None:
            for word in reply.split(" "):
                on_text(word + " ")
        return _Response("end_turn", [_Block("text", text=reply)])
