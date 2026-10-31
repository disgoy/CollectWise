"""Voice checks (Milestone 4.7): does speech-to-text keep the facts that
matter, is the confidence threshold right, and where does speaking time go?

1. Each phrase in phrases.json is spoken by three Deepgram voices at phone
   quality (8 kHz), mixed with background noise at several levels on our side,
   and transcribed. Scored on meaning: was the amount, yes/no, account name or
   handoff request heard correctly?
2. Optional: your own recordings, `<phrase_id>.wav` in --recordings (kept on
   this machine; never commit them).
3. Speaking time for short, medium and long replies: time to the first piece
   of audio versus the whole reply (the number streaming in M5 depends on).
4. Listening samples: one amount written four ways, for you to compare by ear.

With --live (M5.7), clips are heard the way a live call hears them: streamed
into Flux in 80 ms frames (at --pace times real time), scored the same way,
with threshold tables for the average and the lowest word confidence. Live
mode uses --voices 1 and noise levels clean / 10 dB, and skips parts 3-4.

Spends Deepgram credit (about $0.10; live mode a few cents). No Claude calls.
Runs only with COLLECTWISE_SPEECH=deepgram and DEEPGRAM_API_KEY set.

Usage, from the repo root:
    set -a; source .env; set +a
    COLLECTWISE_SPEECH=deepgram python -m evals.voice_checks.run_voice_checks \\
        [--recordings evals/voice_checks/recordings] [--live]
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

PHRASES_PATH = Path(__file__).with_name("phrases.json")
VOICES = ["aura-2-thalia-en", "aura-2-draco-en", "aura-2-amalthea-en"]  # US, British, Filipino
NOISE_LEVELS = [None, 20, 10, 5]  # SNR in dB; None = clean
LIVE_NOISE_LEVELS = [None, 10]  # streaming takes real time, so fewer levels
FLUX_PER_MINUTE = 0.0077  # deepgram.com/pricing, checked 2026-10-31
CALLER_SAMPLE_RATE = 8_000  # phone quality

REPLIES = {
    "short": "Your payment request is recorded.",
    "medium": "I've prepared a payment request for 120 US dollars from your main account. "
              "Do you want me to submit it?",
    "long": "Your main account has 500 US dollars available and it's active. You can make a payment "
            "request, check an earlier request, or talk to a member of our team. What would you like "
            "to do next? I can also read your recent payment requests back to you.",
}
AMOUNT_STYLES = {
    "decimals": "Your main account has 500.00 US dollars.",
    "plain": "Your main account has 500 dollars.",
    "words": "Your main account has five hundred dollars.",
    "symbol": "Your main account has $500.",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recordings", type=Path, help="folder of your own <phrase_id>.wav recordings")
    parser.add_argument("--out", type=Path, help="output folder (default: a new temp folder)")
    parser.add_argument("--recordings-only", action="store_true",
                        help="test only your recordings: skip the synthetic voices, timing and samples")
    parser.add_argument("--live", action="store_true",
                        help="hear clips through live (streaming) Flux, as a live call does")
    parser.add_argument("--pace", type=float, default=2.0, help="live mode: times real time to stream at")
    parser.add_argument("--voices", type=int, help="how many synthetic voices (default: 3, or 1 with --live)")
    args = parser.parse_args(argv)
    if args.recordings_only and not args.recordings:
        print("--recordings-only needs --recordings.", file=sys.stderr)
        return 1

    if os.environ.get("COLLECTWISE_SPEECH") != "deepgram" or not os.environ.get("DEEPGRAM_API_KEY"):
        print("Refusing to run: set COLLECTWISE_SPEECH=deepgram and DEEPGRAM_API_KEY (load .env first).",
              file=sys.stderr)
        return 1

    # Imported late so the refusal above never needs the SDK.
    from deepgram import DeepgramClient

    from backend.app import config
    from backend.app.voice.audio import AudioFormatError, read_wav, write_wav
    from backend.app.voice.deepgram import REQUEST_OPTIONS, TTS_SAMPLE_RATE, DeepgramSTT, DeepgramTTS
    from backend.app.voice.deepgram_live import DeepgramLiveSTT
    from backend.app.voice.speech import SpeechUnavailableError
    from evals.costs import deepgram_stt_cost, deepgram_tts_cost
    from evals.voice_checks.live_hearing import LEAD_SILENCE_S, TRAIL_SILENCE_S, hear_live
    from evals.voice_checks.noise import add_noise
    from evals.voice_checks.scoring import heard_fact, is_correct, summarize

    phrases = json.loads(PHRASES_PATH.read_text())["phrases"]
    out = args.out or Path(tempfile.mkdtemp(prefix="collectwise-voice-checks-"))
    (out / "caller").mkdir(parents=True, exist_ok=True)
    client = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])
    stt = DeepgramSTT(client, model=config.STT_MODEL)
    if args.live:
        from deepgram import AsyncDeepgramClient

        live_stt = DeepgramLiveSTT(
            AsyncDeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"]), model=config.LIVE_STT_MODEL,
            eot_threshold=config.EOT_THRESHOLD, eot_timeout_ms=config.EOT_TIMEOUT_MS,
        )
    voices = VOICES[: args.voices if args.voices is not None else (1 if args.live else len(VOICES))]
    noise_levels = LIVE_NOISE_LEVELS if args.live else NOISE_LEVELS
    streamed_seconds = 0.0
    rng = random.Random(4_7)  # same noise every run
    rows: list[dict] = []
    errors: list[str] = []
    tts_chars = 0

    def listen(source: str, phrase: dict, clip, noise) -> None:
        nonlocal streamed_seconds
        heard_clip = add_noise(clip, noise, rng)
        started = time.perf_counter()
        try:
            if args.live:
                hearing = asyncio.run(hear_live(live_stt, heard_clip, pace=args.pace))
                text, confidence, lowest = hearing.text, hearing.mean, hearing.lowest
                streamed_seconds += heard_clip.duration_ms / 1000 + LEAD_SILENCE_S + TRAIL_SILENCE_S
            else:
                transcript = stt.transcribe(heard_clip)
                text, confidence, lowest = transcript.text, transcript.confidence, None
        except SpeechUnavailableError as exc:
            errors.append(f"{source} {phrase['id']} noise={noise}: {exc}")
            return
        rows.append({
            "source": source, "noise": noise, "phrase_id": phrase["id"], "kind": phrase["kind"],
            "expect": phrase["expect"], "transcript": text, "confidence": confidence, "confidence_min": lowest,
            "heard": heard_fact(phrase["kind"], text),
            "correct": is_correct(phrase["kind"], phrase["expect"], text),
            "stt_ms": round((time.perf_counter() - started) * 1000), "audio_ms": heard_clip.duration_ms,
        })

    print(f"Writing to {out}")
    for voice in [] if args.recordings_only else voices:
        speaker = DeepgramTTS(client, voice=voice, sample_rate=CALLER_SAMPLE_RATE)
        for phrase in phrases:
            try:
                clip = speaker.synthesize(phrase["text"])
            except SpeechUnavailableError as exc:
                errors.append(f"{voice} {phrase['id']} synthesis: {exc}")
                continue
            tts_chars += len(phrase["text"])
            (out / "caller" / f"{voice}_{phrase['id']}.wav").write_bytes(write_wav(clip))
            for noise in noise_levels:
                listen(voice, phrase, clip, noise)
        print(f"  {voice}: done")

    if args.recordings:
        found = 0
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
            for noise in (None, 10):
                listen("you", phrase, clip, noise)
        print(f"  your recordings: {found} of {len(phrases)} phrases found")

    # Speaking time: first audio chunk versus the whole reply.
    timing = []
    for size, text in {} if args.recordings_only or args.live else REPLIES.items():
        started = time.perf_counter()
        first = None
        total_bytes = 0
        for chunk in client.speak.v1.audio.generate(
            text=text, model=config.TTS_VOICE, encoding="linear16", container="none",
            sample_rate=TTS_SAMPLE_RATE, request_options=REQUEST_OPTIONS,
        ):
            if first is None and chunk:
                first = time.perf_counter() - started
            total_bytes += len(chunk)
        tts_chars += len(text)
        timing.append({
            "size": size, "chars": len(text), "first_audio_s": round(first or 0, 3),
            "total_s": round(time.perf_counter() - started, 3),
            "audio_s": round(total_bytes / 2 / TTS_SAMPLE_RATE, 2),
        })

    # Listening samples for you.
    agent_voice = DeepgramTTS(client, voice=config.TTS_VOICE)
    for style, text in {} if args.recordings_only or args.live else AMOUNT_STYLES.items():
        (out / f"amount_{style}.wav").write_bytes(write_wav(agent_voice.synthesize(text)))
        tts_chars += len(text)

    (out / "results.json").write_text(json.dumps({"rows": rows, "tts_timing": timing, "errors": errors}, indent=2))

    print()
    print(summarize(rows))
    print("\nSpeech-to-text time per clip: " + (
        f"median {statistics.median(r['stt_ms'] for r in rows)} ms, "
        f"p95 {sorted(r['stt_ms'] for r in rows)[int(len(rows) * 0.95)]} ms (n={len(rows)})" if rows else "-"
    ))
    print("Speaking time (agent voice, 24 kHz):")
    for t in timing:
        print(f"  {t['size']:6} {t['chars']:4} chars: first audio after {t['first_audio_s']:.2f} s, "
              f"whole reply {t['total_s']:.2f} s, for {t['audio_s']:.1f} s of speech")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for error in errors:
            print(f"  {error}")
    if args.live:
        # Flux bills the audio streamed, quiet padding included.
        minutes = streamed_seconds / 60
        listening = f"{minutes:.1f} min streamed to Flux (~${minutes * FLUX_PER_MINUTE:.3f})"
    else:
        audio_minutes = sum(r["audio_ms"] for r in rows) / 60_000
        listening = f"{audio_minutes:.1f} min transcribed (~${deepgram_stt_cost(audio_minutes * 60):.3f})"
    print(f"\nDeepgram use: {listening}, "
          f"{tts_chars} characters spoken (~${deepgram_tts_cost(tts_chars):.3f}); from the free credit")
    print(f"Listen to {out}/amount_*.wav (and caller/ for the test voices). Full results: {out}/results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
