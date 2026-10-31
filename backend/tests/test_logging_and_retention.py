"""Logs and retention (M6.3): nothing a caller said, no account data, no
database values or exception messages in any log line; old telemetry is
purged and business records never are. docs/DATA_HANDLING.md lists every
store with the test that proves it."""

import io
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from backend.app import config, telemetry
from backend.app.agent import claude
from backend.app.api.providers import get_llm
from backend.app.db import models
from backend.app.logging_setup import QUIET_LIBRARIES, configure_logging
from backend.app.main import app
from backend.app.schemas.domain import ServicingOperation, ServicingRequest
from backend.app.services import conversation_events
from backend.app.services.servicing import evaluate_servicing_request
from backend.app.tools import accounts
from backend.tests.helpers import seed_customer
from backend.tests.test_agent_api import agent_client, auth, llm  # noqa: F401  (fixtures)
from backend.tests.test_failure_drills import REPO, assert_named_tests_exist
from backend.tests.test_live_report import metric
from backend.tests.test_telemetry import _FakeMessagesAPI, spans  # noqa: F401  (spans is a fixture)
from scripts import purge_old_data
from scripts.purge_old_data import purge

MARKER = "PRIVATE-WORDS-MARKER"


@pytest.fixture
def logs():
    """Configure logging as the app does, writing to a string; restore after."""
    def configure(log_format: str = "text") -> io.StringIO:
        handler = configure_logging(log_format)
        handler.setStream(stream := io.StringIO())
        return stream

    yield configure
    configure_logging(config.LOG_FORMAT)


# --- what reaches a log line ---------------------------------------------------------


def test_a_database_error_names_the_statement_not_its_values() -> None:
    from backend.app.db.session import engine

    with pytest.raises(OperationalError) as caught, engine.connect() as connection:
        connection.execute(text("SELECT * FROM no_such_table WHERE note = :note"), {"note": MARKER})

    assert "no_such_table" in str(caught.value)
    assert MARKER not in str(caught.value)


def test_an_exception_is_logged_as_types_and_locations(logs) -> None:
    output = logs()
    try:
        try:
            raise KeyError(f"{MARKER} first")
        except KeyError as inner:
            raise RuntimeError(f"{MARKER} second") from inner
    except RuntimeError:
        logging.getLogger("backend.app.example").exception("Tool %s raised an unexpected error", "list_accounts")

    written = output.getvalue()
    assert "Tool list_accounts raised an unexpected error" in written
    assert "RuntimeError" in written and "caused by:" in written and "KeyError" in written
    assert "test_logging_and_retention.py:" in written  # where it happened
    assert MARKER not in written


def test_noisy_libraries_stay_quiet_whatever_their_settings(logs) -> None:
    # ANTHROPIC_LOG=debug sets the SDK's logger to DEBUG at import, where it
    # writes each request's options (the whole conversation); basicConfig
    # would also have added a plain root handler.
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.DEBUG)
    plain = logging.StreamHandler(io.StringIO())
    logging.getLogger().addHandler(plain)

    output = logs()
    logging.getLogger("anthropic._base_client").debug("Request options: %s", MARKER)

    assert all(logging.getLogger(name).level == logging.WARNING for name in QUIET_LIBRARIES)
    assert plain not in logging.getLogger().handlers
    assert MARKER not in output.getvalue()


def test_uvicorn_lines_lose_the_client_address(logs) -> None:
    output = logs()
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "203.0.113.9:51234", "POST",
                                             "/agent/sessions", "1.1", 201)
    logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]', "203.0.113.9:51235",
                                            "/agent/sessions/sess_1/live")

    written = output.getvalue()
    assert '"POST /agent/sessions HTTP/1.1" 201' in written
    assert '"WebSocket /agent/sessions/sess_1/live" [accepted]' in written
    assert "203.0.113.9" not in written


def test_json_lines_carry_the_trace_id(logs, spans) -> None:
    output = logs("json")
    with telemetry.span("agent.turn") as current:
        logging.getLogger("backend.app.example").warning("LLM unavailable in session %s", "sess_1")
    try:
        raise ValueError(MARKER)
    except ValueError:
        logging.getLogger("backend.app.example").exception("Could not record a live turn metric")

    first, second = (json.loads(line) for line in output.getvalue().splitlines())
    assert first["message"] == "LLM unavailable in session sess_1"
    assert (first["level"], first["logger"]) == ("WARNING", "backend.app.example")
    assert first["trace_id"] == format(current.get_span_context().trace_id, "032x")
    assert "trace_id" not in second  # outside any span
    assert second["exception"][0] == "ValueError"
    assert MARKER not in output.getvalue()


def test_no_log_line_holds_the_callers_words_or_account_data(logs, agent_client, monkeypatch) -> None:
    # A real turn through the Anthropic SDK (local fake API), its debug
    # logging asked for, a tool that breaks quoting account data, and an
    # event write that fails: everything that could leak, at once.
    api = _FakeMessagesAPI()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", api.url)
    logging.getLogger("anthropic").setLevel(logging.DEBUG)
    output = logs()

    def broken(*args, **kwargs):
        raise RuntimeError(f"{MARKER} acct_a_1 500.00")

    def unsaved(db, events):
        raise OperationalError("INSERT INTO conversation_events", {"note": MARKER}, Exception(MARKER))

    monkeypatch.setattr(accounts, "list_customer_accounts", broken)
    monkeypatch.setattr(conversation_events, "_save_events", unsaved)
    app.dependency_overrides[get_llm] = lambda: claude.build_claude_client(model="claude-sonnet-5-5", effort="m")
    session_id = agent_client.post("/agent/sessions", headers=auth("cust_a")).json()["session_id"]

    agent_client.post(f"/agent/sessions/{session_id}/turns", json={"text": f"My accounts? {MARKER}"},
                      headers=auth("cust_a"))
    api.server.shutdown()

    written = output.getvalue()
    assert "Tool list_accounts raised an unexpected error" in written  # the failures were logged
    assert "Could not record" in written
    assert "HTTP Request:" not in written  # the SDK's HTTP client (httpx2) logs each request at INFO
    for private in (MARKER, "acct_a_1", "500.00", "test-key-not-real"):
        assert private not in written


def test_the_app_sets_up_logging_when_it_loads() -> None:
    import os
    import subprocess
    import sys

    probe = ("import logging, backend.app.main; "
             "print(logging.getLogger('uvicorn.access').handlers[0].get_name(), "
             "logging.getLogger('anthropic').level)")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO,
                            env={**os.environ, "ANTHROPIC_LOG": "debug"}, timeout=60)

    assert result.stdout.split() == ["collectwise", str(logging.WARNING)]


# --- purging old telemetry -----------------------------------------------------------


NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def event(created_at: datetime) -> models.ConversationEventRecord:
    return models.ConversationEventRecord(session_id="sess_1", customer_id="cust_a", turn=1,
                                          event_type="turn_started", created_at=created_at)


def live(created_at: datetime) -> models.LiveTurnMetricRecord:
    row = metric(1)
    row.created_at = created_at
    return row


def counts(db) -> tuple[int, int]:
    return (len(db.scalars(select(models.ConversationEventRecord)).all()),
            len(db.scalars(select(models.LiveTurnMetricRecord)).all()))


def test_old_telemetry_is_purged_and_the_newest_kept(db_session) -> None:
    edge = NOW - timedelta(days=30)
    db_session.add_all([event(edge - timedelta(seconds=1)), event(edge), event(NOW),
                        live(edge - timedelta(days=1)), live(NOW)])
    db_session.commit()

    result = purge(db_session, days=30, now=NOW, apply=True)
    db_session.rollback()  # what was deleted was committed

    assert result == {"conversation_events": 1, "voice_turn_metrics": 0, "live_turn_metrics": 1}
    assert counts(db_session) == (2, 1)  # exactly 30 days old is kept


def test_a_dry_run_deletes_nothing(db_session) -> None:
    db_session.add_all([event(NOW - timedelta(days=90)), live(NOW - timedelta(days=90))])
    db_session.commit()

    assert purge(db_session, days=30, now=NOW, apply=False)["conversation_events"] == 1
    assert counts(db_session) == (1, 1)
    with pytest.raises(ValueError):
        purge(db_session, days=0, now=NOW, apply=True)


def test_the_purge_never_touches_servicing_records(db_session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    evaluate_servicing_request(db_session, ServicingRequest(
        request_id="pay_old", customer_id="cust_a", account_id="acct_a_1",
        operation=ServicingOperation.REQUEST_PAYMENT, amount=Decimal("10.00"),
    ))
    old = NOW - timedelta(days=3650)
    for table in (models.ServicingRequestRecord, models.ServicingAuditEvent):
        db_session.execute(update(table).values(created_at=old))
    db_session.commit()

    purge(db_session, days=30, now=NOW, apply=True)

    assert len(db_session.scalars(select(models.ServicingRequestRecord)).all()) == 1
    assert len(db_session.scalars(select(models.ServicingAuditEvent)).all()) == 3
    assert {t.__tablename__ for t in purge_old_data.PURGEABLE} == {
        "conversation_events", "voice_turn_metrics", "live_turn_metrics"}


def test_the_purge_command_counts_then_deletes(db_session, capsys) -> None:
    class Borrowed:
        def __enter__(self):
            return db_session

        def __exit__(self, *exc):
            return None

    db_session.add(event(datetime.now(timezone.utc) - timedelta(days=40)))
    db_session.commit()

    purge_old_data.main([], session_factory=Borrowed)
    dry = capsys.readouterr().out
    purge_old_data.main(["--yes"], session_factory=Borrowed)
    applied = capsys.readouterr().out

    assert "Would delete rows older than 30 days:" in dry and "Nothing deleted" in dry
    assert "conversation_events    1" in dry
    assert "Deleted rows older than 30 days:" in applied
    assert counts(db_session) == (0, 0)
    assert config.TELEMETRY_RETENTION_DAYS == 30


def test_every_data_handling_claim_names_a_test_that_exists() -> None:
    assert_named_tests_exist(REPO / "docs" / "DATA_HANDLING.md", at_least=15)
