"""The live call engine (M5.3): listening, guard, agent and speech wired
together, with fakes for speech and the model."""

import asyncio
import logging
import threading

import pytest
from sqlalchemy import String, create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import config
from backend.app.agent.llm import LLMUnavailableError
from backend.app.agent.loop import REPLY_UNAVAILABLE
from backend.app.agent.sessions import SessionStore
from backend.app.db import models
from backend.app.db.base import Base
from backend.app.voice import live_call
from backend.app.voice.guard import MAX_TRANSCRIPT_CHARS, REPLY_NOT_UNDERSTOOD, REPLY_TOO_LONG
from backend.app.voice.live_call import (
    REPLY_GOODBYE_CANT_HEAR,
    AgentAudio,
    AgentSentence,
    CallEnded,
    CallerSaid,
    Caption,
    ReplyDone,
    run_live_call,
)
from backend.tests.fake_live_speech import FakeLink, FakeLiveSTT, FakeLiveTTS, FakeSpeaker
from backend.tests.fake_llm import FakeLLM, FakeResponse, TextBlock, say, use
from backend.tests.helpers import seed_customer

SYSTEM = "test system prompt"


@pytest.fixture
def db_factory():
    # One in-memory database shared across threads: the agent runs on a
    # worker thread.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        seed_customer(db, "cust_a", [("acct_a_1", "USD", "active")])
        db.commit()
    yield factory
    engine.dispose()


class Call:
    def __init__(self, db_factory, llm, *, stt=None, tts=None, keyterms=()) -> None:
        self.session = SessionStore().create("cust_a")
        self.stt = stt or FakeLiveSTT()
        self.tts = tts or FakeLiveTTS()
        self.link = FakeLink()
        self.llm = llm
        self.db_factory = db_factory
        self.keyterms = keyterms
        self.task: asyncio.Task | None = None

    @property
    def listener(self):
        return self.stt.listener

    @property
    def speaker(self) -> FakeSpeaker:
        return self.tts.speaker

    def start(self) -> None:
        self.task = asyncio.create_task(run_live_call(
            self.session, self.link, stt=self.stt, tts=self.tts, llm=self.llm,
            db_factory=self.db_factory, system=SYSTEM, keyterms=self.keyterms,
        ))

    async def end(self) -> str:
        self.link.hang_up()
        return await self.task

    def metrics(self) -> list[models.LiveTurnMetricRecord]:
        with self.db_factory() as db:
            return list(db.scalars(select(models.LiveTurnMetricRecord).order_by(models.LiveTurnMetricRecord.metric_id)))


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, timeout=5))


class GatedLLM:
    """Streams `before`, waits until released, then streams `after`."""

    def __init__(self, before: str, after: str) -> None:
        self.before, self.after = before, after
        self.release = threading.Event()
        self.finished = False

    def create(self, *, system, tools, messages, on_text=None):
        on_text(self.before)
        assert self.release.wait(5)
        on_text(self.after)
        self.finished = True
        return FakeResponse("end_turn", [TextBlock(self.before + self.after)])


class BrokenMidStreamLLM:
    def create(self, *, system, tools, messages, on_text=None):
        on_text("Your balance is")
        raise LLMUnavailableError("dropped")


# --- a normal call ----------------------------------------------------------------


def test_a_turn_from_speech_to_spoken_reply(db_factory) -> None:
    call = Call(db_factory, FakeLLM([say("Your main account has 500 US dollars. Anything else?")]),
                keyterms=["main"])

    async def go():
        call.start()
        call.link.send_frame(b"\x01\x00" * 1280)
        call.listener.say_turn("What's my balance?")
        await call.link.wait_for(ReplyDone)
        return await call.end()

    reason = run(go())

    assert reason == "caller_hung_up"
    assert call.stt.opened_with == {"sample_rate": 16_000, "keyterms": ["main"]}
    assert call.listener.audio == [b"\x01\x00" * 1280]
    assert call.speaker.said == ["Your main account has 500 US dollars.", "Anything else?"]
    kinds = [type(e).__name__ for e in call.link.events]
    assert kinds[:2] == ["Caption", "CallerSaid"]
    assert call.link.of(Caption) == [Caption("What's my balance?")]
    assert call.link.of(CallerSaid) == [CallerSaid("What's my balance?")]
    assert call.link.of(AgentSentence) == [
        AgentSentence(0, 0, "Your main account has 500 US dollars."), AgentSentence(0, 1, "Anything else?"),
    ]
    assert all(audio.reply == 0 for audio in call.link.of(AgentAudio))
    assert kinds[-2:] == ["ReplyDone", "CallEnded"]
    assert call.link.events[-1] == CallEnded("caller_hung_up")
    assert call.session.state.turn == 1


def test_the_first_sentence_is_spoken_while_the_model_is_still_writing(db_factory) -> None:
    llm = GatedLLM("Let me look at that for you. ", "Your balance is 500 US dollars.")
    call = Call(db_factory, llm)

    async def go():
        call.start()
        call.listener.say_turn("What's my balance?")
        first = await call.link.wait_for(AgentSentence)
        spoken_while_writing = list(call.speaker.said)
        audio_while_writing = len(call.link.of(AgentAudio))
        still_writing = not llm.finished
        llm.release.set()
        await call.link.wait_for(ReplyDone)
        await call.end()
        return first, spoken_while_writing, audio_while_writing, still_writing

    first, spoken, audio, still_writing = run(go())

    assert still_writing
    assert first == AgentSentence(0, 0, "Let me look at that for you.")
    assert spoken == ["Let me look at that for you."]
    assert audio > 0
    assert call.speaker.said[-1] == "Your balance is 500 US dollars."


def test_a_reply_whose_audio_all_arrived_before_it_ended_completes_at_once(db_factory, monkeypatch, caplog) -> None:
    # "Let me check that." plays out completely while the agent is still
    # working, and nothing more is said: the reply must end as soon as the
    # turn does, not wait for audio that already came.
    monkeypatch.setattr(live_call, "REPLY_AUDIO_TIMEOUT_S", 0.5)
    llm = GatedLLM("Let me check that. ", "")
    call = Call(db_factory, llm)

    async def go():
        call.start()
        call.listener.say_turn("Which accounts do I have?")
        await call.link.wait_for(AgentAudio)
        await asyncio.sleep(0.05)  # its SegmentDone is handled before the turn ends
        llm.release.set()
        await call.link.wait_for(ReplyDone)
        await call.end()

    with caplog.at_level(logging.WARNING):
        run(go())

    assert call.speaker.said == ["Let me check that."]
    assert "timed out" not in caplog.text  # finished on its own, not by the safety timeout


def test_a_reply_with_a_tool_call_in_the_middle_is_spoken_in_order(db_factory) -> None:
    llm = FakeLLM([use(("t1", "list_accounts", {}), text="One moment. "), say("You have one account.")])
    call = Call(db_factory, llm)

    async def go():
        call.start()
        call.listener.say_turn("Which accounts do I have?")
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert call.speaker.said == ["One moment.", "You have one account."]


def test_a_reply_whose_audio_comes_at_the_end_completes(db_factory) -> None:
    call = Call(db_factory, FakeLLM([say("One. Two. Three.")]),
                tts=FakeLiveTTS(FakeSpeaker(hold_until_end_reply=True)))

    async def go():
        call.start()
        call.listener.say_turn("Count please")
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert call.speaker.replies_ended == 1
    assert len(call.link.of(AgentAudio)) == 3


def test_turns_that_end_while_the_agent_is_busy_wait_their_turn(db_factory) -> None:
    llm = GatedLLM("First answer. ", "Done.")

    class TwoTurns:
        calls = 0

        def create(self, **kwargs):
            TwoTurns.calls += 1
            if TwoTurns.calls == 1:
                return llm.create(**kwargs)
            kwargs["on_text"]("Second answer.")
            return FakeResponse("end_turn", [TextBlock("Second answer.")])

    call = Call(db_factory, TwoTurns())

    async def go():
        call.start()
        call.listener.say_turn("First question")
        await call.link.wait_for(AgentSentence)
        call.listener.say_turn("Second question")
        await asyncio.sleep(0.05)
        second_started_early = len(call.link.of(CallerSaid)) > 1
        llm.release.set()
        await call.link.wait_for(ReplyDone, count=2)
        await call.end()
        return second_started_early

    assert run(go()) is False
    assert call.link.of(CallerSaid) == [CallerSaid("First question"), CallerSaid("Second question")]
    assert call.speaker.said == ["First answer.", "Done.", "Second answer."]
    assert call.session.state.turn == 2


# --- the guard ----------------------------------------------------------------------


def test_silence_gets_no_reply(db_factory) -> None:
    call = Call(db_factory, FakeLLM([]))

    async def go():
        call.start()
        call.listener.hear("end_of_turn", "  ")
        await asyncio.sleep(0.05)
        return await call.end()

    run(go())

    assert call.speaker.said == []
    assert call.link.events == [CallEnded("caller_hung_up")]
    assert call.metrics() == []


@pytest.mark.parametrize(
    ("confidences", "understood"),
    [((0.9, 0.2), False), ((0.95, 0.5), True)],
    ids=["mean-0.55-blocked", "mean-0.725-passes-though-min-is-0.5"],
)
def test_the_guard_uses_mean_word_confidence(db_factory, confidences, understood) -> None:
    call = Call(db_factory, FakeLLM([say("Sure.")]))

    async def go():
        call.start()
        call.listener.hear("end_of_turn", "pay fifty", confidences)
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert (call.speaker.said == ["Sure."]) is understood
    if not understood:
        assert call.speaker.said == [REPLY_NOT_UNDERSTOOD]
        assert call.llm.requests == []
        assert call.session.state.turn == 0
        assert call.session.unheard_by_model == [REPLY_NOT_UNDERSTOOD]


def test_the_live_threshold_is_its_own_setting_read_at_call_time(db_factory, monkeypatch) -> None:
    monkeypatch.setattr(config, "LIVE_MIN_CONFIDENCE", 0.99)
    call = Call(db_factory, FakeLLM([]))

    async def go():
        call.start()
        call.listener.hear("end_of_turn", "yes", (0.98,))
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert config.LIVE_MIN_CONFIDENCE == 0.99 and config.VOICE_MIN_CONFIDENCE == 0.6
    assert call.speaker.said == [REPLY_NOT_UNDERSTOOD]


def test_an_overlong_turn_never_reaches_the_agent(db_factory) -> None:
    call = Call(db_factory, FakeLLM([]))

    async def go():
        call.start()
        call.listener.hear("end_of_turn", "x" * (MAX_TRANSCRIPT_CHARS + 1), (0.99,))
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert call.speaker.said == [REPLY_TOO_LONG]
    assert call.link.of(CallerSaid) == []


# --- fixed replies and failures -------------------------------------------------------


def test_a_fixed_reply_is_spoken_after_a_stream_cut_off(db_factory) -> None:
    call = Call(db_factory, BrokenMidStreamLLM())

    async def go():
        call.start()
        call.listener.say_turn("What's my balance?")
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert call.speaker.said == ["Your balance is", REPLY_UNAVAILABLE]


def test_a_fixed_reply_is_spoken_when_nothing_was_streamed(db_factory) -> None:
    call = Call(db_factory, FakeLLM([LLMUnavailableError("down")]))

    async def go():
        call.start()
        call.listener.say_turn("Hello?")
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert call.speaker.said == [REPLY_UNAVAILABLE]


def test_listening_that_fails_mid_call_ends_it_politely(db_factory) -> None:
    call = Call(db_factory, FakeLLM([]))

    async def go():
        call.start()
        call.listener.break_down()
        return await call.task

    assert run(go()) == "listening_unavailable"
    assert call.speaker.said == [REPLY_GOODBYE_CANT_HEAR]
    assert call.link.events[-1] == CallEnded("listening_unavailable")


def test_audio_that_cant_be_sent_to_the_listener_ends_the_call(db_factory) -> None:
    call = Call(db_factory, FakeLLM([]))
    call.listener.fail_on_audio = True

    async def go():
        call.start()
        call.link.send_frame(b"\x00\x00")
        return await call.task

    assert run(go()) == "listening_unavailable"


def test_listening_that_cant_start_ends_the_call_at_once(db_factory) -> None:
    call = Call(db_factory, FakeLLM([]), stt=FakeLiveSTT(fail_to_open=True))

    assert run(_start_and_wait(call)) == "listening_unavailable"
    assert call.speaker.said == []
    assert call.link.events == [CallEnded("listening_unavailable")]


async def _start_and_wait(call: Call) -> str:
    call.start()
    return await call.task


def test_no_voice_means_a_text_only_call(db_factory) -> None:
    call = Call(db_factory, FakeLLM([say("Hello there. How can I help?")]), tts=FakeLiveTTS(fail_to_open=True))

    async def go():
        call.start()
        call.listener.say_turn("Hi")
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert [s.text for s in call.link.of(AgentSentence)] == ["Hello there.", "How can I help?"]
    assert call.link.of(AgentAudio) == []
    assert call.metrics()[0].audio_ok is False


def test_a_voice_that_fails_mid_call_leaves_the_rest_as_text(db_factory) -> None:
    speaker = FakeSpeaker()
    speaker.fail_on_say = 1
    call = Call(db_factory, FakeLLM([say("First. Second."), say("Later.")]), tts=FakeLiveTTS(speaker))

    async def go():
        call.start()
        call.listener.say_turn("One")
        await call.link.wait_for(ReplyDone)
        call.listener.say_turn("Two")
        await call.link.wait_for(ReplyDone, count=2)
        await call.end()

    run(go())

    assert speaker.said == ["First."]
    assert [s.text for s in call.link.of(AgentSentence)] == ["First.", "Second.", "Later."]


def test_a_voice_that_drops_mid_reply_leaves_this_and_later_replies_as_text(db_factory) -> None:
    speaker = FakeSpeaker(hold_until_end_reply=True)
    gated = GatedLLM("Part one. ", "Part two.")

    class ThenLater:
        calls = 0

        def create(self, **kwargs):
            ThenLater.calls += 1
            if ThenLater.calls == 1:
                return gated.create(**kwargs)
            kwargs["on_text"]("Later.")
            return FakeResponse("end_turn", [TextBlock("Later.")])

    call = Call(db_factory, ThenLater(), tts=FakeLiveTTS(speaker))

    async def go():
        call.start()
        call.listener.say_turn("Tell me")
        await call.link.wait_for(AgentSentence)
        speaker.break_down()
        await asyncio.sleep(0.05)
        gated.release.set()
        await call.link.wait_for(ReplyDone)
        call.listener.say_turn("And then?")
        await call.link.wait_for(ReplyDone, count=2)
        await call.end()

    run(go())

    assert [s.text for s in call.link.of(AgentSentence)] == ["Part one.", "Part two.", "Later."]
    # Nothing more went to the broken voice.
    assert speaker.said == ["Part one."]


def test_an_overlong_sentence_is_shown_but_speech_carries_on(db_factory) -> None:
    speaker = FakeSpeaker()
    speaker.too_long = {"Middle."}
    call = Call(db_factory, FakeLLM([say("Start. Middle. End.")]), tts=FakeLiveTTS(speaker))

    async def go():
        call.start()
        call.listener.say_turn("Go")
        await call.link.wait_for(ReplyDone)
        await call.end()

    run(go())

    assert speaker.said == ["Start.", "End."]
    assert [s.text for s in call.link.of(AgentSentence)] == ["Start.", "Middle.", "End."]


def test_a_handoff_ends_the_call_after_the_reply(db_factory) -> None:
    llm = FakeLLM([use(("t1", "handoff_to_human", {"reason": "caller_requested"})),
                   say("I'm connecting you with a member of our team.")])
    call = Call(db_factory, llm)

    async def go():
        call.start()
        call.listener.say_turn("A person, please")
        return await call.task

    assert run(go()) == "handed_off"
    kinds = [type(e).__name__ for e in call.link.events]
    assert kinds[-2:] == ["ReplyDone", "CallEnded"]
    assert call.speaker.said == ["I'm connecting you with a member of our team."]


def test_a_bug_in_our_code_is_not_hidden(db_factory) -> None:
    call = Call(db_factory, FakeLLM([RuntimeError("bug")]))

    async def go():
        call.start()
        call.listener.say_turn("Hello")
        await call.task

    with pytest.raises(BaseExceptionGroup) as raised:
        run(go())
    assert raised.group_contains(RuntimeError)


# --- metrics ------------------------------------------------------------------------


def test_each_turn_records_timings_and_never_words(db_factory) -> None:
    call = Call(db_factory, FakeLLM([say("Your balance is 500 US dollars.")]))

    async def go():
        call.start()
        call.listener.hear("end_of_turn", "pay fifty", (0.4, 0.3))
        await call.link.wait_for(ReplyDone)
        call.listener.hear("end_of_turn", "What's my balance?", (0.9, 0.8, 1.0))
        await call.link.wait_for(ReplyDone, count=2)
        await call.end()

    run(go())

    guarded, answered = call.metrics()
    assert (guarded.outcome, guarded.turn, guarded.first_text_ms) == ("not_understood", 0, None)
    assert guarded.transcript_chars == len("pay fifty")
    assert guarded.confidence_mean == pytest.approx(0.35) and guarded.confidence_min == pytest.approx(0.3)
    assert answered.outcome == "reply" and answered.turn == 1
    assert answered.reply_chars == len("Your balance is 500 US dollars.") and answered.sentences == 1
    assert answered.audio_ok is True
    assert 0 <= answered.first_text_ms <= answered.first_audio_ms
    assert answered.agent_ms is not None and answered.agent_ms >= answered.first_text_ms
    # Nowhere to put words: the only string columns are IDs and the outcome code.
    string_columns = {c.name for c in models.LiveTurnMetricRecord.__table__.columns if isinstance(c.type, String)}
    assert string_columns == {"session_id", "customer_id", "outcome"}


def test_a_metrics_failure_never_breaks_the_call(db_factory, monkeypatch, caplog) -> None:
    from backend.app.services import voice_metrics

    monkeypatch.setattr(voice_metrics, "LiveTurnMetricRecord", lambda **_: 1 / 0)
    call = Call(db_factory, FakeLLM([say("Hi.")]))

    async def go():
        call.start()
        call.listener.say_turn("Hello")
        await call.link.wait_for(ReplyDone)
        return await call.end()

    with caplog.at_level(logging.ERROR):
        assert run(go()) == "caller_hung_up"
    assert "Could not record a live turn metric" in caplog.text
