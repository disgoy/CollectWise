import json
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent.loop import REPLY_HANDED_OFF, REPLY_TURN_LIMIT
from backend.app.agent.sessions import ConversationSession, SessionStore
from backend.app.db import models
from backend.app.services import voice_metrics
from backend.app.voice.audio import AudioClip
from backend.app.voice.speech import SpeechUnavailableError, Transcript
from backend.app.voice.turn import (
    MAX_TRANSCRIPT_CHARS,
    REPLY_CANT_HEAR,
    REPLY_NOT_UNDERSTOOD,
    REPLY_TOO_LONG,
    run_voice_turn,
)
from backend.tests.fake_llm import FakeLLM, say, use
from backend.tests.fake_speech import FakeSTT, FakeTTS
from backend.tests.helpers import seed_customer

SYSTEM = "test system prompt"
CLIP = AudioClip(data=bytes(2 * 1_600), sample_rate=16_000)  # 100 ms


@pytest.fixture
def session(db_session: Session) -> ConversationSession:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    return SessionStore().create("cust_a")


def voice(session, db, *, stt, tts=None, llm=None):
    return run_voice_turn(
        session, CLIP, stt=stt, tts=tts or FakeTTS(), llm=llm or FakeLLM([]), db=db, system=SYSTEM,
    )


def metrics(db: Session) -> list[models.VoiceTurnMetricRecord]:
    return list(db.scalars(select(models.VoiceTurnMetricRecord).order_by(models.VoiceTurnMetricRecord.metric_id)))


def caller_text_seen_by_model(llm: FakeLLM) -> str:
    return llm.requests[0]["messages"][-1]["content"]


# --- the normal path -----------------------------------------------------------


def test_heard_words_go_to_the_agent_and_the_reply_is_spoken(session, db_session) -> None:
    stt = FakeSTT([Transcript("What accounts do I have?", 0.93)])
    tts = FakeTTS()
    llm = FakeLLM([use(("t1", "list_accounts", {})), say("You have one account.")])

    result = voice(session, db_session, stt=stt, tts=tts, llm=llm)

    assert stt.clips == [CLIP]
    assert caller_text_seen_by_model(llm) == "What accounts do I have?"
    assert result.transcript == "What accounts do I have?"
    assert result.reply == "You have one account."
    assert result.handed_off is False
    assert tts.texts == ["You have one account."]
    assert result.audio.duration_ms == len("You have one account.") * 60
    assert session.state.turn == 1


def test_surrounding_whitespace_is_trimmed(session, db_session) -> None:
    llm = FakeLLM([say("Hi!")])

    result = voice(session, db_session, stt=FakeSTT([Transcript("  hello \n", 0.9)]), llm=llm)

    assert caller_text_seen_by_model(llm) == "hello"
    assert result.transcript == "hello"


def test_metric_row_for_a_normal_turn(session, db_session) -> None:
    voice(session, db_session, stt=FakeSTT([Transcript("hello", 0.88)]), llm=FakeLLM([say("Hi there!")]))

    [row] = metrics(db_session)
    assert (row.session_id, row.customer_id, row.turn, row.outcome) == (
        session.session_id, "cust_a", 1, "reply",
    )
    assert row.audio_ok is True
    assert row.audio_in_ms == 100
    assert row.audio_out_ms == len("Hi there!") * 60
    assert row.confidence == pytest.approx(0.88)
    assert (row.transcript_chars, row.reply_chars) == (5, 9)
    assert all(isinstance(v, int) and v >= 0 for v in (row.stt_ms, row.agent_ms, row.tts_ms, row.total_ms))
    assert row.total_ms >= row.stt_ms + row.agent_ms + row.tts_ms - 2  # rounding


# --- the guard: the model is not called ---------------------------------------


@pytest.mark.parametrize(
    "heard",
    [Transcript("pay 120", 0.3), Transcript("", 0.99), Transcript("   ", None), Transcript("pay 120", 0.0)],
    ids=["low-confidence", "empty", "blank", "zero-confidence"],
)
def test_misheard_turns_never_reach_the_agent(session, db_session, heard) -> None:
    llm = FakeLLM([])
    tts = FakeTTS()

    result = voice(session, db_session, stt=FakeSTT([heard]), tts=tts, llm=llm)

    assert llm.requests == []
    assert result.reply == REPLY_NOT_UNDERSTOOD
    assert tts.texts == [REPLY_NOT_UNDERSTOOD]
    assert result.transcript == heard.text.strip()
    assert session.state.turn == 0  # not a caller turn
    assert session.messages == []
    [row] = metrics(db_session)
    assert (row.outcome, row.agent_ms, row.turn) == ("not_understood", None, 0)


def test_confidence_exactly_at_the_threshold_is_accepted(session, db_session) -> None:
    llm = FakeLLM([say("Hi!")])

    voice(session, db_session, stt=FakeSTT([Transcript("hello", config.VOICE_MIN_CONFIDENCE)]), llm=llm)

    assert len(llm.requests) == 1


def test_missing_confidence_is_accepted(session, db_session) -> None:
    llm = FakeLLM([say("Hi!")])

    voice(session, db_session, stt=FakeSTT([Transcript("hello", None)]), llm=llm)

    assert len(llm.requests) == 1
    assert metrics(db_session)[0].confidence is None


def test_threshold_comes_from_config_at_call_time(session, db_session, monkeypatch) -> None:
    monkeypatch.setattr(config, "VOICE_MIN_CONFIDENCE", 0.95)

    result = voice(session, db_session, stt=FakeSTT([Transcript("hello", 0.9)]))

    assert result.reply == REPLY_NOT_UNDERSTOOD


def test_overlong_transcript_never_reaches_the_agent(session, db_session) -> None:
    llm = FakeLLM([])

    result = voice(session, db_session, stt=FakeSTT([Transcript("a" * (MAX_TRANSCRIPT_CHARS + 1), 0.9)]), llm=llm)

    assert llm.requests == []
    assert result.reply == REPLY_TOO_LONG
    assert metrics(db_session)[0].outcome == "too_long"


def test_transcript_at_the_length_limit_is_accepted(session, db_session) -> None:
    llm = FakeLLM([say("OK, that's a lot.")])

    voice(session, db_session, stt=FakeSTT([Transcript("a" * MAX_TRANSCRIPT_CHARS, 0.9)]), llm=llm)

    assert len(llm.requests) == 1


def test_payment_confirmation_still_needs_a_real_later_turn(session, db_session) -> None:
    # Turn 1 proposes; a misheard turn follows; only the next real turn can confirm.
    llm = FakeLLM([
        use(("t1", "propose_payment", {"account_id": "acct_a_1", "amount": "120"})),
        say("120.00 USD from your account. Shall I go ahead?"),
        use(("t2", "confirm_payment", {})),
        say("Your payment request is recorded."),
    ])
    voice(session, db_session, stt=FakeSTT([Transcript("Pay 120 please", 0.95)]), llm=llm)
    voice(session, db_session, stt=FakeSTT([Transcript("mmhm", 0.2)]), llm=llm)
    assert session.state.turn == 1

    result = voice(session, db_session, stt=FakeSTT([Transcript("Yes, go ahead", 0.97)]), llm=llm)

    assert result.reply == "Your payment request is recorded."
    assert len(list(db_session.scalars(select(models.ServicingRequestRecord)))) == 1


# --- outages ---------------------------------------------------------------------


def test_listening_outage_gets_a_polite_reply(session, db_session) -> None:
    llm = FakeLLM([])
    tts = FakeTTS()

    result = voice(session, db_session, stt=FakeSTT([SpeechUnavailableError("down")]), tts=tts, llm=llm)

    assert result.transcript is None
    assert result.reply == REPLY_CANT_HEAR
    assert tts.texts == [REPLY_CANT_HEAR]
    assert llm.requests == []
    assert session.state.turn == 0
    [row] = metrics(db_session)
    assert (row.outcome, row.confidence, row.transcript_chars) == ("stt_unavailable", None, None)
    assert row.stt_ms is not None and row.agent_ms is None


def test_speaking_outage_still_returns_the_text(session, db_session) -> None:
    tts = FakeTTS(failures=[SpeechUnavailableError("busy")])

    result = voice(session, db_session, stt=FakeSTT([Transcript("hello", 0.9)]), tts=tts, llm=FakeLLM([say("Hi there!")]))

    assert result.reply == "Hi there!"
    assert result.audio is None
    [row] = metrics(db_session)
    assert (row.outcome, row.audio_ok, row.audio_out_ms) == ("reply", False, None)
    assert row.tts_ms is not None


def test_both_speech_services_down(session, db_session) -> None:
    result = voice(
        session, db_session,
        stt=FakeSTT([SpeechUnavailableError("down")]),
        tts=FakeTTS(failures=[SpeechUnavailableError("down")]),
    )

    assert (result.reply, result.audio, result.transcript) == (REPLY_CANT_HEAR, None, None)


def test_a_bug_in_the_listener_is_not_hidden(session, db_session) -> None:
    with pytest.raises(RuntimeError):
        voice(session, db_session, stt=FakeSTT([RuntimeError("bad API key")]))


def test_a_bug_in_the_speaker_is_not_hidden(session, db_session) -> None:
    with pytest.raises(RuntimeError):
        voice(
            session, db_session,
            stt=FakeSTT([Transcript("hello", 0.9)]),
            tts=FakeTTS(failures=[RuntimeError("bad request")]),
            llm=FakeLLM([say("Hi!")]),
        )


# --- after a handoff -------------------------------------------------------------


def test_after_handoff_nothing_is_transcribed(session, db_session) -> None:
    llm = FakeLLM([use(("t1", "handoff_to_human", {"reason": "caller_requested"})), say("Connecting you.")])
    first = voice(session, db_session, stt=FakeSTT([Transcript("A person please", 0.9)]), llm=llm)
    assert first.handed_off is True

    stt = FakeSTT([])  # any transcription attempt would fail the test
    result = voice(session, db_session, stt=stt, llm=llm)

    assert stt.clips == []
    assert result.reply == REPLY_HANDED_OFF
    assert result.handed_off is True
    assert result.transcript is None
    assert metrics(db_session)[-1].outcome == "already_handed_off"
    assert metrics(db_session)[-1].stt_ms is None


# --- telemetry -------------------------------------------------------------------


def test_metrics_hold_no_words(session, db_session) -> None:
    voice(
        session, db_session,
        stt=FakeSTT([Transcript("CANARY-HEARD-7f3a pay 123.45", 0.9)]),
        llm=FakeLLM([say("CANARY-REPLY-9d2e")]),
    )

    dump = json.dumps([
        {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
        for row in metrics(db_session)
    ])
    for secret in ("CANARY-HEARD-7f3a", "CANARY-REPLY-9d2e", "123.45"):
        assert secret not in dump


def test_metric_failure_never_breaks_the_turn(session, db_session, monkeypatch, caplog) -> None:
    def broken_save(db, metric):
        raise RuntimeError("disk full")

    monkeypatch.setattr(voice_metrics, "_save_metric", broken_save)

    with caplog.at_level(logging.ERROR):
        result = voice(session, db_session, stt=FakeSTT([Transcript("hello", 0.9)]), llm=FakeLLM([say("Hi!")]))

    assert result.reply == "Hi!"
    assert "Could not record a voice turn metric" in caplog.text
    assert db_session.get(models.CustomerRecord, "cust_a") is not None


def test_unknown_outcome_is_rejected_by_the_database(db_session) -> None:
    db_session.add(models.VoiceTurnMetricRecord(
        session_id="sess_x", customer_id="cust_a", turn=0, outcome="caller_said_hi",
        audio_ok=True, audio_in_ms=0, total_ms=0, reply_chars=0,
    ))

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_default_threshold_is_the_measured_one() -> None:
    # D69: chosen from the 4.7 voice checks. Skipped if the shell overrides it.
    import os

    if "COLLECTWISE_VOICE_MIN_CONFIDENCE" in os.environ:
        pytest.skip("threshold overridden in this shell")
    assert config.VOICE_MIN_CONFIDENCE == 0.6


def test_a_mishearing_just_under_the_threshold_is_blocked(session, db_session, monkeypatch) -> None:
    # "Pay thirteen dollars" was heard as "Pay $15" at 0.59 in the voice checks.
    monkeypatch.setattr(config, "VOICE_MIN_CONFIDENCE", 0.6)
    llm = FakeLLM([])

    result = voice(session, db_session, stt=FakeSTT([Transcript("Pay $15", 0.59)]), llm=llm)

    assert result.reply == REPLY_NOT_UNDERSTOOD
    assert llm.requests == []


def test_a_spoken_turn_past_the_limit_is_handed_off(session, db_session, monkeypatch) -> None:
    monkeypatch.setattr(config, "MAX_CALLER_TURNS", 0)
    stt = FakeSTT([Transcript("Hello?", 0.95)])
    tts = FakeTTS()

    result = voice(session, db_session, stt=stt, tts=tts)

    assert result.reply == REPLY_TURN_LIMIT
    assert result.handed_off is True
    assert tts.texts == [REPLY_TURN_LIMIT]

    # From then on nothing is transcribed (D63): the script has no more lines.
    assert voice(session, db_session, stt=stt).reply == REPLY_HANDED_OFF
