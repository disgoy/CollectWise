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
    assert "1 reply had no audio" in report
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
