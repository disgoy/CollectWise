import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.agent.llm import LLMUnavailableError
from backend.app.agent.loop import run_turn
from backend.app.agent.sessions import ConversationSession, SessionStore
from backend.app.db import models
from backend.app.services import conversation_events
from backend.app.tools.registry import TOOLS
from backend.tests.fake_llm import FakeLLM, TextBlock, say, use
from backend.tests.helpers import seed_customer

SYSTEM = "test system prompt"


@pytest.fixture
def session(db_session: Session) -> ConversationSession:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    return SessionStore().create("cust_a")


def turn(session: ConversationSession, db: Session, llm: Any, text: str = "hi"):
    return run_turn(session, text, llm=llm, db=db, system=SYSTEM)


def events(db: Session) -> list[models.ConversationEventRecord]:
    return list(
        db.scalars(
            select(models.ConversationEventRecord).order_by(models.ConversationEventRecord.event_id)
        )
    )


def summary(db: Session) -> list[tuple]:
    return [(e.turn, e.event_type, e.name, e.outcome) for e in events(db)]


def test_turn_events_in_order(session, db_session) -> None:
    llm = FakeLLM([use(("t1", "list_accounts", {})), say("You have one account.")])

    turn(session, db_session, llm)

    assert summary(db_session) == [
        (1, "turn_started", None, None),
        (1, "model_call", None, "tool_use"),
        (1, "tool_call", "list_accounts", "ok"),
        (1, "model_call", None, "end_turn"),
        (1, "turn_completed", None, "reply"),
    ]
    rows = events(db_session)
    assert {row.session_id for row in rows} == {session.session_id}
    assert {row.customer_id for row in rows} == {"cust_a"}


def test_latencies_are_recorded_and_non_negative(session, db_session) -> None:
    llm = FakeLLM([use(("t1", "list_accounts", {})), say("ok")])

    turn(session, db_session, llm)

    timed = [e for e in events(db_session) if e.event_type != "turn_started"]
    assert all(isinstance(e.latency_ms, int) and e.latency_ms >= 0 for e in timed)


def test_token_usage_is_recorded_as_counts(session, db_session) -> None:
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[TextBlock("hi")],
        usage=SimpleNamespace(
            input_tokens=4, cache_read_input_tokens=2280,
            cache_creation_input_tokens=92, output_tokens=30,
        ),
    )

    turn(session, db_session, FakeLLM([response]))

    [model_call] = [e for e in events(db_session) if e.event_type == "model_call"]
    assert json.loads(model_call.details) == {
        "input_tokens": 4,
        "cache_read_input_tokens": 2280,
        "cache_creation_input_tokens": 92,
        "output_tokens": 30,
    }


def test_denied_proposal_is_visible(session, db_session) -> None:
    llm = FakeLLM([
        use(("t1", "propose_payment", {"account_id": "acct_a_1", "amount": "900.00"})),
        say("That's more than your available funds."),
    ])

    turn(session, db_session, llm)

    assert (1, "tool_call", "propose_payment", "insufficient_funds") in summary(db_session)


def test_outage_is_recorded(session, db_session) -> None:
    turn(session, db_session, FakeLLM([LLMUnavailableError("timeout")]))

    assert summary(db_session) == [
        (1, "turn_started", None, None),
        (1, "model_call", None, "unavailable"),
        (1, "turn_completed", None, "unavailable"),
    ]


def test_turn_after_handoff_records_no_model_call(session, db_session) -> None:
    llm = FakeLLM([use(("t1", "handoff_to_human", {"reason": "caller_requested"})), say("Transferring.")])
    turn(session, db_session, llm)

    turn(session, db_session, llm, "hello?")

    assert summary(db_session)[-1] == (1, "turn_completed", None, "already_handed_off")
    assert [e.event_type for e in events(db_session)].count("model_call") == 2


def test_events_hold_no_message_text_or_tool_arguments(session, db_session) -> None:
    llm = FakeLLM([
        use(("t1", "propose_payment", {"account_id": "acct_a_1", "amount": "123.45"})),
        say("CANARY-REPLY-9d2e"),
    ])

    turn(session, db_session, llm, "CANARY-CALLER-7f3a please pay")

    dump = json.dumps(
        [
            {column.name: str(getattr(row, column.name)) for column in row.__table__.columns}
            for row in events(db_session)
        ]
    )
    for secret in ("CANARY-CALLER-7f3a", "CANARY-REPLY-9d2e", "123.45", "acct_a_1"):
        assert secret not in dump


def test_recording_failure_never_breaks_the_turn(
    session, db_session, monkeypatch, caplog
) -> None:
    def broken_save(db: Session, events: list) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(conversation_events, "_save_events", broken_save)

    with caplog.at_level(logging.ERROR):
        result = turn(session, db_session, FakeLLM([say("Hello!")]))

    assert result.reply == "Hello!"
    assert "Could not record" in caplog.text
    # The session is still usable afterwards.
    assert db_session.get(models.CustomerRecord, "cust_a") is not None


def test_tool_rollback_does_not_erase_events(session, db_session, monkeypatch) -> None:
    def broken_tool(ctx: Any, args: Any) -> Any:
        ctx.db.add(models.CustomerRecord(customer_id="cust_half", display_name="x"))
        ctx.db.flush()
        raise RuntimeError("boom")

    monkeypatch.setitem(TOOLS, "list_accounts", replace(TOOLS["list_accounts"], function=broken_tool))
    llm = FakeLLM([use(("t1", "list_accounts", {})), say("Sorry, something went wrong.")])

    turn(session, db_session, llm)

    assert (1, "tool_call", "list_accounts", "internal_error") in summary(db_session)
    assert summary(db_session)[0] == (1, "turn_started", None, None)
    assert db_session.get(models.CustomerRecord, "cust_half") is None


def test_events_are_saved_even_when_our_code_crashes(session, db_session) -> None:
    with pytest.raises(RuntimeError):
        turn(session, db_session, FakeLLM([RuntimeError("bug")]))

    assert summary(db_session)[0] == (1, "turn_started", None, None)


def test_unknown_event_type_is_rejected(db_session) -> None:
    db_session.add(
        models.ConversationEventRecord(
            session_id="sess_x", customer_id="cust_a", turn=0, event_type="caller_said",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
