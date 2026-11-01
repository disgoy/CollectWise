"""The live-call report script (M5.7): numbers only, read from the database."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.db.models import LiveTurnMetricRecord
from scripts import live_report
from scripts.live_report import load, percentile, summarize


def metric(turn: int, outcome: str = "reply", *, text=2600, audio=2900, agent=3000, mean=0.98, lowest=0.9,
           session="sess_aaaaaaaa11112222", audio_ok=True, minutes_ago: float = 1) -> LiveTurnMetricRecord:
    return LiveTurnMetricRecord(
        session_id=session, customer_id="cust_demo", turn=turn, outcome=outcome,
        confidence_mean=mean, confidence_min=lowest, transcript_chars=20, reply_chars=60, sentences=2,
        first_text_ms=text, first_audio_ms=audio, agent_ms=agent, audio_ok=audio_ok,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


@pytest.mark.parametrize(("fraction", "expected"), [(0.5, 3), (0.9, 5), (1.0, 5), (0.0, 1)])
def test_nearest_rank_percentile(fraction, expected) -> None:
    assert percentile([5, 1, 4, 2, 3], fraction) == expected


def test_the_report_covers_speed_hearing_and_each_turn() -> None:
    rows = [
        metric(1, text=1500, audio=2200, agent=2000),
        metric(2, text=2600, audio=2900, agent=3000, mean=0.95, lowest=0.55),
        metric(2, "not_understood", text=None, audio=3100, agent=None, mean=0.4, lowest=0.2),
        metric(3, text=None, audio=None, agent=2500, audio_ok=False),
    ]

    report = summarize(rows)

    assert "4 caller turns in 1 call(s): reply 3, not_understood 1" in report
    assert "to the first audio  <- the headline    median   2550 ms" in report  # replies only: 2200, 2900
    assert "speech time (first audio - first text) median    500 ms" in report  # 700 and 300
    assert "1 reply had no audio (the voice failed, or the caller spoke before it began)" in report
    assert "lowest word                            median 0.73, min 0.20, below 0.6: 2" in report
    assert "add 0.5-1.2 s" in report
    assert report.count("11112222") == 4  # one line per turn, by the call's short id


def test_an_empty_window_says_so() -> None:
    assert summarize([]) == "No live turns recorded in this window."


def test_the_window_and_session_filters(db_session) -> None:
    db_session.add_all([
        metric(1, session="sess_old", minutes_ago=120),
        metric(1, session="sess_new", minutes_ago=5),
        metric(2, session="sess_new", minutes_ago=4),
    ])
    db_session.commit()

    assert len(load(db_session, minutes=None, session_id=None)) == 3
    assert [r.session_id for r in load(db_session, minutes=60, session_id=None)] == ["sess_new", "sess_new"]
    assert [r.turn for r in load(db_session, minutes=None, session_id="sess_old")] == [1]


def test_the_command_prints_the_report(db_session, capsys) -> None:
    db_session.add(metric(1))
    db_session.commit()

    assert live_report.main([], session_factory=lambda: _Borrowed(db_session)) == 0

    assert "1 caller turns in 1 call(s): reply 1" in capsys.readouterr().out


class _Borrowed:
    """The test's session as a context manager that doesn't close it."""

    def __init__(self, db) -> None:
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc) -> None:
        return None


def test_a_score_of_exactly_0_6_is_not_below_it() -> None:
    report = summarize([metric(1, mean=0.6, lowest=0.6), metric(2, mean=0.59, lowest=0.59)])

    assert "average word                           median 0.59, min 0.59, below 0.6: 1" in report
    assert "lowest word                            median 0.59, min 0.59, below 0.6: 1" in report


# --- phone and browser calls (M7.5) ----------------------------------------------------------

PHONE = "sess_phone00000001"
BROWSER = "sess_browser000001"


def verified(session: str, event_type: str = "caller_verified"):
    from backend.app.db.models import ConversationEventRecord
    return ConversationEventRecord(session_id=session, customer_id="cust_demo", turn=0, event_type=event_type,
                                   outcome="verified")


def test_phone_calls_are_known_by_their_verification(db_session) -> None:
    rows = [metric(1, session=PHONE), metric(1, session=BROWSER), metric(1, session="sess_failed0000001")]
    db_session.add_all(rows + [verified(PHONE), verified("sess_failed0000001", "verification_failed"),
                               verified("sess_elsewhere00001")])
    db_session.commit()

    assert live_report.phone_sessions(db_session, rows) == {PHONE}
    assert live_report.phone_sessions(db_session, []) == set()


def test_with_both_kinds_the_headline_is_shown_for_each() -> None:
    rows = [metric(1, session=PHONE, audio=3400), metric(2, session=PHONE, audio=3600),
            metric(1, session=BROWSER, audio=2900)]

    report = summarize(rows, phone={PHONE})

    assert "  1 phone call(s), 1 browser call(s)" in report
    assert "  phone calls                          median   3500 ms" in report
    assert "  browser calls                        median   2900 ms" in report
    assert report.count(f"{PHONE[-8:]}  phone  ") == 2
    assert report.count(f"{BROWSER[-8:]}  browser") == 1


def test_with_one_kind_there_is_no_split() -> None:
    browser_only = summarize([metric(1, session=BROWSER)])
    phone_only = summarize([metric(1, session=PHONE)], phone={PHONE})

    assert "phone call(s)" not in browser_only and "  phone calls" not in browser_only
    assert "  1 phone call(s), 0 browser call(s)" in phone_only
    assert "  phone calls " not in phone_only and "  browser calls" not in phone_only


@pytest.mark.parametrize(("flag", "kept"), [([], "2 calls"), (["--phone"], PHONE), (["--browser"], BROWSER)])
def test_the_command_filters_by_kind_of_call(db_session, capsys, flag, kept) -> None:
    db_session.add_all([metric(1, session=PHONE), metric(1, session=BROWSER), verified(PHONE)])
    db_session.commit()

    assert live_report.main(flag, session_factory=lambda: _Borrowed(db_session)) == 0

    out = capsys.readouterr().out
    if kept == "2 calls":
        assert "2 caller turns in 2 call(s)" in out
    else:
        assert "1 caller turns in 1 call(s)" in out
        assert kept[-8:] in out


def test_phone_and_browser_filters_cant_be_combined(db_session) -> None:
    with pytest.raises(SystemExit):
        live_report.main(["--phone", "--browser"], session_factory=lambda: _Borrowed(db_session))
