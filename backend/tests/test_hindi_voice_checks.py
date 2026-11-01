"""Hindi and Hinglish in the voice checks (M7.6): the scorer reads amounts in
Hindi number words and digits of either script, and the Hindi words for
yes, no, unsure, account names and a person; the Hinglish phrases score
correct on their own words, in Devanagari and in Latin letters."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from evals.voice_checks.hindi_text import amounts, normalize, words
from evals.voice_checks.scoring import classify_confirmation, heard_fact, is_correct

FOLDER = Path(__file__).resolve().parents[2] / "evals" / "voice_checks"
HINDI = json.loads((FOLDER / "phrases_hi.json").read_text())["phrases"]
ENGLISH = json.loads((FOLDER / "phrases.json").read_text())["phrases"]


# --- the phrase file -------------------------------------------------------------------


def test_hinglish_phrases_are_well_formed() -> None:
    ids = [p["id"] for p in HINDI]
    assert len(set(ids)) == len(ids)
    assert not set(ids) & {p["id"] for p in ENGLISH}  # one recordings folder holds both
    for phrase in HINDI:
        assert phrase["id"].startswith("hi_")
        assert phrase["kind"] in {"amount", "confirm", "label", "handoff"}
        assert phrase["text"] and phrase["say"] and phrase["expect"]
        assert phrase["say"].isascii()  # for reading aloud


@pytest.mark.parametrize("phrase", HINDI, ids=[p["id"] for p in HINDI])
def test_each_hinglish_phrase_scores_correct_in_either_script(phrase) -> None:
    assert is_correct(phrase["kind"], phrase["expect"], phrase.get("written", phrase["text"]))
    if "written" not in phrase:
        assert is_correct(phrase["kind"], phrase["expect"], phrase["say"])


# --- amounts -------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("दो हज़ार रुपये", ["2000"]),
    ("do hazaar rupaye", ["2000"]),
    ("दो हजार", ["2000"]),  # without the nukta
    ("2 हज़ार", ["2000"]),
    ("२,००० रुपये", ["2000"]),  # Devanagari digits
    ("1,50,000 रुपये", ["150000"]),  # Indian grouping
    ("डेढ़ सौ", ["150"]),
    ("dedh lakh", ["150000"]),
    ("ढाई हज़ार", ["2500"]),
    ("साढ़े तीन सौ", ["350"]),
    ("sava sau", ["125"]),
    ("paune do sau", ["175"]),
    ("पाँच सौ पचास", ["550"]),
    ("पांच सौ पचास", ["550"]),  # anusvara for chandrabindu
    ("do hazaar paanch sau", ["2500"]),
    ("ek lakh pachaas hazaar", ["150000"]),
    ("hazaar rupaye", ["1000"]),
    ("paanch sau crore", ["5000000000"]),  # hundreds before a scale word
    ("सौ रुपये", ["100"]),
    ("साठ रुपये", ["60"]),
    ("teen sau aur paanch sau", ["300", "500"]),  # two amounts
    ("15, 50", ["15", "50"]),
    ("$125.50 now", ["125.50"]),
])
def test_hindi_amounts(text, expected) -> None:
    assert amounts(text) == [Decimal(value) for value in expected]


@pytest.mark.parametrize("text", [
    "हाँ, कर दो",  # "do" the verb, not 2
    "pay kar do",
    "aap ke saath",  # "with", not 60
    "saadhe",  # a fraction word with no number after it
    "no numbers here",
])
def test_words_that_are_not_amounts(text) -> None:
    assert amounts(text) == []


def test_two_after_a_fraction_word_is_a_number() -> None:
    assert amounts("paune do") == [Decimal("1.75")]


def test_amount_scoring_needs_exactly_one_amount() -> None:
    assert is_correct("amount", "2000", "मेरे savings से दो हज़ार रुपये pay कर दो")
    assert not is_correct("amount", "2000", "दो सौ रुपये")
    assert not is_correct("amount", "2000", "दो हज़ार, नहीं तीन हज़ार")
    assert heard_fact("amount", "डेढ़ सौ रुपये") == "150"


# --- words ------------------------------------------------------------------------------


def test_spelling_variants_read_the_same() -> None:
    assert normalize("हज़ार") == normalize("हजार")
    assert normalize("पाँच") == normalize("पांच")
    assert normalize("डेढ़") == normalize("डेढ")
    assert words("नहीं, रहने दो। Cancel करो") == [normalize(w) for w in ("नहीं", "रहने", "दो", "cancel", "करो")]


@pytest.mark.parametrize(("text", "meaning"), [
    ("हाँ, कर दो", "yes"),
    ("Haan ji, theek hai", "yes"),
    ("जी", "yes"),
    ("नहीं", "no"),
    ("nahi ji", "no"),  # refusal wins over the polite "ji"
    ("मत करो", "no"),
    ("Nahi, rehne do", "no"),
    ("पता नहीं", "unsure"),  # contains "nahi", but it's doubt
    ("pata nahi yaar", "unsure"),
    ("शायद", "unsure"),
    ("acha", "none"),
])
def test_hindi_confirmations(text, meaning) -> None:
    assert classify_confirmation(text) == meaning


def test_hindi_account_names_and_asking_for_a_person() -> None:
    assert heard_fact("label", "सेविंग्स वाले account से") == "savings"
    assert heard_fact("label", "करंट अकाउंट से") == "current"
    assert heard_fact("label", "मेन अकाउंट") == "main"
    assert heard_fact("label", "मैं pay करना चाहता हूं") == "none"  # "I", not "main"
    assert heard_fact("label", "सेविंग अकाउंट") == "none"  # like "saving" in English: not the label
    assert is_correct("handoff", "handoff", "मुझे किसी इंसान से बात करनी है")
    assert is_correct("handoff", "handoff", "kisi aadmi se baat karao")
    assert not is_correct("handoff", "handoff", "मुझे बात करनी है")


# --- the runner (no network: Deepgram is replaced) ------------------------------------------

import asyncio  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from deepgram.core.api_error import ApiError  # noqa: E402

from backend.app.voice.live_speech import HeardEvent  # noqa: E402
from backend.tests.test_voice_checks import tone  # noqa: E402
from evals.voice_checks import run_hindi_checks  # noqa: E402

HINDI_TONE_S, ENGLISH_TONE_S = 0.6, 0.3
# What each model "hears", by which clip it was (told apart by length).
HEARD = {
    ("flux-general-multi", "hinglish"): ("दो हज़ार रुपये pay कर दो", [0.95, 0.9]),
    ("flux-general-en", "hinglish"): ("Do has our rupee pay core do", [0.5, 0.2]),
    ("flux-general-multi", "english"): ("Yes.", [0.97]),
    ("flux-general-en", "english"): ("Yes.", [0.99]),
}


class FakeListener:
    def __init__(self, model: str, opened: list) -> None:
        self.model, self.opened = model, opened

    def open(self, *, sample_rate: int, keyterms=()):
        stt = self

        @asynccontextmanager
        async def listening():
            stt.opened.append((stt.model, sample_rate))
            received = bytearray()
            done = asyncio.Event()

            class Listener:
                async def send_audio(self, chunk: bytes) -> None:
                    received.extend(chunk)

                async def finish(self) -> None:
                    done.set()

                async def events(self):
                    await done.wait()
                    seconds = len(received) / 2 / sample_rate - run_hindi_checks_padding()
                    phrase_set = "hinglish" if seconds > (HINDI_TONE_S + ENGLISH_TONE_S) / 2 else "english"
                    text, confidences = HEARD[(stt.model, phrase_set)]
                    yield HeardEvent("end_of_turn", text, tuple(confidences))

            yield Listener()

        return listening()


def run_hindi_checks_padding() -> float:
    from evals.voice_checks.live_hearing import LEAD_SILENCE_S, TRAIL_SILENCE_S
    return LEAD_SILENCE_S + TRAIL_SILENCE_S


@pytest.fixture
def deepgram_on(monkeypatch):
    monkeypatch.setenv("COLLECTWISE_SPEECH", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key-not-real")


def fakes(*, broken_voice: str | None = None):
    spoken, opened, hints = [], [], {}

    def synthesize(voice: str, text: str, rate: int):
        if voice == broken_voice:
            raise ApiError(status_code=400, headers={}, body="")
        spoken.append((voice, rate))
        seconds = ENGLISH_TONE_S if voice.endswith("-en") else HINDI_TONE_S
        return tone(seconds, rate).data, 0.25

    def listener(model: str, language_hints):
        hints[model] = tuple(language_hints)
        return FakeListener(model, opened)

    return synthesize, listener, spoken, opened, hints


def test_the_hindi_runner_refuses_without_the_speech_switch(monkeypatch, capsys) -> None:
    monkeypatch.delenv("COLLECTWISE_SPEECH", raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key-not-real")

    assert run_hindi_checks.main([]) == 1
    assert "Refusing to run" in capsys.readouterr().err


def test_the_hindi_runner_refuses_recordings_only_without_a_folder(deepgram_on, capsys) -> None:
    assert run_hindi_checks.main(["--recordings-only"]) == 1
    assert "needs --recordings" in capsys.readouterr().err


def test_both_models_hear_every_clip_and_are_compared(deepgram_on, tmp_path, capsys) -> None:
    synthesize, listener, spoken, opened, hints = fakes(broken_voice="flux-rahul-hi")
    out = tmp_path / "out"

    code = run_hindi_checks.main(["--phrases", "hi_amount_do_hazaar,confirm_yes", "--out", str(out), "--pace", "50"],
                                 synthesize=synthesize, listener=listener)

    assert code == 0
    assert hints == {"flux-general-en": (), "flux-general-multi": ("hi", "en")}
    results = json.loads((out / "results.json").read_text())
    rows = results["rows"]
    # One Hindi voice (the other failed) and two Indian-English voices, each
    # phrase clean and at 10 dB, each heard by both models.
    assert len(rows) == (1 + 2) * 2 * 2
    assert {(r["source"], r["set"], r["phrase_id"]) for r in rows} == {
        ("flux-anjali-hi", "hinglish", "hi_amount_do_hazaar"),
        ("flux-meena-en", "english", "confirm_yes"), ("flux-naveen-en", "english", "confirm_yes"),
    }
    by_model = {(r["model"], r["set"]): r["correct"] for r in rows}
    assert by_model == {("flux-general-multi", "hinglish"): True, ("flux-general-en", "hinglish"): False,
                        ("flux-general-multi", "english"): True, ("flux-general-en", "english"): True}
    assert {rate for _, rate in opened} == {16_000}
    # Caller clips at a browser call's 16 kHz, then the samples at its 24 kHz playback rate.
    assert spoken == [("flux-anjali-hi", 16_000), ("flux-meena-en", 16_000), ("flux-naveen-en", 16_000)] + [
        ("flux-anjali-hi", 24_000)] * 5 + [("flux-meena-en", 24_000), ("flux-naveen-en", 24_000)]

    report = capsys.readouterr().out
    table = " ".join(report.split("\n\n")[1].split())  # the model table, spaces collapsed
    assert table == ("Accuracy of the fact that matters, by listening model: "
                     "flux-general-en english, synthetic voices 4/4 (confirm 4/4) "
                     "hinglish, synthetic voices 0/2 (amount 0/2) "
                     "flux-general-multi english, synthetic voices 4/4 (confirm 4/4) "
                     "hinglish, synthetic voices 2/2 (amount 2/2)")
    assert "flux-rahul-hi couldn't speak: ApiError 400" in report
    # Samples: five styles from the working Hindi voice, English from both Indian-English voices.
    assert sorted(p.name for p in (out / "samples").iterdir()) == sorted(
        [f"flux-anjali-hi_{style}.wav" for style in run_hindi_checks.READBACKS]
        + ["flux-meena-en_english.wav", "flux-naveen-en_english.wav"])
    assert len(results["voice_timing"]) == 7
    assert "Deepgram use:" in report


def test_your_recordings_of_either_set_are_heard_and_nothing_is_spoken(deepgram_on, tmp_path) -> None:
    from backend.app.voice.audio import write_wav

    synthesize, listener, spoken, opened, _ = fakes()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "hi_amount_do_hazaar.wav").write_bytes(write_wav(tone(HINDI_TONE_S, 16_000)))
    (recordings / "confirm_yes.wav").write_bytes(write_wav(tone(ENGLISH_TONE_S, 16_000)))
    (recordings / "unrelated.wav").write_bytes(b"not a phrase")
    out = tmp_path / "out"

    assert run_hindi_checks.main(["--recordings", str(recordings), "--recordings-only", "--out", str(out),
                                  "--pace", "50"], synthesize=synthesize, listener=listener) == 0

    assert spoken == []
    rows = json.loads((out / "results.json").read_text())["rows"]
    assert {(r["source"], r["set"], r["phrase_id"]) for r in rows} == {
        ("you", "hinglish", "hi_amount_do_hazaar"), ("you", "english", "confirm_yes")}
    assert len(rows) == 2 * 2 * 2
    assert not (out / "samples").exists()


def test_the_model_table_splits_by_model_set_and_voice() -> None:
    rows = [
        {"model": "m1", "set": "hinglish", "source": "flux-anjali-hi", "kind": "amount", "correct": True},
        {"model": "m1", "set": "hinglish", "source": "you", "kind": "amount", "correct": False},
        {"model": "m2", "set": "english", "source": "flux-meena-en", "kind": "confirm", "correct": True},
    ]

    table = " ".join(run_hindi_checks.model_table(rows).split())

    assert table == ("Accuracy of the fact that matters, by listening model: "
                     "m1 hinglish, synthetic voices 1/1 (amount 1/1) hinglish, your voice 0/1 (amount 0/1) "
                     "m2 english, synthetic voices 1/1 (confirm 1/1)")


def test_a_voice_that_cant_speak_is_tried_once_not_for_every_phrase(deepgram_on, tmp_path, capsys) -> None:
    synthesize, listener, spoken, opened, _ = fakes(broken_voice="flux-rahul-hi")
    attempts = []

    def counting(voice: str, text: str, rate: int):
        attempts.append(voice)
        return synthesize(voice, text, rate)

    assert run_hindi_checks.main(["--phrases", "hi_amount_do_hazaar,hi_confirm_haan", "--no-samples",
                                  "--out", str(tmp_path / "out"), "--pace", "50"],
                                 synthesize=counting, listener=listener) == 0

    assert attempts.count("flux-rahul-hi") == 1
    assert attempts.count("flux-anjali-hi") == 2
    assert capsys.readouterr().out.count("flux-rahul-hi couldn't speak") == 1
