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
