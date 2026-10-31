"""The evidence report's data builder: right aggregates, and nobody's words."""

import json
from pathlib import Path

import pytest

from backend.app.db import models
from scripts import build_evidence
from scripts.build_evidence import build, check_private, eval_version, load_eval, load_live, load_voice

MARKER = "PRIVATE-WORDS-MARKER"


def eval_row(case: str, rep: int, *, quality: float = 1.0, verdicts=None) -> dict:
    grade = {"all_pass": quality, "outcome": 1.0, "compliance": 1.0, "quality": quality, "payments": 1.0, "handoff": 1.0}
    verdicts = verdicts or {"grounded": "pass", "no_paid_claim": "not_applicable"}
    return {
        "prompt_id": case, "rep": rep, "prompt": f"Caller: {MARKER}", "grade": grade, "latency_s": 2.0,
        "tool_calls": 2, "explanation": {"quality": MARKER},
        "meta": {"judge_verdicts": {c: {"verdict": v, "reason": MARKER} for c, v in verdicts.items()}},
    }


def write_eval(root: Path, version: str, rows: list[dict]) -> None:
    (root / version).mkdir(parents=True)
    (root / version / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def voice_row(source: str, noise, phrase: str, kind: str, expect: str, transcript: str, confidence) -> dict:
    # A stale stored score, to show the builder scores again.
    return {"source": source, "noise": noise, "phrase_id": phrase, "kind": kind, "expect": expect,
            "transcript": transcript, "confidence": confidence, "heard": "stale", "correct": True}


def test_an_eval_version_gives_rates_criteria_and_failures() -> None:
    rows = [
        eval_row("pay_happy_path", 0),
        eval_row("pay_happy_path", 1, quality=0.0, verdicts={"grounded": "pass", "no_internal_ids": "fail"}),
        eval_row("facts", 0, verdicts={"grounded": "fail"}),
        eval_row("facts", 1),
    ]

    result = eval_version(rows)

    assert result["rates"]["all_pass"] == 75.0
    assert result["judge_criteria"]["grounded"] == {"pass_rate": 75.0, "fails": 1, "applicable": 4}
    assert result["judge_criteria"]["no_paid_claim"]["applicable"] == 0  # not-applicable verdicts don't count
    assert result["failures"] == [
        {"case": "pay_happy_path", "rep": 1, "failed": ["quality"], "judge_criteria_failed": ["no_internal_ids"]},
    ]
    assert result["failure_classes"] == {"quality": 1}
    assert result["tool_calls"] == 8


def test_eval_versions_load_in_order_with_the_grader_checks(tmp_path) -> None:
    write_eval(tmp_path, "v1", [eval_row("a", 0)])
    write_eval(tmp_path, "baseline", [eval_row("a", 0, quality=0.0)])
    write_eval(tmp_path / "judge_checks", "oracle", [eval_row("a", 0)])

    loaded = load_eval(tmp_path)

    assert [v["version"] for v in loaded["versions"]] == ["baseline", "v1"]
    assert loaded["versions"][1]["change"].startswith("Tools stop returning request IDs")
    assert loaded["grader_checks"] == {"oracle": {m: 100.0 for m in build_evidence.METRICS}}


def test_voice_clips_are_scored_again_and_grouped_by_condition(tmp_path) -> None:
    path = tmp_path / "results-x.json"
    path.write_text(json.dumps({"rows": [
        voice_row("you", None, "amount_a_hundred_and_twenty", "amount", "120", f"20 dollars {MARKER}", 1.0),
        voice_row("you", None, "confirm_yes", "confirm", "yes", "Yes please", 0.95),
        voice_row("aura-2-draco-en", 10, "amount_thirty", "amount", "30", "Thirty", 0.52),
        voice_row("aura-2-draco-en", None, "amount_thirty", "amount", "30", "30 dollars", 0.99),
    ]}))

    voice = load_voice([path])

    assert (voice["clips"], voice["correct"]) == (4, 2)
    assert [(c["condition"], c["correct"], c["total"]) for c in voice["conditions"]] == [
        ("synthetic voices, clean", 1, 1), ("synthetic voices, noise 10 dB", 0, 1), ("your voice, clean", 1, 2),
    ]
    assert voice["mishearings"][0] == {
        "phrase": "amount_a_hundred_and_twenty", "voice": "your voice", "noise_db": None,
        "expected": "120", "heard": "20", "confidence": 1.0,
    }
    assert voice["mishearings"][1]["voice"] == "draco"
    assert voice["confidence_when_wrong"] == {"min": 0.52, "median": 0.76, "max": 1.0, "n": 2}
    assert {e["threshold"]: e["passed_wrong"] for e in voice["thresholds"]}[0.6] == 1
    assert load_voice([]) == {}


def test_live_speed_is_split_by_whether_the_turn_used_a_tool(db_session) -> None:
    def metric(session: str, turn: int, first_audio: int, first_text: int) -> models.LiveTurnMetricRecord:
        return models.LiveTurnMetricRecord(
            session_id=session, customer_id="cust_demo", turn=turn, outcome="reply", confidence_mean=0.9,
            confidence_min=0.61, transcript_chars=10, reply_chars=40, sentences=1, first_text_ms=first_text,
            first_audio_ms=first_audio, agent_ms=first_audio, audio_ok=True,
        )

    db_session.add_all([
        metric("sess_a", 1, 3200, 2900), metric("sess_a", 2, 1600, 800), metric("sess_b", 1, 3000, 2700),
        models.ConversationEventRecord(session_id="sess_a", customer_id="cust_demo", turn=1, event_type="tool_call",
                                       name="list_accounts", outcome="ok"),
        models.ConversationEventRecord(session_id="sess_b", customer_id="cust_demo", turn=1, event_type="tool_call",
                                       name="get_account_summary", outcome="ok"),
    ])
    db_session.commit()

    live = load_live(db_session)

    assert (live["calls"], live["turns"]) == (2, 3)
    assert live["first_audio_median_ms_by_lookups"] == {"no lookup": 1600, "with lookups": 3100}
    assert live["turns_by_lookups"] == {"no lookup": 1, "with lookups": 2}
    assert live["median_ms"] == {"first_text": 2700, "first_audio": 3000, "speech": 300}
    assert live["outcomes"] == [{"outcome": "reply", "turns": 3}]


def test_the_privacy_check_refuses_words() -> None:
    with pytest.raises(ValueError, match="private field"):
        check_private({"voice": {"mishearings": [{"transcript": "pay 20"}]}})
    with pytest.raises(ValueError, match="long text"):
        check_private({"eval": {"note": "x" * 161}})
    check_private({"facts": {"limits": ["x" * 400]}})  # the curated facts may hold prose


def test_a_full_build_holds_no_transcript_prompt_or_judge_reasoning(tmp_path, db_session) -> None:
    write_eval(tmp_path / "eval", "baseline", [eval_row("a", 0, quality=0.0, verdicts={"grounded": "fail"})])
    voice = tmp_path / "results-1.json"
    voice.write_text(json.dumps({"rows": [voice_row("you", None, "confirm_no", "confirm", "no", f"Know {MARKER}", 0.7)]}))
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps({"limits": ["Synthetic data only."]}))

    data = build(db_session, eval_dir=tmp_path / "eval", voice_paths=[voice], facts_path=facts)

    dumped = json.dumps(data)
    assert MARKER not in dumped
    assert data["eval"]["versions"][0]["failures"][0]["judge_criteria_failed"] == ["grounded"]
    assert data["voice"]["mishearings"][0]["heard"] == "none"


def test_the_command_writes_the_data_file(tmp_path, db_session, monkeypatch, capsys) -> None:
    monkeypatch.setattr(build_evidence, "EVAL_DIR", tmp_path / "missing")
    monkeypatch.setattr(build_evidence, "VOICE_RESULTS", tmp_path)
    out = tmp_path / "data.json"

    class Borrowed:
        def __enter__(self):
            return db_session

        def __exit__(self, *exc):
            return None

    assert build_evidence.main(["--out", str(out)], session_factory=Borrowed) == 0

    written = json.loads(out.read_text())
    assert set(written) == {"generated_at", "eval", "voice", "live", "facts"}
    assert "0 live turns" in capsys.readouterr().out


def test_the_committed_facts_are_public_safe() -> None:
    facts = json.loads(build_evidence.FACTS.read_text())
    check_private({"facts": facts})
    steps = facts["mutation_checks"]["steps"]
    assert all(s["first_time"] + s["test_gaps"] <= s["bugs"] for s in steps)
    tests = [s["tests"] for s in facts["tests_over_time"]]
    assert tests == sorted(tests)  # the suite only grew
