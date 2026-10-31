import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: Any
    type: str = "tool_use"


@dataclass(frozen=True)
class FakeResponse:
    stop_reason: str
    content: list[Any]


def say(text: str) -> FakeResponse:
    return FakeResponse(stop_reason="end_turn", content=[TextBlock(text)])


def use(*calls: tuple[str, str, Any], text: str | None = None) -> FakeResponse:
    """A tool_use response; each call is (tool_use_id, tool_name, input)."""
    content: list[Any] = [TextBlock(text)] if text else []
    content += [ToolUseBlock(id=i, name=n, input=a) for i, n, a in calls]
    return FakeResponse(stop_reason="tool_use", content=content)


@dataclass
class FakeLLM:
    """Replays a script of responses (or raises scripted exceptions) and records
    a snapshot of every request, as the model would have received it."""

    script: list[Any]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def create(self, *, system: str, tools: list[dict], messages: list[dict], **streaming) -> FakeResponse:
        self.requests.append(
            {"system": system, "tools": tools, "messages": copy.deepcopy(messages), "streamed": bool(streaming)}
        )
        if not self.script:
            raise AssertionError("FakeLLM script exhausted: the loop made an unexpected call")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if hasattr(step, "create"):
            # A scripted model of its own for this call (e.g. one that waits).
            return step.create(system=system, tools=tools, messages=messages, **streaming)
        if on_text := streaming.get("on_text"):
            # Like the real stream: each text block in small pieces.
            for block in step.content:
                if isinstance(block, TextBlock):
                    for start in range(0, len(block.text), 4):
                        on_text(block.text[start:start + 4])
        return step
