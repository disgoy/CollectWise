"""Load testing (M6.5): the simulated model and speech exist only in a load
test, behave like the real services' measured timings, and the load script's
numbers are right. Ends with a small end-to-end run through a real server."""

import asyncio
import json
import logging
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import config
from backend.app.agent import simulated
from backend.app.agent.loop import run_turn
from backend.app.agent.sessions import SessionStore
from backend.app.agent.simulated import LoadTestOnly, SimulatedLLM
from backend.app.api import providers
from backend.app.main import app
from backend.app.voice import simulated_live
from backend.app.voice.live_speech import SegmentDone, SpokenAudio
from backend.app.voice.simulated_live import SimulatedLiveSTT, SimulatedLiveTTS
from backend.tests.helpers import seed_customer
from scripts import load_test


@pytest.fixture
def load_test_mode(monkeypatch):
    monkeypatch.setattr(config, "LOAD_TEST", True)
    providers._simulated_llm.cache_clear()
    providers._simulated_live_speech.cache_clear()
    yield
    providers._simulated_llm.cache_clear()
    providers._simulated_live_speech.cache_clear()


# --- only in a load test ---------------------------------------------------------------


def test_the_simulations_refuse_outside_a_load_test() -> None:
    assert config.LOAD_TEST is False
    for make in (SimulatedLLM, SimulatedLiveSTT, SimulatedLiveTTS):
        with pytest.raises(LoadTestOnly):
            make()


def test_asking_for_them_outside_a_load_test_leaves_the_agent_and_speech_off(monkeypatch, caplog) -> None:
    monkeypatch.setattr(config, "AGENT_LLM", "simulated")
    monkeypatch.setattr(config, "SPEECH_PROVIDER", "simulated")
    providers._simulated_llm.cache_clear()
    providers._simulated_live_speech.cache_clear()

    with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as refused:
        providers.get_llm()

    assert refused.value.status_code == 503
    assert providers.get_live_stt() is None and providers.get_live_tts() is None
    assert "needs COLLECTWISE_LOAD_TEST=1" in caplog.text


def test_in_a_load_test_they_are_used_and_the_server_warns(load_test_mode, monkeypatch, caplog) -> None:
    monkeypatch.setattr(config, "AGENT_LLM", "simulated")
    monkeypatch.setattr(config, "SPEECH_PROVIDER", "simulated")

    assert isinstance(providers.get_llm(), SimulatedLLM)
    assert isinstance(providers.get_live_stt(), SimulatedLiveSTT)
    assert isinstance(providers.get_live_tts(), SimulatedLiveTTS)
    with caplog.at_level(logging.WARNING), TestClient(app):
        pass
    assert "Load-test mode is ON" in caplog.text


def test_the_switches_are_off_by_default() -> None:
    assert (config.AGENT_LLM, config.SPEECH_PROVIDER, config.SIMULATED_SPEED) == (None, None, 1.0)


# --- the simulated model ------------------------------------------------------------------


def test_the_simulated_model_looks_up_on_two_turns_in_three(load_test_mode, db_session) -> None:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    session = SessionStore().create("cust_a")
    llm = SimulatedLLM(speed=1000)

    results = [run_turn(session, "What's my balance?", llm=llm, db=db_session, system="s") for _ in range(3)]

    assert [[c.name for c in r.tool_calls] for r in results] == [["list_accounts"], ["list_accounts"], []]
    assert all(c.ok for r in results for c in r.tool_calls)
    assert results[0].reply == simulated.REPLY_WITH_LOOKUP
    assert results[2].reply == simulated.REPLY_WITHOUT_LOOKUP
    assert [simulated.looks_up(n) for n in (1, 2, 3, 4)] == [True, True, False, True]


def test_the_simulated_model_takes_the_measured_time_and_streams(load_test_mode) -> None:
    llm = SimulatedLLM(speed=10)
    pieces: list[str] = []

    started = time.monotonic()
    decided = llm.create(system="s", tools=[], messages=[{"role": "user", "content": "hi"}])
    decide_s = time.monotonic() - started
    started = time.monotonic()
    answered = llm.create(system="s", tools=[], on_text=pieces.append, messages=[
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": decided.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "sim_1", "content": "{}"}]}])
    answer_s = time.monotonic() - started

    assert decided.stop_reason == "tool_use" and decided.content[0].name == "list_accounts"
    assert answered.stop_reason == "end_turn"
    assert decide_s == pytest.approx(simulated.DECIDE_S / 10, abs=0.05)
    assert answer_s == pytest.approx(simulated.ANSWER_S / 10, abs=0.05)
    assert "".join(pieces).strip() == simulated.REPLY_WITH_LOOKUP
    with pytest.raises(ValueError):
        SimulatedLLM(speed=0)


# --- simulated speech -------------------------------------------------------------------------


def test_simulated_listening_ends_a_turn_after_the_set_audio(load_test_mode) -> None:
    async def go():
        async with SimulatedLiveSTT().open(sample_rate=16_000) as listener:
            second = bytes(16_000 * 2)
            for _ in range(int(simulated_live.TURN_AUDIO_S) * 2):  # two turns' worth
                await listener.send_audio(second)
            await listener.finish()
            return [event async for event in listener.events()]

    events = asyncio.run(go())

    assert [(e.kind, e.text) for e in events] == [
        ("speech_started", ""), ("end_of_turn", simulated_live.PHRASES[0]),
        ("speech_started", ""), ("end_of_turn", simulated_live.PHRASES[1])]
    assert events[1].word_confidences == (0.95, 0.95, 0.95)


def test_simulated_speaking_starts_after_the_first_audio_time(load_test_mode) -> None:
    async def go():
        async with SimulatedLiveTTS(speed=1).open() as speaker:
            started = time.monotonic()
            await speaker.say("Your balance is fine.")
            await speaker.say("Anything else?")
            items, first_audio = [], None
            async for item in speaker.output():
                if isinstance(item, SpokenAudio) and first_audio is None:
                    first_audio = time.monotonic() - started
                items.append(item)
                if isinstance(item, SegmentDone) and item.sentences == (1,):
                    break
            return items, first_audio

    items, first_audio = asyncio.run(go())

    assert first_audio == pytest.approx(simulated_live.FIRST_AUDIO_S, abs=0.05)
    assert [i.sentences for i in items if isinstance(i, SegmentDone)] == [(0,), (1,)]
    audio = sum(len(i.data) for i in items if isinstance(i, SpokenAudio) and i.segment == 0)
    expected = len("Your balance is fine.") * simulated_live.SECONDS_PER_CHAR * simulated_live.OUTPUT_RATE * 2
    assert audio == pytest.approx(expected, rel=0.05)


def test_each_reply_waits_for_its_own_first_audio(load_test_mode) -> None:
    async def first_audio_after(speaker, sentence: str) -> float:
        started = time.monotonic()
        await speaker.say(sentence)
        async for item in speaker.output():
            if isinstance(item, SpokenAudio):
                waited = time.monotonic() - started
            if isinstance(item, SegmentDone):
                return waited

    async def go():
        async with SimulatedLiveTTS(speed=1).open() as speaker:
            first = await first_audio_after(speaker, "One.")
            await speaker.end_reply()
            second = await first_audio_after(speaker, "Two.")
            return first, second

    first, second = asyncio.run(go())

    assert first == pytest.approx(simulated_live.FIRST_AUDIO_S, abs=0.05)
    assert second == pytest.approx(simulated_live.FIRST_AUDIO_S, abs=0.05)  # a new reply, a new wait


def test_clearing_drops_the_sentences_not_yet_spoken(load_test_mode) -> None:
    async def go():
        async with SimulatedLiveTTS(speed=10).open() as speaker:
            for sentence in ("First.", "Second.", "Third."):
                await speaker.say(sentence)
            await asyncio.sleep(0)  # the first is being spoken
            await speaker.clear()
            done = []
            output = speaker.output()
            try:
                while True:
                    item = await asyncio.wait_for(anext(output), 0.5)
                    if isinstance(item, SegmentDone):
                        done.append(item.sentences)
            except TimeoutError:
                return done

    assert asyncio.run(go()) == [(0,)]


def test_the_simulation_uses_the_measured_timings() -> None:
    # The medians from real calls (6.2's report, D94) and Aura's first audio.
    assert (simulated.DECIDE_S, simulated.ANSWER_S, simulated_live.FIRST_AUDIO_S) == (1.6, 1.1, 0.3)


# --- the load script's numbers -------------------------------------------------------------------


def test_expected_time_follows_the_simulated_model() -> None:
    assert load_test.expected_s(1, 1) == pytest.approx(simulated.DECIDE_S + simulated.ANSWER_S)
    assert load_test.expected_s(3, 2) == pytest.approx(simulated.ANSWER_S / 2)


def test_a_level_summary_counts_errors_and_overhead() -> None:
    turns = [
        {"latency_s": 3.0, "expected_s": 2.7, "error": None},
        {"latency_s": 1.5, "expected_s": 1.1, "error": None},
        {"latency_s": 30.0, "expected_s": 2.7, "error": "http_500"},
        {"latency_s": 0.0, "expected_s": 0.0, "error": "timeout"},
    ]

    row = load_test.summarize_level("typed", 4, turns, 1.0)

    assert (row["turns_ok"], row["errors"]) == (2, {"http_500": 1, "timeout": 1})
    assert row["latency_s"] == {"p50": 2.25, "p95": 3.0, "max": 3.0, "n": 2}
    assert row["overhead_s"]["max"] == pytest.approx(0.4)
    assert "errors: http_500 1, timeout 1" in load_test.format_row({**row, "server": {"tool_call_s": {"p95": None}}})


def test_a_small_load_test_runs_end_to_end(tmp_path) -> None:
    # A real server subprocess, simulated services 20x faster; live audio is
    # still sent in real time, as the server requires.
    assert load_test.main(["--typed", "2", "--live", "1", "--turns", "2", "--speed", "20", "--out", str(tmp_path)]) == 0

    [result] = tmp_path.glob("load-sqlite-*.json")
    data = json.loads(result.read_text())
    assert [(row["kind"], row["callers"], row["turns_ok"], row["errors"]) for row in data["levels"]] == [
        ("typed", 2, 4, {}), ("live", 1, 2, {})]
    live = data["levels"][1]
    assert live["server"]["live_first_audio_s"]["n"] == 2
    assert 0 <= live["overhead_s"]["p50"] < 1


# --- the fixes the load test led to (M6.5) ---------------------------------------------------


class _CountingConnections:
    """A model call that notes how many database connections are checked out
    while it runs."""

    def __init__(self, engine, reply) -> None:
        self.engine, self.reply, self.seen = engine, reply, None

    def create(self, **kwargs):
        self.seen = self.engine.pool.checkedout()
        return self.reply


@pytest.mark.parametrize("tool", [("t1", "list_accounts", {}),
                                  ("t1", "get_account_summary", {"account_id": "acct_a_1"})])
def test_no_connection_is_held_through_the_model_call_after_a_lookup(tool) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from backend.app.db import models
    from backend.tests.databases import make_test_engine
    from backend.tests.fake_llm import FakeLLM, say, use

    engine = make_test_engine(concurrent=True)  # a real pool, so checked-out connections count
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        seed_customer(db, "cust_a", [("acct_a_1", "USD", "active")])
    session = SessionStore().create("cust_a")
    answer = _CountingConnections(engine, say("Done."))

    with Session() as db:
        result = run_turn(session, "my balance?", llm=FakeLLM([use(tool), answer]), db=db, system="s")

    assert result.tool_calls[0].ok
    assert answer.seen == 0  # the lookup's connection went back to the pool
    with Session() as db:  # what the tool saved (an audited view) still stands
        views = db.scalars(select(models.ServicingRequestRecord)).all()
    assert len(views) == (1 if tool[1] == "get_account_summary" else 0)
    engine.dispose()


def test_live_agent_turns_run_on_their_own_pool_and_quick_writes_dont(monkeypatch) -> None:
    import threading

    from backend.app.voice import live_call
    from backend.app.voice.live_call import ReplyDone
    from backend.tests.databases import make_test_engine
    from backend.tests.fake_llm import FakeLLM, say
    from backend.tests.test_live_call import Call, run

    threads = {}

    class Noting:
        def create(self, **kwargs):
            threads["agent"] = threading.current_thread().name
            return say("Hello.")

    real_record = live_call.record_live_turn_metric

    def noting_record(db, metric):
        threads["timing"] = threading.current_thread().name
        real_record(db, metric)

    monkeypatch.setattr(live_call, "record_live_turn_metric", noting_record)
    live_call.shutdown_agent_workers()
    monkeypatch.setattr(config, "LIVE_AGENT_THREADS", 7)
    engine = make_test_engine()
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        seed_customer(db, "cust_a", [("acct_a_1", "USD", "active")])
    call = Call(factory, FakeLLM([Noting()]))

    async def go():
        call.start()
        call.listener.say_turn("Hi")
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert threads["agent"].startswith("live-agent")
    assert not threads["timing"].startswith("live-agent")
    assert live_call._agent_workers()._max_workers == 7
    live_call.shutdown_agent_workers()
    assert live_call._agent_pool is None
    engine.dispose()


def test_the_app_stops_the_live_agent_pool_and_ships_64_threads() -> None:
    from backend.app.voice import live_call

    live_call._agent_workers()
    with TestClient(app):
        pass

    assert live_call._agent_pool is None
    assert config.LIVE_AGENT_THREADS == 64


def test_a_write_a_tool_left_unsaved_is_dropped_not_saved(monkeypatch, db_session) -> None:
    # Every service commits its own writes; one that didn't would be a bug,
    # and its half-done work must not be saved by whatever commits next.
    from backend.app.db import models
    from backend.app.tools import accounts
    from backend.tests.fake_llm import FakeLLM, say, use

    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    real = accounts.list_customer_accounts

    def half_done(db, customer_id):
        db.add(models.CustomerRecord(customer_id="cust_half_done", display_name="Never saved"))
        db.flush()
        return real(db, customer_id)

    monkeypatch.setattr(accounts, "list_customer_accounts", half_done)
    session = SessionStore().create("cust_a")

    run_turn(session, "my accounts?", llm=FakeLLM([use(("t1", "list_accounts", {})), say("Done.")]),
             db=db_session, system="s")

    db_session.expire_all()
    assert db_session.get(models.CustomerRecord, "cust_half_done") is None
    assert db_session.get(models.CustomerRecord, "cust_a") is not None
