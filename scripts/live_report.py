"""Live-call report (M5.7): what the live_turn_metrics table says about speed
and hearing. Free: reads the local database only. The table holds numbers and
codes, never words, so neither does this report.

Timings start when Flux says the caller has finished, not when they actually
stopped talking: add Flux's own end-of-turn lag (0.5-1.2 s measured, D80) for
what the caller experiences.

Per stage (M6.2), from the conversation events of the same turns: model calls
that decide to use a tool and those that answer, tools, and model calls per
turn. Samples are small, so the p95 is close to the max: both are shown.

Phone calls (M7.5) are told apart from browser calls by their caller_verified
event; with both in the window, the headline is also shown for each. Phone
timings stop where ours do: the phone line and Twilio's playback add more.

Usage, from the repo root:
    python -m scripts.live_report [--minutes 60] [--session sess_...] [--phone | --browser]
"""

import argparse
import statistics
import sys
from collections import Counter
from collections.abc import Sequence, Set
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import ConversationEventRecord, LiveTurnMetricRecord
from backend.app.db.session import SessionLocal, require_current_schema


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    rank = max(1, round(fraction * len(ordered) + 0.5 - 1e-9))
    return ordered[min(rank, len(ordered)) - 1]


def _spread(name: str, values: list[float]) -> str:
    if not values:
        return f"  {name:38} -"
    return (
        f"  {name:38} median {statistics.median(values):6.0f} ms, p95 {percentile(values, 0.95):6.0f} ms, "
        f"min {min(values):6.0f} ms, max {max(values):6.0f} ms  (n={len(values)})"
    )


def summarize(rows: list[LiveTurnMetricRecord], events: Sequence[ConversationEventRecord] = (),
              phone: Set[str] = frozenset()) -> str:
    """`phone`: the session ids of phone calls; every other call is a browser's."""
    if not rows:
        return "No live turns recorded in this window."
    lines = []
    sessions = {row.session_id for row in rows}
    outcomes = Counter(row.outcome for row in rows)
    lines.append(f"{len(rows)} caller turns in {len(sessions)} call(s): " + ", ".join(
        f"{outcome} {count}" for outcome, count in outcomes.most_common()))
    phone_calls = len(sessions & phone)
    if phone_calls:
        lines.append(f"  {phone_calls} phone call(s), {len(sessions) - phone_calls} browser call(s)")

    replies = [row for row in rows if row.outcome == "reply"]
    lines.append("\nSpeed (from Flux's end of turn; add 0.5-1.2 s for what the caller feels):")
    first_text = [r.first_text_ms for r in replies if r.first_text_ms is not None]
    first_audio = [r.first_audio_ms for r in replies if r.first_audio_ms is not None]
    lines.append(_spread("to the agent's first words (text)", first_text))
    lines.append(_spread("to the first audio  <- the headline", first_audio))
    if sessions & phone and sessions - phone:
        for name, on_phone in (("  phone calls", True), ("  browser calls", False)):
            lines.append(_spread(name, [r.first_audio_ms for r in replies
                                        if r.first_audio_ms is not None and (r.session_id in phone) == on_phone]))
    lines.append(_spread("speech time (first audio - first text)", [
        r.first_audio_ms - r.first_text_ms for r in replies
        if r.first_audio_ms is not None and r.first_text_ms is not None
    ]))
    lines.append(_spread("whole agent turn", [r.agent_ms for r in replies if r.agent_ms is not None]))
    no_audio = sum(not r.audio_ok for r in replies)
    if no_audio:
        lines.append(f"  {no_audio} repl{'y' if no_audio == 1 else 'ies'} had no audio (the voice failed: text only)")

    if events:
        lines.append("\nPer stage (from the conversation events of these turns):")
        lines.extend(_stages(events))

    lines.append("\nHearing (word confidence of the final transcript):")
    lines.append(_confidence_line("average word", [r.confidence_mean for r in rows if r.confidence_mean is not None]))
    lines.append(_confidence_line("lowest word", [r.confidence_min for r in rows if r.confidence_min is not None]))

    lines.append("\nTurn by turn (ms from end of turn):")
    lines.append("  call      via      turn outcome          conf avg/min  first text  first audio  agent  sentences")
    for row in rows:
        conf = (f"{row.confidence_mean:.2f}/{row.confidence_min:.2f}"
                if row.confidence_mean is not None and row.confidence_min is not None else "-")
        lines.append(
            f"  {row.session_id[-8:]}  {'phone' if row.session_id in phone else 'browser':7}  {row.turn:>4} {row.outcome:16} {conf:>12}  {_ms(row.first_text_ms):>10}  "
            f"{_ms(row.first_audio_ms):>11}  {_ms(row.agent_ms):>5}  {row.sentences:>9}"
        )
    return "\n".join(lines)


def _stages(events: Sequence[ConversationEventRecord]) -> list[str]:
    calls = [e for e in events if e.event_type == "model_call"]
    tools = [e for e in events if e.event_type == "tool_call"]
    per_turn = Counter((e.session_id, e.turn) for e in calls)
    lines = [
        _spread("model call that chose a tool", [e.latency_ms for e in calls if e.outcome == "tool_use"]),
        _spread("model call that answered", [e.latency_ms for e in calls if e.outcome == "end_turn"]),
        _spread("tool", [e.latency_ms for e in tools if e.latency_ms is not None]),
    ]
    counts = sorted(per_turn.values())
    if counts:
        lines.append(f"  {'model calls per turn':38} median {statistics.median(counts):g}, max {counts[-1]}  (n={len(counts)})")
    failed = sum(e.outcome == "unavailable" for e in calls)
    if failed:
        lines.append(f"  {failed} model call(s) failed (unavailable)")
    return lines


def _confidence_line(name: str, values: list[float]) -> str:
    if not values:
        return f"  {name:38} -"
    return (
        f"  {name:38} median {statistics.median(values):.2f}, min {min(values):.2f}, "
        f"below 0.6: {sum(v < 0.6 for v in values)}  (n={len(values)})"
    )


def _ms(value: int | None) -> str:
    return "-" if value is None else str(value)


def load(db: Session, *, minutes: float | None, session_id: str | None) -> list[LiveTurnMetricRecord]:
    query = select(LiveTurnMetricRecord).order_by(LiveTurnMetricRecord.metric_id)
    if session_id:
        query = query.where(LiveTurnMetricRecord.session_id == session_id)
    if minutes is not None:
        query = query.where(LiveTurnMetricRecord.created_at >= datetime.now(timezone.utc) - timedelta(minutes=minutes))
    return list(db.scalars(query))


def phone_sessions(db: Session, rows: list[LiveTurnMetricRecord]) -> set[str]:
    """Which of these calls came by phone: a phone caller is verified first."""
    sessions = {row.session_id for row in rows}
    if not sessions:
        return set()
    query = (
        select(ConversationEventRecord.session_id)
        .where(ConversationEventRecord.session_id.in_(sessions))
        .where(ConversationEventRecord.event_type == "caller_verified")
    )
    return set(db.scalars(query))


def load_events(db: Session, rows: list[LiveTurnMetricRecord]) -> list[ConversationEventRecord]:
    """The model and tool calls of exactly these turns."""
    turns = {(row.session_id, row.turn) for row in rows}
    if not turns:
        return []
    query = (
        select(ConversationEventRecord)
        .where(ConversationEventRecord.session_id.in_({session for session, _ in turns}))
        .where(ConversationEventRecord.event_type.in_(("model_call", "tool_call")))
        .order_by(ConversationEventRecord.event_id)
    )
    return [event for event in db.scalars(query) if (event.session_id, event.turn) in turns]


def main(argv: list[str] | None = None, session_factory=SessionLocal) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=float, help="only turns from the last N minutes")
    parser.add_argument("--session", help="only this conversation (session id)")
    channel = parser.add_mutually_exclusive_group()
    channel.add_argument("--phone", action="store_true", help="only phone calls")
    channel.add_argument("--browser", action="store_true", help="only browser calls")
    args = parser.parse_args(argv)
    if session_factory is SessionLocal:
        require_current_schema()
    with session_factory() as db:
        rows = load(db, minutes=args.minutes, session_id=args.session)
        phone = phone_sessions(db, rows)
        if args.phone or args.browser:
            rows = [row for row in rows if (row.session_id in phone) == args.phone]
        print(summarize(rows, load_events(db, rows), phone))
    return 0


if __name__ == "__main__":
    sys.exit(main())
