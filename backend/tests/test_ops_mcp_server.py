"""The operations MCP server (M6.6): right numbers, never a customer id or
anything a caller said, can't write, works over the real protocol (in
process and over stdio), and stays away from the app and the agent."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from mcp import Client, StdioServerParameters
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.db import models
from backend.app.tools.registry import tool_schemas
from backend.ops import mcp_server, queries
from backend.tests.databases import make_test_engine
from backend.tests.test_failure_drills import REPO

MARKER = "cust_PRIVATE_MARKER"
SESSION_1 = "sess_aaaaaaaaaaaaaaaaaaaaaaaa11112222"
SESSION_2 = "sess_bbbbbbbbbbbbbbbbbbbbbbbb33334444"
NOW = datetime.now(timezone.utc)
TOOLS = {"service_health", "live_call_speed", "recent_calls", "conversation_outcomes", "eval_results"}


def turn(session, n, outcome="reply", *, first_text=1000, first_audio=1300, mean=0.95, audio_ok=True, ago=5):
    return models.LiveTurnMetricRecord(
        session_id=session, customer_id=MARKER, turn=n, outcome=outcome, confidence_mean=mean, confidence_min=mean,
        transcript_chars=20, reply_chars=40, sentences=1, first_text_ms=first_text, first_audio_ms=first_audio,
        agent_ms=first_audio, audio_ok=audio_ok, created_at=NOW - timedelta(minutes=ago))


def event(session, n, event_type, name=None, outcome=None, latency=None, ago=5):
    return models.ConversationEventRecord(session_id=session, customer_id=MARKER, turn=n, event_type=event_type,
                                          name=name, outcome=outcome, latency_ms=latency,
                                          created_at=NOW - timedelta(minutes=ago))


@pytest.fixture
def ops_db():
    """A seeded database, read by the server through a read-only engine."""
    engine = make_test_engine(concurrent=True)
    with sessionmaker(bind=engine)() as db:
        db.add_all([
            turn(SESSION_1, 1), turn(SESSION_1, 2, first_text=2000, first_audio=2500, audio_ok=False),
            turn(SESSION_2, 1, "not_understood", first_text=None, first_audio=None, mean=0.4),
            turn(SESSION_2, 2, first_text=1500, first_audio=1800),
            turn(SESSION_1, 9, ago=3 * 24 * 60),  # outside every window but the call list
            event(SESSION_1, 1, "model_call", outcome="tool_use", latency=1600),
            event(SESSION_1, 1, "tool_call", name="list_accounts", outcome="ok", latency=8),
            event(SESSION_1, 1, "model_call", outcome="end_turn", latency=1100),
            event(SESSION_1, 1, "turn_completed", outcome="reply"),
            event(SESSION_1, 2, "tool_call", name="propose_payment", outcome="internal_error", latency=5),
            event(SESSION_1, 2, "model_call", outcome="unavailable", latency=15000),
            event(SESSION_1, 2, "turn_completed", outcome="unavailable"),
            event(SESSION_1, 2, "payment_cancelled", outcome="not_read_back"),
            event(SESSION_2, 2, "tool_call", name="handoff_to_human", outcome="ok", latency=1),
            event(SESSION_2, 2, "payment_cancelled", outcome="call_ended"),
            event(SESSION_2, 3, "turn_completed", outcome="turn_limit"),
            event(SESSION_1, 9, "model_call", outcome="end_turn", latency=9999, ago=3 * 24 * 60),
            # A typed conversation: in the window, but not a live call.
            event("sess_typed_cccccccccccccccccccc5555", 1, "tool_call", name="list_accounts", outcome="ok", latency=50),
        ])
        db.commit()
    read_only = mcp_server.read_only_engine(engine.url.render_as_string(hide_password=False))
    mcp_server.use_engine(read_only)
    yield read_only
    mcp_server.use_engine(None)
    read_only.dispose()
    engine.dispose()


def call(name, arguments=None):
    async def go():
        async with Client(mcp_server.server) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(go())


# --- the numbers ---------------------------------------------------------------------


def test_live_call_speed_over_a_window(ops_db) -> None:
    result = call("live_call_speed", {"minutes": 60}).structured_content

    assert (result["calls"], result["turns"]) == (2, 4)
    assert result["outcomes"] == [{"outcome": "reply", "count": 3}, {"outcome": "not_understood", "count": 1}]
    assert result["first_audio_ms"] == {"p50": 1800, "p95": 2500, "max": 2500, "n": 3}
    assert result["speech_ms"]["p50"] == 300
    assert result["model_call_choosing_a_tool_ms"]["p50"] == 1600
    assert result["model_call_answering_ms"] == {"p50": 1100, "p95": 1100, "max": 1100, "n": 1}  # not the old 9999
    assert result["tool_ms"]["n"] == 3  # the tool calls of these four live turns, not the typed one
    assert result["word_confidence"]["turns_below_threshold"] == 1


def test_recent_calls_newest_first_with_short_references(ops_db) -> None:
    calls = call("recent_calls", {"limit": 5}).structured_content["result"]

    assert [c["call"] for c in calls] == ["11112222", "33334444"]  # session 1's last turn came last
    first = calls[0]
    assert (first["turns"], first["text_only_replies"], first["first_audio_ms_median"]) == (3, 1, 1300)
    assert len(call("recent_calls", {"limit": 1}).structured_content["result"]) == 1


def test_conversation_outcomes_over_a_window(ops_db) -> None:
    result = call("conversation_outcomes", {"minutes": 60}).structured_content

    assert {o["outcome"]: o["count"] for o in result["turn_outcomes"]} == {
        "reply": 1, "unavailable": 1, "turn_limit": 1}
    assert (result["model_calls"], result["model_calls_failed"]) == (3, 1)
    assert result["tool_errors"] == [{"tool": "propose_payment", "error": "internal_error", "count": 1}]
    assert {p["cause"] for p in result["payments_cancelled"]} == {"not_read_back", "call_ended"}
    assert result["handoffs"] == 2  # one by the tool, one by the turn limit


def test_service_health_and_eval_results(ops_db, monkeypatch, tmp_path) -> None:
    health = call("service_health").structured_content
    assert health == {"database": "reachable", "schema_revision": health["latest_revision"],
                      "latest_revision": health["latest_revision"], "schema_current": True}

    from scripts import build_evidence

    monkeypatch.setattr(build_evidence, "EVAL_DIR", tmp_path / "none")
    assert call("eval_results").structured_content == {
        "versions": [], "grader_checks": {}, "note": "No eval results on this machine."}


def test_service_health_reports_a_schema_behind_the_code(tmp_path) -> None:
    from alembic import command

    from backend.app.db.session import alembic_config

    url = f"sqlite:///{tmp_path}/old.db"
    command.upgrade(alembic_config(url), "0001")
    engine = mcp_server.read_only_engine(url)
    mcp_server.use_engine(engine)
    try:
        health = call("service_health").structured_content
    finally:
        mcp_server.use_engine(None)
        engine.dispose()

    assert (health["schema_revision"], health["schema_current"]) == ("0001", False)


# --- privacy and safety ---------------------------------------------------------------------


def test_no_result_names_a_customer_or_a_whole_session_id(ops_db) -> None:
    everything = json.dumps([call(name).structured_content for name in TOOLS])

    for private in (MARKER, SESSION_1, SESSION_2):
        assert private not in everything


def test_a_result_that_could_hold_words_is_refused(ops_db, monkeypatch) -> None:
    monkeypatch.setattr(queries, "service_health", lambda db: {"transcript": "pay 120 from savings"})

    result = call("service_health")

    assert result.is_error
    assert "pay 120" not in json.dumps(result.model_dump())


def test_the_server_cannot_write(ops_db) -> None:
    with pytest.raises(DBAPIError), sessionmaker(bind=ops_db)() as db:
        db.add(models.CustomerRecord(customer_id="cust_new", display_name="Never"))
        db.commit()
    with pytest.raises(ValueError, match="in-memory"):
        mcp_server.read_only_engine("sqlite://")


# --- over the protocol -----------------------------------------------------------------------


def test_every_tool_is_read_only_and_bad_arguments_are_refused(ops_db) -> None:
    async def go():
        async with Client(mcp_server.server) as client:
            return (await client.list_tools()).tools, await client.call_tool("live_call_speed", {"minutes": 0})

    tools, refused = asyncio.run(go())

    assert {t.name for t in tools} == TOOLS
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)
    assert refused.is_error
    assert call("live_call_speed", {"minutes": 7 * 24 * 60 + 1}).is_error
    assert not call("live_call_speed", {"minutes": 7 * 24 * 60}).is_error
    assert call("recent_calls", {"limit": 101}).is_error


def test_the_docs_are_resources(ops_db) -> None:
    async def go():
        async with Client(mcp_server.server) as client:
            listed = (await client.list_resources()).resources
            text = (await client.read_resource("collectwise://docs/failure-modes")).contents[0].text
            return listed, text

    listed, text = asyncio.run(go())

    assert {str(r.uri) for r in listed} == {"collectwise://docs/failure-modes", "collectwise://docs/data-handling",
                                            "collectwise://docs/load-test"}
    assert text.startswith("# Failure modes")


def test_it_runs_over_stdio_as_its_own_process(ops_db) -> None:
    url = ops_db.url.render_as_string(hide_password=False)
    if ops_db.url.get_backend_name() == "sqlite":  # the server opens the file read-only itself
        url = "sqlite:///" + ops_db.url.database.removeprefix("file:")
    params = StdioServerParameters(command=sys.executable, args=["-m", "backend.ops.mcp_server"], cwd=str(REPO),
                                   env={**os.environ, "COLLECTWISE_DATABASE_URL": url})

    async def go():
        async with Client(params) as client:
            tools = (await client.list_tools()).tools
            return tools, (await client.call_tool("live_call_speed", {"minutes": 60})).structured_content

    tools, speed = asyncio.run(go())

    assert {t.name for t in tools} == TOOLS
    assert speed["turns"] == 4


# --- away from the app and the agent ------------------------------------------------------------


def test_the_agent_never_gets_these_tools_and_the_app_never_loads_them() -> None:
    assert TOOLS.isdisjoint(schema["name"] for schema in tool_schemas())
    probe = "import sys, backend.app.main; print(any(m == 'backend.ops' or m.startswith('backend.ops.') for m in sys.modules))"
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO, timeout=60)

    assert loaded.stdout.strip() == "False"
