"""What the operations MCP server reports (M6.6): numbers, codes and short
call references from the database and the eval results. Never customer ids,
caller words, replies or amounts; the server checks every result with
build_evidence.check_private before returning it.
"""

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.db.models import ConversationEventRecord, LiveTurnMetricRecord
from backend.app.db.session import head_revision, schema_revision
from scripts.live_report import percentile

CALL_REF_CHARS = 8  # the end of a session id: enough to tell calls apart


def spread(values: list[float]) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"p50": None, "p95": None, "max": None, "n": 0}
    return {"p50": statistics.median(values), "p95": percentile(values, 0.95), "max": max(values), "n": len(values)}


def counts(values, key: str) -> list[dict]:
    """[{key: value, "count": n}], most common first. A list, so codes such as
    "reply" stay data and are never field names."""
    return [{key: value, "count": n} for value, n in Counter(values).most_common()]


def call_ref(session_id: str) -> str:
    return session_id[-CALL_REF_CHARS:]


def service_health(db: Session) -> dict:
    revision = schema_revision(db.connection())
    return {"database": "reachable", "schema_revision": revision, "latest_revision": head_revision(),
            "schema_current": revision == head_revision()}


def live_call_speed(db: Session, *, minutes: int, now: datetime) -> dict:
    since = now - timedelta(minutes=minutes)
    rows = list(db.scalars(select(LiveTurnMetricRecord).where(LiveTurnMetricRecord.created_at >= since)))
    replies = [r for r in rows if r.outcome == "reply"]
    turns = {(r.session_id, r.turn) for r in rows}
    events = [e for e in db.scalars(select(ConversationEventRecord).where(
        ConversationEventRecord.created_at >= since,
        ConversationEventRecord.event_type.in_(("model_call", "tool_call")))) if (e.session_id, e.turn) in turns]
    calls = [e for e in events if e.event_type == "model_call"]
    return {
        "window_minutes": minutes,
        "calls": len({r.session_id for r in rows}),
        "turns": len(rows),
        "outcomes": counts((r.outcome for r in rows), "outcome"),
        "first_text_ms": spread([r.first_text_ms for r in replies]),
        "first_audio_ms": spread([r.first_audio_ms for r in replies]),
        "speech_ms": spread([r.first_audio_ms - r.first_text_ms for r in replies
                             if r.first_audio_ms is not None and r.first_text_ms is not None]),
        "model_call_choosing_a_tool_ms": spread([e.latency_ms for e in calls if e.outcome == "tool_use"]),
        "model_call_answering_ms": spread([e.latency_ms for e in calls if e.outcome == "end_turn"]),
        "tool_ms": spread([e.latency_ms for e in events if e.event_type == "tool_call"]),
        "word_confidence": {
            "average_median": statistics.median(m) if (m := [r.confidence_mean for r in rows
                                                              if r.confidence_mean is not None]) else None,
            "turns_below_threshold": sum(r.confidence_mean is not None and r.confidence_mean < config.LIVE_MIN_CONFIDENCE
                                         for r in rows),
            "threshold": config.LIVE_MIN_CONFIDENCE,
        },
    }


def recent_calls(db: Session, *, limit: int) -> list[dict]:
    rows = db.scalars(select(LiveTurnMetricRecord).order_by(LiveTurnMetricRecord.metric_id)).all()
    by_call: dict[str, list[LiveTurnMetricRecord]] = defaultdict(list)
    for row in rows:
        by_call[row.session_id].append(row)
    latest = sorted(by_call.values(), key=lambda turns: turns[-1].metric_id, reverse=True)[:limit]
    return [{
        "call": call_ref(turns[0].session_id),
        "started": turns[0].created_at.strftime("%Y-%m-%dT%H:%MZ"),
        "turns": len(turns),
        "outcomes": counts((t.outcome for t in turns), "outcome"),
        "first_audio_ms_median": spread([t.first_audio_ms for t in turns if t.outcome == "reply"])["p50"],
        "text_only_replies": sum(t.outcome == "reply" and not t.audio_ok for t in turns),
    } for turns in latest]


def conversation_outcomes(db: Session, *, minutes: int, now: datetime) -> dict:
    events = db.scalars(select(ConversationEventRecord).where(
        ConversationEventRecord.created_at >= now - timedelta(minutes=minutes))).all()
    tools = [e for e in events if e.event_type == "tool_call"]
    return {
        "window_minutes": minutes,
        "turn_outcomes": counts((e.outcome for e in events if e.event_type == "turn_completed"), "outcome"),
        "model_calls": sum(e.event_type == "model_call" for e in events),
        "model_calls_failed": sum(e.event_type == "model_call" and e.outcome == "unavailable" for e in events),
        "tool_errors": [{"tool": tool, "error": error, "count": n} for (tool, error), n in
                        Counter((e.name, e.outcome) for e in tools if e.outcome != "ok").most_common()],
        # "cause", not "reason": the privacy check treats "reason" as a field
        # that could hold words (judge reasoning).
        "payments_cancelled": counts((e.outcome for e in events if e.event_type == "payment_cancelled"), "cause"),
        "handoffs": sum(e.name == "handoff_to_human" and e.outcome == "ok" for e in tools)
                    + sum(e.event_type == "turn_completed" and e.outcome in ("turn_limit", "stuck") for e in events),
    }


def eval_results() -> dict:
    from scripts.build_evidence import EVAL_DIR, load_eval

    loaded = load_eval(EVAL_DIR) if EVAL_DIR.is_dir() else {}
    return {
        "versions": [{
            "version": v["version"], "change": v["change"], "conversations": v["conversations"],
            "rates": v["rates"],
            "judge_failures": [{"criterion": c, "fails": s["fails"], "applicable": s["applicable"]}
                               for c, s in v["judge_criteria"].items() if s["fails"]],
        } for v in loaded.get("versions", [])],
        "grader_checks": loaded.get("grader_checks", {}),
        "note": None if loaded.get("versions") else "No eval results on this machine.",
    }
