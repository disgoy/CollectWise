"""Build the evidence report's data (evidence/data.json) from the project's real
result files. Free: reads local files only.

Sources: the agent eval (.claude/hillclimb/agent_conversations), the voice
checks (results-*.json in the git-ignored recordings folder), live-call
timings (live_turn_metrics, joined with conversation_events tool calls) and
the hand-curated evidence/facts.json.

Aggregates only: counts, rates, timings, phrase ids and the fact heard. No
transcript, reply, prompt or judge reasoning is copied, and check_private()
refuses to write output containing them.

Usage, from the repo root:
    python -m scripts.build_evidence
"""

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from evals.voice_checks.scoring import heard_fact, is_correct, threshold_table

REPO = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO / ".claude" / "hillclimb" / "agent_conversations"
VOICE_RESULTS = REPO / "evals" / "voice_checks" / "recordings"
FACTS = REPO / "evidence" / "facts.json"
OUTPUT = REPO / "evidence" / "data.json"

VERSIONS = ["baseline", "v1", "v2", "v3"]
VERSION_CHANGES = {
    "baseline": "First measured version (M3)",
    "v1": "Tools stop returning request IDs (D55)",
    "v2": "Spoken account names; account IDs never said (D58, D65)",
    "v3": "Payment expiry and fixed-reply notes (H3, H4)",
}
METRICS = ["all_pass", "outcome", "compliance", "quality", "payments", "handoff"]
JUDGE_CRITERIA = ["grounded", "no_paid_claim", "no_internal_ids", "plain_language", "no_overpromise",
                  "responsive", "case_expectations"]

# Never allowed anywhere in the output: these keys carry words people or the
# model wrote.
PRIVATE_KEYS = {"transcript", "text", "reply", "prompt", "reason", "explanation", "messages", "judge_notes"}


def _rate(values: list[float]) -> float | None:
    return round(100 * sum(values) / len(values), 1) if values else None


def eval_version(rows: list[dict]) -> dict:
    """Pass rates per metric and judge criterion, and which cases failed."""
    rates = {metric: _rate([r["grade"][metric] for r in rows]) for metric in METRICS}
    criteria = {}
    for criterion in JUDGE_CRITERIA:
        verdicts = [
            r["meta"]["judge_verdicts"][criterion]["verdict"]
            for r in rows
            if criterion in r.get("meta", {}).get("judge_verdicts", {})
        ]
        applicable = [v for v in verdicts if v != "not_applicable"]
        criteria[criterion] = {
            "pass_rate": _rate([v == "pass" for v in applicable]),
            "fails": sum(v == "fail" for v in applicable),
            "applicable": len(applicable),
        }
    failures = []
    for r in rows:
        failed_metrics = [m for m in METRICS[1:] if r["grade"][m] < 1]
        if failed_metrics:
            verdicts = r.get("meta", {}).get("judge_verdicts", {})
            failures.append({
                "case": r["prompt_id"],
                "rep": r["rep"],
                "failed": failed_metrics,
                "judge_criteria_failed": sorted(c for c, v in verdicts.items() if v.get("verdict") == "fail"),
            })
    latencies = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
    return {
        "conversations": len(rows),
        "rates": rates,
        "judge_criteria": criteria,
        "failures": failures,
        "failure_classes": dict(Counter(m for f in failures for m in f["failed"])),
        "turn_latency_s_median": round(statistics.median(latencies), 2) if latencies else None,
        "tool_calls": sum(r.get("tool_calls", 0) for r in rows),
    }


def load_eval(eval_dir: Path = EVAL_DIR) -> dict:
    versions = []
    for name in VERSIONS:
        path = eval_dir / name / "results.jsonl"
        if path.exists():
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            versions.append({"version": name, "change": VERSION_CHANGES.get(name, ""), **eval_version(rows)})
    grader = {}
    for name in ("oracle", "null"):
        path = eval_dir / "judge_checks" / name / "results.jsonl"
        if path.exists():
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            grader[name] = {metric: _rate([r["grade"][metric] for r in rows]) for metric in METRICS}
    return {"versions": versions, "grader_checks": grader}


def _condition(row: dict) -> str:
    voice = "your voice" if row["source"] == "you" else "synthetic voices"
    return f"{voice}, " + ("clean" if row["noise"] is None else f"noise {row['noise']} dB")


def load_voice(paths: list[Path]) -> dict:
    rows = []
    for path in paths:
        for row in json.loads(path.read_text())["rows"]:
            # Re-scored with today's scorer, so older result files count the
            # same way (and show 20, not 2E+1). The transcript is only read here.
            rows.append({
                **{k: v for k, v in row.items() if k != "transcript"},
                "heard": heard_fact(row["kind"], row["transcript"]),
                "correct": is_correct(row["kind"], row["expect"], row["transcript"]),
            })
    if not rows:
        return {}
    by_condition = defaultdict(list)
    for row in rows:
        by_condition[_condition(row)].append(row)
    conditions = []
    for name in sorted(by_condition, key=lambda c: (c.startswith("your"), "clean" not in c, c)):
        subset = by_condition[name]
        conditions.append({
            "condition": name,
            "correct": sum(r["correct"] for r in subset),
            "total": len(subset),
            "by_kind": {
                kind: [sum(r["correct"] for r in subset if r["kind"] == kind), sum(r["kind"] == kind for r in subset)]
                for kind in ("amount", "confirm", "label", "handoff")
                if any(r["kind"] == kind for r in subset)
            },
        })

    def spread(values: list[float]) -> dict | None:
        values = sorted(v for v in values if v is not None)
        if not values:
            return None
        return {"min": round(values[0], 2), "median": round(statistics.median(values), 2),
                "max": round(values[-1], 2), "n": len(values)}

    mishearings = [
        {
            "phrase": r["phrase_id"],
            "voice": "your voice" if r["source"] == "you" else r["source"].replace("aura-2-", "").replace("-en", ""),
            "noise_db": r["noise"],
            "expected": r["expect"],
            "heard": r["heard"],
            "confidence": None if r["confidence"] is None else round(r["confidence"], 2),
        }
        for r in sorted(rows, key=lambda r: -(r["confidence"] or 0))
        if not r["correct"]
    ]
    return {
        "clips": len(rows),
        "correct": sum(r["correct"] for r in rows),
        "conditions": conditions,
        "confidence_when_right": spread([r["confidence"] for r in rows if r["correct"]]),
        "confidence_when_wrong": spread([r["confidence"] for r in rows if not r["correct"]]),
        "thresholds": threshold_table(rows),
        "mishearings": mishearings,
    }


def load_live(db: Session) -> dict:
    from backend.app.db.models import ConversationEventRecord, LiveTurnMetricRecord

    metrics = list(db.scalars(select(LiveTurnMetricRecord).order_by(LiveTurnMetricRecord.metric_id)))
    tools = Counter(
        (e.session_id, e.turn)
        for e in db.scalars(select(ConversationEventRecord).where(ConversationEventRecord.event_type == "tool_call"))
    )
    replies = [m for m in metrics if m.outcome == "reply" and m.first_audio_ms is not None]

    def median(values: list[int]) -> int | None:
        return int(statistics.median(values)) if values else None

    call_number = {session: n for n, session in enumerate(dict.fromkeys(m.session_id for m in metrics), start=1)}
    with_lookup = [m for m in replies if tools[(m.session_id, m.turn)] > 0]
    without = [m for m in replies if tools[(m.session_id, m.turn)] == 0]
    return {
        "calls": len({m.session_id for m in metrics}),
        "turns": len(metrics),
        # A list, not a dict: outcome codes such as "reply" are data, not field names.
        "outcomes": [{"outcome": code, "turns": n} for code, n in Counter(m.outcome for m in metrics).most_common()],
        "first_audio_ms": sorted(m.first_audio_ms for m in replies),
        "median_ms": {
            "first_text": median([m.first_text_ms for m in replies if m.first_text_ms is not None]),
            "first_audio": median([m.first_audio_ms for m in replies]),
            "speech": median([m.first_audio_ms - m.first_text_ms for m in replies if m.first_text_ms is not None]),
        },
        "first_audio_median_ms_by_lookups": {
            "no lookup": median([m.first_audio_ms for m in without]),
            "with lookups": median([m.first_audio_ms for m in with_lookup]),
        },
        "turns_by_lookups": {"no lookup": len(without), "with lookups": len(with_lookup)},
        "lowest_word_confidence": sorted(round(m.confidence_min, 2) for m in metrics if m.confidence_min is not None),
        # Per turn, for the page to work out its own figures: no ids, no words.
        "turn_rows": [
            {"call": call_number[m.session_id], "turn": m.turn, "first_text_ms": m.first_text_ms,
             "first_audio_ms": m.first_audio_ms, "lookups": tools[(m.session_id, m.turn)]}
            for m in replies
        ],
    }


def check_private(value, path: str = "") -> None:
    """Refuse output that could hold anyone's words: a private key anywhere,
    or a long string (aggregates never need one) outside the curated facts."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PRIVATE_KEYS:
                raise ValueError(f"private field {path}.{key} in the evidence data")
            check_private(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_private(item, f"{path}[{index}]")
    elif isinstance(value, str) and len(value) > 160 and not path.startswith(".facts"):
        raise ValueError(f"long text at {path}: aggregates only")


def build(db: Session, *, eval_dir: Path = EVAL_DIR, voice_paths: list[Path] | None = None,
          facts_path: Path = FACTS) -> dict:
    if voice_paths is None:
        voice_paths = sorted(VOICE_RESULTS.glob("results-*.json"))
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eval": load_eval(eval_dir),
        "voice": load_voice(voice_paths),
        "live": load_live(db),
        "facts": json.loads(facts_path.read_text()),
    }
    check_private(data)
    return data


def datasets(data: dict) -> dict[str, list[dict]]:
    """The report's data as one table (a list of rows) per chart or table on
    the page, so every figure there comes from a named file."""
    facts = data["facts"]
    versions = data["eval"]["versions"]
    voice = data["voice"]
    return {
        "eval_versions": [{"version": v["version"], "change": v["change"], "conversations": v["conversations"],
                           **v["rates"]} for v in versions],
        "judge_fails": [{"version": v["version"], "criterion": c, "fails": stats["fails"], "applicable": stats["applicable"]}
                        for v in versions for c, stats in v["judge_criteria"].items()],
        "grader_checks": [{"agent": agent, **rates} for agent, rates in data["eval"]["grader_checks"].items()],
        "prompt_leaks": [{"leak": name.replace("_", " "), **{k: v for k, v in leak.items()}}
                         for name, leak in facts["prompt_leaks"].items() if not name.startswith("_")],
        "voice_conditions": [{"condition": c["condition"], "correct": c["correct"], "total": c["total"]}
                             for c in voice.get("conditions", [])],
        "voice_confidence": [{"group": group, **voice[key]}
                             for group, key in (("heard right", "confidence_when_right"), ("heard wrong", "confidence_when_wrong"))
                             if voice.get(key)],
        "thresholds": voice.get("thresholds", []),
        # `clip` tells rows apart: the same phrase can be misheard at several noise levels.
        "mishearings": [{"clip": f"{m['phrase']} · {m['voice']} · " + ("clean" if m["noise_db"] is None else f"{m['noise_db']} dB"),
                         **m} for m in voice.get("mishearings", [])],
        "live_turns": data["live"]["turn_rows"],
        "m4_stages": [{"stage": stage.replace("_s", "").replace("_", " "), "low_s": low, "high_s": high}
                      for stage, (low, high) in ((k, v) for k, v in facts["turn_based_voice_m4"].items() if not k.startswith("_"))],
        "tests_over_time": facts["tests_over_time"],
        "mutation_steps": facts["mutation_checks"]["steps"],
        "notes": ([{"kind": "rule", "ref": "", "item": rule, "why": ""} for rule in facts["safety_rules_in_code"]]
                  + [{"kind": "decision", "ref": d["id"], "item": d["decision"], "why": d["why"]} for d in facts["decisions"]]
                  + [{"kind": "limit", "ref": "", "item": limit, "why": ""} for limit in facts["limits"]]),
        "headline_facts": [{"measure": "decisions logged", "value": facts["decisions_logged"]}],
    }


def write_datasets(data: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, rows in datasets(data).items():
        if name != "notes":  # the curated notes are prose by design
            check_private({name: rows})
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None, session_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    if session_factory is None:
        from backend.app.db.session import SessionLocal, create_tables

        create_tables()
        session_factory = SessionLocal
    with session_factory() as db:
        data = build(db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    write_datasets(data, args.out.parent / "datasets")
    versions = ", ".join(f"{v['version']} {v['rates']['all_pass']}%" for v in data["eval"]["versions"])
    print(f"Wrote {args.out.relative_to(REPO) if args.out.is_relative_to(REPO) else args.out}: eval {versions}; "
          f"{data['voice'].get('clips', 0)} voice clips; {data['live']['turns']} live turns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
