"""Tracing (M6.2): off by default, the right spans when on, and never a
caller's words, the model's reply or tool data in any span, including the
ones FastAPI and the Anthropic SDK make."""

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from backend.app import config, telemetry
from backend.app.agent import claude
from backend.app.agent.llm import LLMUnavailableError
from backend.app.agent.loop import run_turn
from backend.app.agent.sessions import SessionStore
from backend.app.api.providers import get_llm
from backend.app.main import app
from backend.app.voice.live_call import ReplyDone
from backend.tests.fake_llm import FakeLLM, say, use
from backend.tests.helpers import seed_customer
from backend.tests.test_agent_api import agent_client, auth, llm  # noqa: F401  (fixtures)
from backend.tests.test_failure_drills import PROPOSE
from backend.tests.test_live_call import Call, db_factory, run  # noqa: F401  (db_factory is a fixture)
from scripts.live_report import summarize

MARKER = "PRIVATE-WORDS-MARKER"
SYSTEM = "test system prompt"


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = telemetry.install(provider)
    yield exporter
    telemetry.install(previous)


@pytest.fixture
def session(db_session):
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    return SessionStore().create("cust_a")


def named(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def attrs(span) -> dict:
    return {key.removeprefix("collectwise."): value for key, value in span.attributes.items()}


def everything_recorded(exporter) -> str:
    """Every name, attribute, event and status text of every span, as one string."""
    parts = []
    for span in exporter.get_finished_spans():
        parts += [span.name, json.dumps(dict(span.attributes), default=str), str(span.status.description)]
        parts += [json.dumps(dict(span.resource.attributes), default=str)]
        for event in span.events:
            parts += [event.name, json.dumps(dict(event.attributes or {}), default=str)]
    return "\n".join(parts)


# --- the switch -------------------------------------------------------------------


def test_tracing_is_off_unless_switched_on(session, db_session, caplog) -> None:
    assert config.TRACING is None
    assert telemetry.configure(None) is None
    with caplog.at_level(logging.WARNING):
        assert telemetry.configure("jaeger") is None  # not a mode: stays off, says so
    assert "tracing stays off" in caplog.text

    run_turn(session, "hi", llm=FakeLLM([say("Hello!")]), db=db_session, system=SYSTEM)

    assert not telemetry.tracer().start_span("anything").is_recording()


@pytest.mark.parametrize(
    ("mode", "processor", "exporter"),
    [("console", SimpleSpanProcessor, ConsoleSpanExporter), ("otlp", BatchSpanProcessor, OTLPSpanExporter)],
)
def test_each_mode_sends_spans_its_own_way(mode, processor, exporter) -> None:
    provider = telemetry.configure(mode)
    try:
        [installed] = provider._active_span_processor._span_processors
        assert isinstance(installed, processor)
        assert isinstance(installed.span_exporter, exporter)
        assert provider.resource.attributes["service.name"] == "collectwise"
        assert telemetry.tracer().start_span("x").is_recording()
    finally:
        telemetry.shutdown()
    assert not telemetry.tracer().start_span("x").is_recording()


def test_fastapi_traces_requests_but_keeps_no_logs_or_metrics() -> None:
    settings = app._telemetry

    assert settings["tracer_provider"] is telemetry.PROVIDER
    assert (settings["tracing"], settings["metrics"], settings["logs"]) == (True, False, False)
    assert (settings["operation_spans"], settings["auto_configure"]) == (False, False)


# --- the spans ---------------------------------------------------------------------


def test_a_turn_with_a_tool_is_one_tree(spans, session, db_session) -> None:
    llm = FakeLLM([use(("t1", "list_accounts", {}), text="One moment. "), say("You have one account.")])

    run_turn(session, "my accounts?", llm=llm, db=db_session, system=SYSTEM, on_text=lambda _: None)

    [turn] = named(spans, "agent.turn")
    calls = named(spans, "agent.model_call")
    [tool] = named(spans, "agent.tool")
    assert attrs(turn) == {"session_id": session.session_id, "turn": 1, "outcome": "reply",
                           "model_calls": 2, "tool_calls": 1}
    assert [attrs(c)["stop_reason"] for c in calls] == ["tool_use", "end_turn"]
    assert all(attrs(c)["streamed"] for c in calls)
    assert [e.name for e in calls[0].events] == ["first_text"]
    assert attrs(tool) == {"tool": "list_accounts", "ok": True}
    for child in [*calls, tool]:
        assert child.parent.span_id == turn.context.span_id


def test_the_typed_route_puts_the_turn_under_its_request(spans, agent_client) -> None:
    llm = FakeLLM([say("Hello!")])
    app.dependency_overrides[get_llm] = lambda: llm
    session_id = agent_client.post("/agent/sessions", headers=auth("cust_a")).json()["session_id"]

    agent_client.post(f"/agent/sessions/{session_id}/turns", json={"text": "hi"}, headers=auth("cust_a"))
    agent_client.get("/health")

    [turn] = named(spans, "agent.turn")
    request = next(s for s in spans.get_finished_spans() if s.context.span_id == turn.parent.span_id)
    assert request.attributes["http.route"] == "/agent/sessions/{session_id}/turns"
    assert not [s for s in spans.get_finished_spans() if "/health" in json.dumps(dict(s.attributes))]


def test_a_live_turn_holds_the_agent_turn_across_the_worker_thread(spans, db_factory) -> None:
    call = Call(db_factory, FakeLLM([say("Your balance is 500 US dollars.")]))

    async def go():
        call.start()
        call.listener.say_turn("What's my balance?", confidence=0.9)
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    [live] = named(spans, "live.turn")
    [turn] = named(spans, "agent.turn")
    assert turn.parent.span_id == live.context.span_id
    values = attrs(live)
    assert (values["turn"], values["outcome"], values["audio_ok"]) == (1, "reply", True)
    assert values["confidence_mean"] == pytest.approx(0.9)
    assert values["first_text_ms"] >= 0 and values["first_audio_ms"] >= values["first_text_ms"]
    assert "first_audio" in [e.name for e in live.events]


def test_failures_are_error_spans_with_our_codes(spans, session, db_session) -> None:
    run_turn(session, "Pay 120", llm=FakeLLM([use(PROPOSE), LLMUnavailableError("timeout")]),
             db=db_session, system=SYSTEM)

    [turn] = named(spans, "agent.turn")
    failed = named(spans, "agent.model_call")[-1]
    assert turn.status.status_code == failed.status.status_code == StatusCode.ERROR
    assert attrs(failed)["error"] == "unavailable"
    assert attrs(turn)["outcome"] == "unavailable"
    assert [(e.name, dict(e.attributes)) for e in turn.events] == [
        ("payment_cancelled", {"collectwise.reason": "not_read_back"})]


def test_a_tool_that_breaks_is_an_error_span(spans, session, db_session, monkeypatch) -> None:
    from backend.app.tools import accounts

    def broken(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(accounts, "list_customer_accounts", broken)
    run_turn(session, "hi", llm=FakeLLM([use(("t1", "list_accounts", {})), say("Sorry.")]),
             db=db_session, system=SYSTEM)

    [tool] = named(spans, "agent.tool")
    assert tool.status.status_code == StatusCode.ERROR
    assert attrs(tool) == {"tool": "list_accounts", "ok": False, "error": "internal_error"}


def test_an_exception_keeps_its_type_and_never_its_message(spans, session, db_session) -> None:
    class Broken:
        def create(self, **kwargs):
            raise ValueError(f"caller said {MARKER}")

    with pytest.raises(ValueError):
        run_turn(session, "hi", llm=Broken(), db=db_session, system=SYSTEM)

    [turn] = named(spans, "agent.turn")
    assert turn.status.status_code == StatusCode.ERROR
    assert turn.status.description is None
    assert [dict(e.attributes) for e in turn.events if e.name == "exception"] == [{"exception.type": "ValueError"}]
    assert MARKER not in everything_recorded(spans)


def test_a_status_description_is_never_kept(spans) -> None:
    with telemetry.span("x") as current:
        current.set_status(Status(StatusCode.ERROR, f"caller said {MARKER}"))

    [span] = named(spans, "x")
    assert (span.status.status_code, span.status.description) == (StatusCode.ERROR, None)


# --- privacy, end to end, with the real Anthropic SDK ------------------------------------


class _FakeMessagesAPI:
    """A local stand-in for the Messages API: a tool call, then a reply."""

    def __init__(self) -> None:
        self.headers: list[dict] = []
        replies = [
            {"content": [{"type": "text", "text": f"Let me check {MARKER}."},
                         {"type": "tool_use", "id": "t1", "name": "list_accounts", "input": {}}],
             "stop_reason": "tool_use"},
            {"content": [{"type": "text", "text": f"You have one account {MARKER}."}], "stop_reason": "end_turn"},
        ]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                outer.headers.append(dict(self.headers))
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps({
                    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                    "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 7}, **replies.pop(0),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def test_no_span_holds_the_callers_words_the_reply_or_tool_data(spans, agent_client, monkeypatch) -> None:
    api = _FakeMessagesAPI()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", api.url)
    # The SDK's own switch for recording whole requests and replies: ignored.
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    client = claude.build_claude_client(model="claude-sonnet-5-5", effort="medium")
    app.dependency_overrides[get_llm] = lambda: client
    session_id = agent_client.post("/agent/sessions", headers=auth("cust_a")).json()["session_id"]

    response = agent_client.post(f"/agent/sessions/{session_id}/turns",
                                 json={"text": f"What accounts do I have? {MARKER}"}, headers=auth("cust_a"))
    api.server.shutdown()

    assert MARKER in response.json()["reply"]  # it was there to leak
    sdk_spans = [s for s in spans.get_finished_spans() if s.instrumentation_scope.name == "com.anthropic.sdk.python"]
    assert len(sdk_spans) == 2  # the SDK's own spans were recorded, and checked
    recorded = everything_recorded(spans)
    for private in (MARKER, "acct_a_1", "500.00", "test-key-not-real"):
        assert private not in recorded
    assert all("traceparent" not in {k.lower() for k in headers} for headers in api.headers)


# --- the report ------------------------------------------------------------------------


class _Event:
    def __init__(self, session_id, turn, event_type, outcome, latency_ms) -> None:
        self.session_id, self.turn, self.event_type = session_id, turn, event_type
        self.outcome, self.latency_ms = outcome, latency_ms


def test_the_report_gives_each_stage_p50_and_p95() -> None:
    from backend.tests.test_live_report import metric

    events = [
        _Event("s", 1, "model_call", "tool_use", 1500), _Event("s", 1, "tool_call", "ok", 10),
        _Event("s", 1, "model_call", "end_turn", 900), _Event("s", 2, "model_call", "end_turn", 1300),
        _Event("s", 3, "model_call", "unavailable", 15000),
    ]

    report = summarize([metric(1), metric(2), metric(3)], events)

    assert "model call that chose a tool           median   1500 ms, p95   1500 ms" in report
    assert "model call that answered               median   1100 ms, p95   1300 ms" in report
    assert "tool                                   median     10 ms" in report
    assert "model calls per turn                   median 1, max 2  (n=3)" in report
    assert "1 model call(s) failed (unavailable)" in report
    assert "p90" not in report
