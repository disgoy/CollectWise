"""Step 4.7's measuring tools, checked offline: if these are wrong, the paid
voice-check numbers can't be trusted."""

import json
import math
import random
import struct
from array import array
from decimal import Decimal

import pytest

from backend.app.voice.audio import AudioClip
from evals.voice_checks import run_voice_checks
from evals.voice_checks.noise import add_noise
from evals.voice_checks.scoring import (
    amounts_in,
    classify_confirmation,
    heard_fact,
    is_correct,
    summarize,
    threshold_table,
)

PHRASES = json.loads(run_voice_checks.PHRASES_PATH.read_text())["phrases"]


# --- the phrase file -----------------------------------------------------------------


def test_phrases_are_well_formed() -> None:
    assert len({p["id"] for p in PHRASES}) == len(PHRASES)
    for phrase in PHRASES:
        assert phrase["kind"] in {"amount", "confirm", "label", "handoff"}


@pytest.mark.parametrize("phrase", PHRASES, ids=[p["id"] for p in PHRASES])
def test_each_phrase_scores_correct_on_its_own_text_written_as_digits(phrase) -> None:
    # What a perfect, smart-formatted transcript would say.
    perfect = {
        "amount_fifteen": "Please pay $15 from my main account.",
        "amount_fifty": "Please pay $50 from my main account.",
        "amount_thirteen": "Pay $13, please.",
        "amount_thirty": "Pay $30, please.",
        "amount_dollars_and_cents": "Can you pay $125.50?",
        "amount_a_hundred_and_twenty": "I'd like to pay a 120.",
        "amount_two_thousand_rupees": "Pay 2,000 rupees from my savings account.",
    }.get(phrase["id"], phrase["text"])

    assert is_correct(phrase["kind"], phrase["expect"], perfect)


# --- amounts ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "amounts"),
    [("Pay $15.", ["15"]), ("pay 2,000 rupees", ["2000"]), ("$125.50 now", ["125.50"]), ("no numbers", [])],
)
def test_amounts_in(text, amounts) -> None:
    assert amounts_in(text) == [Decimal(a) for a in amounts]


@pytest.mark.parametrize(
    ("text", "correct"),
    [
        ("Please pay $15 from my main account.", True),
        ("Please pay 15.00 dollars.", True),
        ("Please pay $50 from my main account.", False),  # the dangerous mishearing
        ("Please pay fifteen dollars.", False),  # no digits: the amount wasn't captured
        ("Pay 15 or 50.", False),  # two numbers: ambiguous
    ],
)
def test_amount_scoring(text, correct) -> None:
    assert is_correct("amount", "15", text) is correct


# --- confirmations, labels, handoff ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "meaning"),
    [
        ("Yes.", "yes"), ("Yeah, go ahead.", "yes"), ("Sure.", "yes"),
        ("No.", "no"), ("Nope, cancel that.", "no"),
        ("Hmm, maybe. I'm not sure.", "unsure"), ("I'm not sure.", "unsure"),
        ("", "none"), ("Banana.", "none"),
    ],
)
def test_confirmation_meaning(text, meaning) -> None:
    assert classify_confirmation(text) == meaning


@pytest.mark.parametrize(
    ("text", "heard"),
    [("pay a and 20", "20"), ("$125.50", "125.5"), ("2,000 rupees", "2000"), ("$100", "100"), ("15 or 50", "15,50")],
)
def test_heard_amounts_read_as_plain_numbers(text, heard) -> None:
    assert heard_fact("amount", text) == heard


def test_label_and_handoff() -> None:
    assert heard_fact("label", "Use my savings account.") == "savings"
    assert heard_fact("label", "Use my saving account.") == "none"
    assert is_correct("label", "current", "From my current account, please.")
    assert is_correct("handoff", "handoff", "Can I talk to a real person?")
    assert not is_correct("handoff", "handoff", "Can I talk to a real parson?")


def test_unknown_kind_is_an_error() -> None:
    with pytest.raises(ValueError):
        heard_fact("colour", "blue")


# --- noise ---------------------------------------------------------------------------


def tone(seconds: float = 0.5, rate: int = 8_000) -> AudioClip:
    count = int(rate * seconds)
    return AudioClip(
        data=b"".join(struct.pack("<h", int(8_000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(count)),
        sample_rate=rate,
    )


def samples(clip: AudioClip) -> array:
    values = array("h")
    values.frombytes(clip.data)
    return values


def test_no_noise_returns_the_clip() -> None:
    clip = tone()
    assert add_noise(clip, None, random.Random(1)) is clip


@pytest.mark.parametrize("snr_db", [20, 10, 5])
def test_noise_level_matches_the_requested_ratio(snr_db) -> None:
    clean = tone(1.0)

    noisy = add_noise(clean, snr_db, random.Random(7))

    signal = samples(clean)
    noise = [n - s for n, s in zip(samples(noisy), signal)]
    measured = 10 * math.log10(sum(s * s for s in signal) / sum(n * n for n in noise))
    assert measured == pytest.approx(snr_db, abs=0.5)
    assert noisy.sample_rate == clean.sample_rate and noisy.duration_ms == clean.duration_ms


def test_noise_is_repeatable_with_the_same_seed() -> None:
    clip = tone()
    assert add_noise(clip, 10, random.Random(3)) == add_noise(clip, 10, random.Random(3))
    assert add_noise(clip, 10, random.Random(3)) != add_noise(clip, 10, random.Random(4))


def test_loud_noise_is_clipped_to_valid_samples() -> None:
    loud = AudioClip(data=struct.pack("<4h", 32_000, -32_000, 32_000, -32_000), sample_rate=8_000)

    noisy = samples(add_noise(loud, -20, random.Random(1)))

    assert all(-32_768 <= s <= 32_767 for s in noisy)


def test_silence_stays_silent() -> None:
    silent = AudioClip(data=bytes(1_600), sample_rate=8_000)
    assert add_noise(silent, 10, random.Random(1)) == silent


# --- summary and threshold ------------------------------------------------------------


def row(correct: bool, confidence: float | None, noise=None, source="aura-2-thalia-en", kind="amount"):
    return {
        "source": source, "noise": noise, "phrase_id": "p", "kind": kind, "transcript": "t",
        "confidence": confidence, "correct": correct, "heard": "x",
    }


def test_threshold_table_counts_both_kinds_of_mistake() -> None:
    rows = [row(True, 0.95), row(True, 0.45), row(False, 0.92), row(False, 0.35), row(False, None)]

    at = {t["threshold"]: t for t in threshold_table(rows)}

    assert (at[0.5]["blocked_correct"], at[0.5]["passed_wrong"]) == (1, 2)  # 0.45 blocked; 0.92 and None pass
    assert (at[0.3]["blocked_correct"], at[0.3]["passed_wrong"]) == (0, 3)
    assert (at[0.9]["blocked_correct"], at[0.9]["passed_wrong"]) == (1, 2)
    assert at[0.5]["threshold"] == 0.5


def test_a_confidence_equal_to_the_threshold_passes() -> None:
    # Same rule as the voice turn: below the threshold is blocked.
    at = {t["threshold"]: t for t in threshold_table([row(True, 0.5), row(False, 0.5)])}

    assert (at[0.5]["blocked_correct"], at[0.5]["passed_wrong"]) == (0, 1)


def test_summary_separates_conditions_and_lists_wrong_answers() -> None:
    rows = [row(True, 0.99), row(False, 0.88, noise=5), row(True, 0.9, source="you")]

    text = summarize(rows)

    assert "synthetic voices, clean" in text and "1/1" in text
    assert "synthetic voices, noise at 5 dB SNR" in text and "0/1" in text
    assert "your voice, clean" in text
    assert "Wrongly heard (1)" in text and "conf 0.88" in text


# --- the runner -----------------------------------------------------------------------


def test_runner_refuses_without_the_speech_switch(monkeypatch, capsys) -> None:
    monkeypatch.delenv("COLLECTWISE_SPEECH", raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key-not-real")

    assert run_voice_checks.main([]) == 1
    assert "Refusing to run" in capsys.readouterr().err


def test_runner_refuses_without_a_key(monkeypatch) -> None:
    monkeypatch.setenv("COLLECTWISE_SPEECH", "deepgram")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    assert run_voice_checks.main([]) == 1


def test_recordings_only_needs_a_recordings_folder(monkeypatch, capsys) -> None:
    monkeypatch.setenv("COLLECTWISE_SPEECH", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key-not-real")

    assert run_voice_checks.main(["--recordings-only"]) == 1
    assert "needs --recordings" in capsys.readouterr().err


def test_recordings_only_transcribes_your_files_and_nothing_else(monkeypatch, tmp_path) -> None:
    import deepgram
    from types import SimpleNamespace

    from backend.app.voice.audio import write_wav
    from backend.tests.test_deepgram import StubSDK, transcription

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "confirm_yes.wav").write_bytes(write_wav(tone(0.5, 16_000)))
    sdk = StubSDK(transcribe=transcription(SimpleNamespace(transcript="Yes.", confidence=0.97)), speak=[])
    monkeypatch.setattr(deepgram, "DeepgramClient", lambda **kwargs: sdk)
    monkeypatch.setenv("COLLECTWISE_SPEECH", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key-not-real")

    out = tmp_path / "out"
    assert run_voice_checks.main(["--recordings", str(recordings), "--recordings-only", "--out", str(out)]) == 0

    assert [name for name, _ in sdk.calls] == ["transcribe", "transcribe"]  # clean + 10 dB, no speech
    rows = json.loads((out / "results.json").read_text())["rows"]
    assert {(r["source"], r["phrase_id"], r["noise"], r["correct"]) for r in rows} == {
        ("you", "confirm_yes", None, True), ("you", "confirm_yes", 10, True),
    }
