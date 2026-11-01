"""The live evidence page (M6.7): live speed from this server's database, the
rest from result files or the committed snapshot (labelled), aggregates
only, cached, and served like the live-call page."""

import json
from datetime import datetime, timezone

import pytest

from backend.app.api import evidence_page
from backend.app.db import models
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.services import evidence
from backend.app.services.evidence import FACTS, live_evidence
from backend.tests.test_build_evidence import eval_row, voice_row, write_eval

SNAPSHOT = {
    "generated_at": "2026-10-31T18:16:56+00:00",
    "eval": {"versions": [], "grader_checks": {"oracle": {"all_pass": 100.0}}},
    "voice": {"conditions": [{"condition": "synthetic voices, clean", "correct": 44, "total": 45}]},
}


def live_turn(session: str, turn: int, first_audio: int = 2900) -> models.LiveTurnMetricRecord:
    return models.LiveTurnMetricRecord(
        session_id=session, customer_id="cust_PRIVATE_MARKER", turn=turn, outcome="reply", confidence_mean=0.95,
        confidence_min=0.9, transcript_chars=20, reply_chars=40, sentences=1, first_text_ms=first_audio - 300,
        first_audio_ms=first_audio, agent_ms=first_audio, audio_ok=True)


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps(SNAPSHOT))
    return path


@pytest.fixture(autouse=True)
def fresh_cache():
    evidence_page.clear_cache()
    yield
    evidence_page.clear_cache()


# --- the data -------------------------------------------------------------------------


def test_live_speed_comes_from_this_database_and_the_rest_from_files(db_session, tmp_path, snapshot) -> None:
    write_eval(tmp_path / "eval", "baseline", [eval_row("a", 0), eval_row("a", 1, quality=0.0)])
    db_session.add_all([live_turn("sess_1", 1), live_turn("sess_1", 2, 1800)])
    db_session.commit()

    payload = live_evidence(db_session, eval_dir=tmp_path / "eval", voice_dir=tmp_path / "no-voice",
                            snapshot_path=snapshot)

    assert payload["sources"] == {"eval": "result files", "voice": "snapshot of 2026-10-31",
                                  "live": "this server", "facts": "evidence/facts.json"}
    tables = payload["tables"]
    assert [(t["call"], t["turn"], t["first_audio_ms"]) for t in tables["live_turns"]] == [(1, 1, 2900), (1, 2, 1800)]
    assert tables["eval_versions"][0]["all_pass"] == 50.0  # from the result file
    assert tables["voice_conditions"] == SNAPSHOT["voice"]["conditions"]  # from the snapshot
    assert tables["tests_over_time"] == json.loads(FACTS.read_text())["tests_over_time"]
    assert "cust_PRIVATE_MARKER" not in json.dumps(payload) and "sess_1" not in json.dumps(payload)


def test_without_result_files_everything_but_live_speed_is_the_snapshot(db_session, tmp_path, snapshot) -> None:
    payload = live_evidence(db_session, eval_dir=tmp_path / "none", voice_dir=tmp_path / "none",
                            snapshot_path=snapshot)

    assert (payload["sources"]["eval"], payload["sources"]["voice"]) == ("snapshot of 2026-10-31",) * 2
    assert payload["tables"]["grader_checks"] == [{"agent": "oracle", "all_pass": 100.0}]
    assert payload["tables"]["live_turns"] == []


def test_voice_results_on_this_machine_win_over_the_snapshot(db_session, tmp_path, snapshot) -> None:
    voice = tmp_path / "voice"
    voice.mkdir()
    (voice / "results-1.json").write_text(json.dumps({"rows": [
        voice_row("you", None, "confirm_yes", "confirm", "yes", "Yes please", 0.95)]}))

    payload = live_evidence(db_session, eval_dir=tmp_path / "none", voice_dir=voice, snapshot_path=snapshot)

    assert payload["sources"]["voice"] == "result files"
    assert payload["tables"]["voice_conditions"] == [{"condition": "your voice, clean", "correct": 1, "total": 1}]


def test_no_snapshot_and_no_files_still_answers(db_session, tmp_path) -> None:
    payload = live_evidence(db_session, eval_dir=tmp_path / "none", voice_dir=tmp_path / "none",
                            snapshot_path=tmp_path / "missing.json")

    assert payload["sources"]["eval"] == "none"
    assert payload["tables"]["eval_versions"] == [] and payload["tables"]["voice_conditions"] == []


def test_anything_that_could_hold_words_is_refused(db_session, tmp_path, snapshot, monkeypatch) -> None:
    real = evidence.load_live

    def leaky(db):
        live = real(db)
        live["turn_rows"] = [{"call": 1, "transcript": "pay 120 from savings"}]
        return live

    monkeypatch.setattr(evidence, "load_live", leaky)

    with pytest.raises(ValueError, match="private field"):
        live_evidence(db_session, eval_dir=tmp_path / "none", voice_dir=tmp_path / "none", snapshot_path=snapshot)


# --- the endpoint --------------------------------------------------------------------------


def test_a_new_live_turn_shows_once_the_cache_expires(client, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(evidence_page, "_clock", lambda: clock[0])
    db = next(app.dependency_overrides[get_db]())

    def turns() -> int:
        return len(client.get("/evidence/data").json()["tables"]["live_turns"])

    assert turns() == 0
    db.add(live_turn("sess_new", 1))
    db.commit()
    clock[0] += evidence_page.CACHE_S - 1
    assert turns() == 0  # still the cached answer
    clock[0] += 1
    assert turns() == 1
    assert evidence_page.CACHE_S == 30.0


def test_the_data_is_open_and_says_when_it_was_built(client) -> None:
    response = client.get("/evidence/data", headers={"X-Admin-Key": ""})

    assert response.status_code == 200
    built = datetime.fromisoformat(response.json()["generated_at"])
    assert (datetime.now(timezone.utc) - built).total_seconds() < 60


# --- the page --------------------------------------------------------------------------------


def test_the_page_is_served_like_the_live_call_page(client) -> None:
    response = client.get("/evidence")

    assert response.status_code == 200
    assert "<title>CollectWise evidence</title>" in response.text
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "default-src 'none'" in policy
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/evidence/static/evidence-app.mjs").status_code == 200


def test_the_two_pages_link_to_each_other(client) -> None:
    assert 'href="/evidence"' in client.get("/live").text
    assert 'href="/live"' in client.get("/evidence").text
