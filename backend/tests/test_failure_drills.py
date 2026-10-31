"""Failure drills (M6.1): each test breaks one thing and checks what the
caller hears, what is recorded and what state is left. docs/FAILURE_MODES.md
lists every failure with the test that proves it, including the ones covered
elsewhere."""

import asyncio
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from backend.app import config
from backend.app.agent import claude
from backend.app.agent.llm import LLMUnavailableError
from backend.app.agent.loop import REPLY_UNAVAILABLE, run_turn
from backend.app.agent.sessions import SessionStore
from backend.app.db import models
from backend.app.tools import payments
from backend.app.voice import live_call
from backend.app.voice.live_call import REPLY_TIME_LIMIT, AgentSentence, CallEnded, ReplyDone
from backend.tests.fake_live_speech import FakeLiveTTS, FakeSpeaker
from backend.tests.fake_llm import FakeLLM, say, use
from backend.tests.test_agent_api import agent_client, auth, llm  # noqa: F401  (fixtures)
from backend.tests.helpers import seed_customer
from backend.tests.test_live_call import (  # noqa: F401  (db_factory is a fixture)
    PROPOSE,
    READ_BACK,
    Call,
    GatedLLM,
    ManualClock,
    db_factory,
    payment_records,
    run,
)

SYSTEM = "test system prompt"
CONFIRM = ("t2", "confirm_payment", {})
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def session(db_session):
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    return SessionStore().create("cust_a")


def turn(session, db, script, text="hi", **kwargs):
    return run_turn(session, text, llm=FakeLLM(script), db=db, system=SYSTEM, **kwargs)


def cancelled_at_call_end(db_factory) -> list[models.ConversationEventRecord]:
    with db_factory() as db:
        return list(db.scalars(select(models.ConversationEventRecord).where(
            models.ConversationEventRecord.event_type == "payment_cancelled")))


# --- the model ---------------------------------------------------------------------


class _StalledServer:
    """Accepts connections and never answers, like an API that hangs."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.connections: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections.append(conn)

    def close(self) -> None:
        for conn in self.connections:
            conn.close()
        self.sock.close()


@pytest.mark.parametrize("streamed", [False, True])
def test_a_model_that_hangs_gives_up_after_the_timeout(monkeypatch, streamed) -> None:
    server = _StalledServer()
    monkeypatch.setattr(config, "AGENT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(config, "AGENT_MAX_RETRIES", 0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.port}")
    client = claude.build_claude_client(model="claude-sonnet-5-5", effort="medium")
    outcome: list[BaseException | None] = []

    def call() -> None:
        stream = {"on_text": lambda _: None} if streamed else {}
        try:
            client.create(system="s", tools=[], messages=[{"role": "user", "content": "hi"}], **stream)
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            outcome.append(exc)

    worker = threading.Thread(target=call, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(5)
    server.close()

    assert not worker.is_alive(), "still waiting on a model that never answers"
    assert time.monotonic() - started < 3
    assert isinstance(outcome[0], LLMUnavailableError)


def test_the_shipped_model_timeout_and_retries(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    client = claude.build_claude_client(model="claude-sonnet-5-5", effort="medium")

    assert (config.AGENT_TIMEOUT_S, config.AGENT_MAX_RETRIES) == (15.0, 1)
    assert client._client.timeout.read == config.AGENT_TIMEOUT_S
    assert client._client.timeout.connect == claude.CONNECT_TIMEOUT_S
    assert client._client.max_retries == config.AGENT_MAX_RETRIES


def test_a_model_failure_after_a_proposal_drops_the_unheard_payment(session, db_session) -> None:
    first = turn(session, db_session, [use(PROPOSE), LLMUnavailableError("timeout")], "Pay 120")

    assert first.reply == REPLY_UNAVAILABLE  # the caller never heard a read-back
    assert session.state.pending_payment is None

    second = turn(session, db_session, [use(CONFIRM), say("There's nothing to confirm yet.")], "Yes")

    assert second.tool_calls[0].error == "no_pending_payment"
    assert payment_records_in(db_session) == 0
    [event] = db_session.scalars(select(models.ConversationEventRecord).where(
        models.ConversationEventRecord.event_type == "payment_cancelled")).all()
    assert (event.outcome, event.turn) == ("not_read_back", 1)


def test_a_model_failure_leaves_an_earlier_read_back_alone(session, db_session) -> None:
    # Proposed and read back in turn 1; turn 2 is about something else and
    # fails: the caller did hear the read-back, so it can still be confirmed.
    turn(session, db_session, [use(PROPOSE), say(READ_BACK)], "Pay 120")
    turn(session, db_session, [LLMUnavailableError("timeout")], "What's my balance?")

    assert session.state.pending_payment is not None
    third = turn(session, db_session, [use(CONFIRM), say("Recorded.")], "Yes, pay it")

    assert third.tool_calls[0].ok
    assert payment_records_in(db_session) == 1


def payment_records_in(db) -> int:
    return len(list(db.scalars(select(models.ServicingRequestRecord))))


# --- the database --------------------------------------------------------------------


def test_a_database_failure_while_confirming_records_nothing_and_can_be_retried(
    session, db_session, monkeypatch,
) -> None:
    turn(session, db_session, [use(PROPOSE), say(READ_BACK)], "Pay 120")
    real = payments.evaluate_servicing_request

    def down(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    monkeypatch.setattr(payments, "evaluate_servicing_request", down)
    failed = turn(session, db_session, [use(CONFIRM), say("Sorry, that didn't go through.")], "Yes")

    assert failed.tool_calls[0].error == "internal_error"
    assert payment_records_in(db_session) == 0
    assert not session.state.pending_payment.submitted  # still waiting, not lost

    monkeypatch.setattr(payments, "evaluate_servicing_request", real)
    retried = turn(session, db_session, [use(("t3", "confirm_payment", {})), say("Recorded.")], "Try again")

    assert retried.tool_calls[0].ok
    assert payment_records_in(db_session) == 1


# --- the voice ---------------------------------------------------------------------


class _StalledSpeaker(FakeSpeaker):
    """Takes every sentence and never produces audio."""

    def _deliver(self, sentences: list[int]) -> None:
        return None


def test_a_voice_that_stalls_lets_the_call_carry_on_in_text(db_factory, monkeypatch) -> None:
    monkeypatch.setattr(live_call, "REPLY_AUDIO_TIMEOUT_S", 0.2)
    llm = FakeLLM([say("Your balance is 500 US dollars."), say("Anything else?")])
    call = Call(db_factory, llm, tts=FakeLiveTTS(speaker=_StalledSpeaker()))

    async def go():
        call.start()
        call.listener.say_turn("What's my balance?")
        await call.link.wait_for(ReplyDone)
        call.listener.say_turn("Thanks")
        await call.link.wait_for(ReplyDone, count=2)
        return await call.end()

    assert run(go()) == "caller_hung_up"
    assert [e.text for e in call.link.of(AgentSentence)] == ["Your balance is 500 US dollars.", "Anything else?"]
    assert call.session.state.turn == 2


# --- the caller's connection -----------------------------------------------------------


def test_hanging_up_while_the_agent_works_lets_the_turn_finish_first(db_factory) -> None:
    gated = GatedLLM("", "You have one account.")
    call = Call(db_factory, FakeLLM([use(("t1", "list_accounts", {})), gated]))

    async def go():
        call.start()
        call.listener.say_turn("Which accounts do I have?")
        await asyncio.sleep(0.05)  # the agent is mid-turn
        call.link.hang_up()
        await asyncio.sleep(0.05)
        still_open = not call.task.done()
        gated.release.set()
        return still_open, await call.task

    still_open, reason = run(go())

    assert still_open  # the turn is never abandoned halfway
    assert reason == "caller_hung_up"
    assert call.session.messages[-1]["role"] == "assistant"  # history complete
    assert call.link.events[-1] == CallEnded("caller_hung_up")


def test_a_call_that_ends_after_a_read_back_cancels_the_unconfirmed_payment(db_factory) -> None:
    # A hang-up message, a closed tab and a lost network all end the link's
    # incoming() (api/live.py), so the engine sees each as the caller leaving.
    llm = FakeLLM([use(PROPOSE), say(READ_BACK)])
    call = Call(db_factory, llm)

    async def go():
        call.start()
        call.listener.say_turn("Pay 120 from my account")
        await call.link.wait_for(ReplyDone)
        return await call.end()

    assert run(go()) == "caller_hung_up"
    assert call.session.state.pending_payment is None
    assert payment_records(db_factory) == 0
    [event] = cancelled_at_call_end(db_factory)
    assert (event.outcome, event.turn) == ("call_ended", 1)


def test_a_payment_confirmed_on_the_call_survives_its_end(db_factory) -> None:
    llm = FakeLLM([use(PROPOSE), say(READ_BACK), use(CONFIRM), say("Recorded.")])
    call = Call(db_factory, llm)

    async def go():
        call.start()
        call.listener.say_turn("Pay 120 from my account")
        await call.link.wait_for(ReplyDone)
        call.link.play_everything()
        await asyncio.sleep(0.05)
        call.listener.say_turn("Yes, go ahead")
        await call.link.wait_for(ReplyDone, count=2)
        return await call.end()

    run(go())

    assert payment_records(db_factory) == 1
    assert call.session.state.pending_payment.submitted
    assert cancelled_at_call_end(db_factory) == []


def test_a_call_that_ends_with_nothing_pending_records_no_cancellation(db_factory) -> None:
    call = Call(db_factory, FakeLLM([say("Hello!")]))

    async def go():
        call.start()
        call.listener.say_turn("Hi")
        await call.link.wait_for(ReplyDone)
        return await call.end()

    run(go())

    assert cancelled_at_call_end(db_factory) == []


def test_the_time_limit_waits_for_a_running_turn(db_factory, monkeypatch) -> None:
    monkeypatch.setattr(live_call, "WATCHDOG_TICK_S", 0.01)
    clock = ManualClock()
    gated = GatedLLM("", "Your balance is 500 US dollars.")
    call = Call(db_factory, FakeLLM([gated]), clock=clock)

    async def go():
        call.start()
        call.listener.say_turn("What's my balance?")
        await asyncio.sleep(0.03)
        clock.now = 15 * 60  # the limit arrives mid-turn
        await asyncio.sleep(0.05)
        gated.release.set()
        return await call.task

    assert run(go()) == "time_limit"
    assert call.speaker.said == ["Your balance is 500 US dollars.", REPLY_TIME_LIMIT]


# --- two typed messages at once ------------------------------------------------------------


class _BlockingLLM:
    """Answers only when released, like a slow model call."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def create(self, *, system, tools, messages, **streaming):
        assert self.release.wait(5)
        return say("Hello!")


def test_a_second_typed_message_during_a_turn_is_refused_after_a_short_wait(db_factory) -> None:
    from backend.app.agent.loop import SessionBusyError

    session = SessionStore().create("cust_a")
    slow = _BlockingLLM()

    def first_turn() -> None:
        with db_factory() as db:
            run_turn(session, "Hi", llm=slow, db=db, system=SYSTEM)

    worker = threading.Thread(target=first_turn, daemon=True)
    worker.start()
    time.sleep(0.05)  # the first turn holds the conversation

    started = time.monotonic()
    with db_factory() as db, pytest.raises(SessionBusyError):
        run_turn(session, "Hello?", llm=FakeLLM([]), db=db, system=SYSTEM, lock_timeout=0.1)
    waited = time.monotonic() - started

    slow.release.set()
    worker.join(5)
    assert 0.1 <= waited < 1
    assert session.state.turn == 1  # the refused message never counted as a turn
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


def test_the_typed_route_answers_409_while_a_turn_is_running(agent_client, monkeypatch) -> None:
    from backend.app.agent.loop import SessionBusyError
    from backend.app.api import agent as agent_routes

    seen = {}

    def busy(*args, **kwargs):
        seen["lock_timeout"] = kwargs.get("lock_timeout")
        raise SessionBusyError()

    session_id = agent_client.post("/agent/sessions", headers=auth("cust_a")).json()["session_id"]
    monkeypatch.setattr(agent_routes, "run_turn", busy)
    response = agent_client.post(f"/agent/sessions/{session_id}/turns", json={"text": "hi"}, headers=auth("cust_a"))

    assert response.status_code == 409
    assert response.json() == {"detail": "Still answering your last message"}
    assert seen["lock_timeout"] == config.TURN_LOCK_WAIT_S == 5.0


# --- the failure table stays honest -------------------------------------------------------


def test_every_failure_mode_names_a_test_that_exists() -> None:
    table = (REPO / "docs" / "FAILURE_MODES.md").read_text()
    python_tests = set(re.findall(r"`(test_[a-z0-9_]+)`", table))
    node_tests = set(re.findall(r'node: "([^"]+)"', table))
    tests = REPO / "backend" / "tests"
    defined_python = set()
    for path in tests.rglob("test_*.py"):
        defined_python |= set(re.findall(r"^def (test_[a-z0-9_]+)\(", path.read_text(), re.MULTILINE))
    defined_node = set()
    for path in (tests / "js").glob("*.test.mjs"):
        defined_node |= set(re.findall(r'^test\("([^"]+)"', path.read_text(), re.MULTILINE))

    assert len(python_tests) >= 30 and len(node_tests) >= 2
    assert sorted(python_tests - defined_python) == []
    assert sorted(node_tests - defined_node) == []
