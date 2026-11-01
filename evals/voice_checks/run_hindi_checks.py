"""Hindi and Hinglish checks (Milestone 7.6, part A): can a live call hear
Hinglish and Indian English, and which Deepgram voices should answer?

1. Hearing. The Hinglish phrases (phrases_hi.json) are spoken by Deepgram's
   Hindi voices and the English phrases (phrases.json) by its Indian-English
   voices; your own recordings of either set are used too. Every clip, clean
   and with noise at 10 dB, is streamed into each listening model the way a
   browser call hears it (16 kHz, --pace times real time): today's
   flux-general-en and the multilingual flux-general-multi with the hints
   "hi" and "en". Scored on meaning (amount, yes/no, account, a person), in
   either script. So: does multi hear Hinglish, and does it hear English as
   well as en does?
2. Listening samples for you to judge by ear (samples/): a payment read-back
   in Devanagari words, Devanagari with digits and in Latin letters, by each
   Hindi voice, and in English by each Indian-English voice; with each
   voice's time to first audio.

Spends Deepgram credit (about $0.40 with no recordings). No Claude calls.
Runs only with COLLECTWISE_SPEECH=deepgram and DEEPGRAM_API_KEY set.

Usage, from the repo root:
    set -a; source .env; set +a
    COLLECTWISE_SPEECH=deepgram python -m evals.voice_checks.run_hindi_checks \\
        [--recordings evals/voice_checks/recordings] [--recordings-only] [--phrases ID,ID]
"""

import argparse
import asyncio
import json
import os
import random
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

FOLDER = Path(__file__).parent
SAMPLE_RATE = 16_000  # what a browser call streams
SAMPLE_VOICE_RATE = 24_000  # what a browser call plays
HINDI_VOICES = ["flux-anjali-hi", "flux-rahul-hi"]  # female, male
INDIAN_ENGLISH_VOICES = ["flux-meena-en", "flux-naveen-en"]
MODELS = {"flux-general-en": (), "flux-general-multi": ("hi", "en")}
NOISE_LEVELS = [None, 10]
FLUX_PER_MINUTE = 0.0077  # the same for flux-general-multi (deepgram.com, checked 2026-11-01)
FLUX_TTS_PER_1K_CHARS = 0.045  # reported after the launch offer; check deepgram.com/pricing
READBACKS = {
    "devanagari_words": "आपके savings account से दो हज़ार रुपये का payment request तैयार है। "
                        "क्या मैं इसे submit कर दूँ?",
    "devanagari_digits": "आपके savings account से 2,000 रुपये का payment request तैयार है। "
                         "क्या मैं इसे submit कर दूँ?",
    "devanagari_paise": "आपके savings account से 2,350.50 रुपये का payment request तैयार है। "
                        "क्या मैं इसे submit कर दूँ?",
    "latin": "Aapke savings account se do hazaar rupaye ka payment request taiyaar hai. "
             "Kya main ise submit kar doon?",
    "latin_digits": "Aapke savings account se 2,000 rupaye ka payment request taiyaar hai. "
                    "Kya main ise submit kar doon?",
}
ENGLISH_READBACK = ("I've prepared a payment request for 2,000.00 rupees from your savings account. "
                    "Shall I submit it?")

# (voice, text, sample rate) -> (audio bytes, seconds to the first audio)
Synthesize = Callable[[str, str, int], tuple[bytes, float]]


def load_phrases() -> dict[str, list[dict]]:
    return {
        "hinglish": json.loads((FOLDER / "phrases_hi.json").read_text())["phrases"],
        "english": json.loads((FOLDER / "phrases.json").read_text())["phrases"],
    }


def model_table(rows: list[dict]) -> str:
    """Accuracy for each listening model, by phrase set and voice."""
    lines = ["Accuracy of the fact that matters, by listening model:"]
    groups = sorted({(r["set"], r["source"] == "you") for r in rows})
    for model in sorted({r["model"] for r in rows}):
        lines.append(f"  {model}")
        for phrase_set, is_you in groups:
            subset = [r for r in rows if r["model"] == model and r["set"] == phrase_set
                      and (r["source"] == "you") == is_you]
            if not subset:
                continue
            by_kind = ", ".join(
                f"{kind} {sum(r['correct'] for r in subset if r['kind'] == kind)}/"
                f"{sum(r['kind'] == kind for r in subset)}"
                for kind in ("amount", "confirm", "label", "handoff") if any(r["kind"] == kind for r in subset)
            )
            label = f"{phrase_set}, {'your voice' if is_you else 'synthetic voices'}"
            lines.append(f"    {label:32} {sum(r['correct'] for r in subset):3}/{len(subset):<3} ({by_kind})")
    return "\n".join(lines)


def deepgram_synthesize(client) -> Synthesize:
    from backend.app.voice.deepgram import REQUEST_OPTIONS

    def synthesize(voice: str, text: str, sample_rate: int) -> tuple[bytes, float]:
        started = time.perf_counter()
        first = None
        audio = bytearray()
        # Flux voices are on /v2/speak; the SDK requests lazily, while iterating.
        for chunk in client.speak.v2.audio.generate(
            model=voice, text=text, encoding="linear16", container="none", sample_rate=sample_rate,
            mip_opt_out=True, request_options=REQUEST_OPTIONS,
        ):
            if first is None and chunk:
                first = time.perf_counter() - started
            audio.extend(chunk)
        return bytes(audio), first or 0.0
    return synthesize


def deepgram_listener(api_key: str):
    from deepgram import AsyncDeepgramClient

    from backend.app import config
    from backend.app.voice.deepgram_live import DeepgramLiveSTT

    def listener(model: str, hints: Sequence[str]):
        return DeepgramLiveSTT(AsyncDeepgramClient(api_key=api_key), model=model, eot_threshold=config.EOT_THRESHOLD,
                               eot_timeout_ms=config.EOT_TIMEOUT_MS, language_hints=hints)
    return listener


def main(argv: list[str] | None = None, *, synthesize: Synthesize | None = None, listener=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recordings", type=Path, help="folder of your own <phrase_id>.wav recordings (either set)")
    parser.add_argument("--recordings-only", action="store_true",
                        help="only your recordings: no synthetic voices, no samples")
    parser.add_argument("--phrases", help="only these phrase ids, comma-separated")
    parser.add_argument("--no-samples", action="store_true", help="skip the listening samples")
    parser.add_argument("--out", type=Path, help="output folder (default: a new temp folder)")
    parser.add_argument("--pace", type=float, default=2.0, help="times real time to stream at")
    args = parser.parse_args(argv)
    if args.recordings_only and not args.recordings:
        print("--recordings-only needs --recordings.", file=sys.stderr)
        return 1
    if os.environ.get("COLLECTWISE_SPEECH") != "deepgram" or not os.environ.get("DEEPGRAM_API_KEY"):
        print("Refusing to run: set COLLECTWISE_SPEECH=deepgram and DEEPGRAM_API_KEY (load .env first).",
              file=sys.stderr)
        return 1

    # Imported late so the refusal above never needs the SDK.
    import httpx
    from deepgram.core.api_error import ApiError

    from backend.app.voice.audio import AudioClip, AudioFormatError, read_wav, write_wav
    from backend.app.voice.speech import SpeechUnavailableError
    from evals.voice_checks.live_hearing import LEAD_SILENCE_S, TRAIL_SILENCE_S, hear_live
    from evals.voice_checks.noise import add_noise
    from evals.voice_checks.scoring import heard_fact, is_correct, summarize

    if synthesize is None:
        from deepgram import DeepgramClient

        synthesize = deepgram_synthesize(DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"]))
    if listener is None:
        listener = deepgram_listener(os.environ["DEEPGRAM_API_KEY"])

    wanted = set(args.phrases.split(",")) if args.phrases else None
    phrase_sets = {name: [p for p in phrases if wanted is None or p["id"] in wanted]
                   for name, phrases in load_phrases().items()}
    out = args.out or Path(tempfile.mkdtemp(prefix="collectwise-hindi-checks-"))
    (out / "caller").mkdir(parents=True, exist_ok=True)
    listeners = {model: listener(model, hints) for model, hints in MODELS.items()}
    rng = random.Random(7_6)  # same noise every run
    rows: list[dict] = []
    errors: list[str] = []
    streamed_seconds = 0.0
    tts_chars = 0

    def failure(exc: Exception) -> str:
        return f"{type(exc).__name__} {getattr(exc, 'status_code', '') or ''}".strip()

    def voice(voice_name: str, text: str, rate: int) -> tuple[AudioClip, float] | None:
        nonlocal tts_chars
        try:
            data, first = synthesize(voice_name, text, rate)
        except (ApiError, httpx.HTTPError, SpeechUnavailableError, OSError) as exc:
            errors.append(f"{voice_name} couldn't speak: {failure(exc)}")
            return None
        tts_chars += len(text)
        return AudioClip(data=data, sample_rate=rate), first

    def hear(source: str, phrase_set: str, phrase: dict, clip: AudioClip) -> None:
        nonlocal streamed_seconds
        for noise in NOISE_LEVELS:
            heard_clip = add_noise(clip, noise, rng)
            for model, stt in listeners.items():
                try:
                    hearing = asyncio.run(hear_live(stt, heard_clip, pace=args.pace))
                except (ApiError, SpeechUnavailableError, OSError) as exc:
                    errors.append(f"{model} {source} {phrase['id']} noise={noise}: {failure(exc)}")
                    continue
                streamed_seconds += heard_clip.duration_ms / 1000 + LEAD_SILENCE_S + TRAIL_SILENCE_S
                rows.append({
                    "model": model, "set": phrase_set, "source": source, "noise": noise,
                    "phrase_id": phrase["id"], "kind": phrase["kind"], "expect": phrase["expect"],
                    "transcript": hearing.text, "confidence": hearing.mean, "confidence_min": hearing.lowest,
                    "heard": heard_fact(phrase["kind"], hearing.text),
                    "correct": is_correct(phrase["kind"], phrase["expect"], hearing.text),
                })

    print(f"Writing to {out}")
    voices = [] if args.recordings_only else (
        [(v, "hinglish") for v in HINDI_VOICES] + [(v, "english") for v in INDIAN_ENGLISH_VOICES])
    for voice_name, phrase_set in voices:
        for phrase in phrase_sets[phrase_set]:
            spoken = voice(voice_name, phrase["text"], SAMPLE_RATE)
            if spoken is None:
                break  # this voice is unavailable: the error says why
            (out / "caller" / f"{voice_name}_{phrase['id']}.wav").write_bytes(write_wav(spoken[0]))
            hear(voice_name, phrase_set, phrase, spoken[0])
        print(f"  {voice_name}: done")

    if args.recordings:
        found = 0
        for phrase_set, phrases in phrase_sets.items():
            for phrase in phrases:
                path = args.recordings / f"{phrase['id']}.wav"
                if not path.exists():
                    continue
                try:
                    clip = read_wav(path.read_bytes())
                except AudioFormatError as exc:
                    errors.append(f"your {path.name}: {exc.reason} (convert with afconvert, see README)")
                    continue
                found += 1
                hear("you", phrase_set, phrase, clip)
        print(f"  your recordings: {found} found")

    timing = []
    if not (args.recordings_only or args.no_samples):
        (out / "samples").mkdir(exist_ok=True)
        samples = [(v, style, text) for v in HINDI_VOICES for style, text in READBACKS.items()]
        samples += [(v, "english", ENGLISH_READBACK) for v in INDIAN_ENGLISH_VOICES]
        for voice_name, style, text in samples:
            spoken = voice(voice_name, text, SAMPLE_VOICE_RATE)
            if spoken is None:
                continue
            (out / "samples" / f"{voice_name}_{style}.wav").write_bytes(write_wav(spoken[0]))
            timing.append({"voice": voice_name, "style": style, "first_audio_s": round(spoken[1], 3)})

    (out / "results.json").write_text(json.dumps({"rows": rows, "voice_timing": timing, "errors": errors}, indent=2))

    print()
    print(model_table(rows))
    for model in MODELS:
        subset = [r for r in rows if r["model"] == model]
        if subset:
            print(f"\n=== {model} ===")
            print(summarize(subset))
    if timing:
        print("\nTime to first audio (listening samples):")
        for t in timing:
            print(f"  {t['voice']:16} {t['style']:18} {t['first_audio_s']:.2f} s")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for error in errors:
            print(f"  {error}")
    minutes = streamed_seconds / 60
    print(f"\nDeepgram use: {minutes:.1f} min streamed (~${minutes * FLUX_PER_MINUTE:.3f}), {tts_chars} characters "
          f"spoken (~${tts_chars / 1000 * FLUX_TTS_PER_1K_CHARS:.3f}); from the free credit")
    if timing:
        print(f"Listen to {out}/samples/*.wav and tell which voice and writing style sounds right.")
    print(f"Full results: {out}/results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
