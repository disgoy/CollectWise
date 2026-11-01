"""Live check of Deepgram's streaming speech (M5.2): no Claude calls, about
2 cents of Deepgram credit (free credit). Synthetic phrases only.

Part 1 asks the Aura socket what its docs don't say: what Flushed's
sequence_id counts, whether audio still arrives after Clear, whether Cleared
comes when nothing is queued, how fast the first audio comes after a flush,
and whether an idle socket is closed.

Part 2 runs our adapters end to end: speaks three phrases over the Aura
socket, streams the audio into Flux at real-time pace, and prints the turn
events with timings, how numbers come out, and the word confidences.

With --hindi (M7.6 B2) the same two parts check a Hindi call instead: the
Flux voice socket (/v2/speak, Rahul) — what Flushed reports, whether audio
still comes after Interrupt, whether SpeechInterrupted comes with nothing
queued, and whether 25 quick flushes draw a warning (Aura allows 20 a
minute) — then Rahul's Hinglish into the multilingual listener, once with
the account names as keyterms and once without (does it take them, and is
"current" heard right?). A few cents.

Usage, from the repo root:
    set -a; source .env; set +a
    COLLECTWISE_SPEECH=deepgram python -m scripts.smoke_live_speech [--idle 30] [--hindi]
"""

import argparse
import asyncio
import os
import statistics
from collections import Counter
import sys
import time

if os.environ.get("COLLECTWISE_SPEECH") != "deepgram" or not os.environ.get("DEEPGRAM_API_KEY"):
    sys.exit("Refusing to run: set COLLECTWISE_SPEECH=deepgram and DEEPGRAM_API_KEY (load .env first).")

from deepgram import AsyncDeepgramClient  # noqa: E402
from deepgram.core.api_error import ApiError  # noqa: E402
from deepgram.speak.v1.types import SpeakV1Text  # noqa: E402
from deepgram.speak.v2.types import SpeakV2Speak  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from backend.app import config  # noqa: E402
from backend.app.voice.deepgram_live import FLUX_VOICE, DeepgramLiveSTT, DeepgramLiveTTS, speak_protocol  # noqa: E402
from backend.app.voice.live_speech import SegmentDone, SpokenAudio  # noqa: E402
from evals.costs import deepgram_tts_cost  # noqa: E402

PHRASES = [
    "Please pay one hundred and twenty dollars from my main account.",
    "Yes, go ahead.",
    "What's the balance on my savings account?",
]
KEYTERMS = ["main", "savings"]
HINDI_PHRASES = [
    "मेरे savings account से दो हज़ार रुपये pay कर दो।",
    "हाँ, कर दो।",
    "मेरे current account में कितना balance है?",
]
HINDI_KEYTERMS = ["savings", "current"]
RATE = 16_000  # what Flux gets; the caller's browser will send the same
FRAME_BYTES = RATE * 2 * 80 // 1000  # 80 ms of 16-bit mono
FLUX_PER_MINUTE = 0.0077  # deepgram.com/pricing, checked 2026-10-31

tts_chars = 0


def message_order(log: list[tuple[float, str, object]]) -> str:
    """The message types in order, a run of audio frames as one entry, so one
    can see where Flushed and the end-of-speech message fall (D111)."""
    order: list[str] = []
    for _, kind, size in log:
        if kind == "audio" and order and order[-1].startswith("audio"):
            frames, total = order[-1][6:-1].split("/")
            order[-1] = f"audio({int(frames) + 1}/{int(total) + size})"
        else:
            order.append(f"audio(1/{size})" if kind == "audio" else kind)
    return " -> ".join(order)


async def voice_facts(client: AsyncDeepgramClient, voice: str, idle_seconds: int) -> None:
    """Aura (Text / Clear / Cleared) or a Flux voice (Speak / Interrupt /
    SpeechInterrupted): the same questions."""
    global tts_chars
    flux = speak_protocol(voice) is FLUX_VOICE
    confirmed = "SpeechInterrupted" if flux else "Cleared"
    print(f"\n=== Part 1: {voice} socket behaviour")
    async with (client.speak.v2 if flux else client.speak.v1).connect(
        model=voice, encoding="linear16", sample_rate=24_000, mip_opt_out="true",
    ) as socket:
        log: list[tuple[float, str, object]] = []
        started = time.perf_counter()

        async def read() -> None:
            try:
                async for message in socket:
                    now = time.perf_counter() - started
                    if isinstance(message, bytes):
                        log.append((now, "audio", len(message)))
                    else:
                        label = getattr(message, "sequence_id", None) or getattr(message, "speech_id", None) \
                            or getattr(message, "code", None)
                        log.append((now, getattr(message, "type", "?"), label))
            except ConnectionClosed as exc:
                log.append((time.perf_counter() - started, "closed", type(exc).__name__))

        reader = asyncio.create_task(read())

        async def speak(text: str) -> float:
            global tts_chars
            tts_chars += len(text)
            sent = time.perf_counter() - started
            await (socket.send_speak(SpeakV2Speak(text=text)) if flux else socket.send_text(SpeakV1Text(text=text)))
            await socket.send_flush()
            return sent

        async def interrupt() -> None:
            await (socket.send_interrupt() if flux else socket.send_clear())

        # 1. Three flushes in a row: what do the ids count?
        firsts = ("पहला वाक्य।", "दूसरा।", "तीसरा।") if flux else ("First sentence.", "Second one.", "Third.")
        sent_at = [await speak(text) for text in firsts]
        await asyncio.sleep(4)
        flushed = [(t, seq) for t, kind, seq in log if kind == "Flushed"]
        print(f"  Flushed ids after three flushes: {[seq for _, seq in flushed]}")
        audio_times = [t for t, kind, _ in log if kind == "audio"]
        if audio_times:
            print(f"  first audio {audio_times[0] - sent_at[0]:.2f} s after the first flush")
        print(f"  message order for these flushes: {message_order(log)}")

        # 2. Interrupt in the middle of a sentence.
        mark = len(log)
        await speak("यह एक लंबा वाक्य है जिसे बीच में ही रोक दिया जाना चाहिए, इससे पहले कि यह पूरा हो।" if flux else
                    "This is a longer sentence that a clear message should cut off well before it finishes.")
        for _ in range(60):
            if any(kind == "audio" for _, kind, _ in log[mark:]):
                break
            await asyncio.sleep(0.05)
        cleared_at = time.perf_counter() - started
        await interrupt()
        await asyncio.sleep(2.5)
        after = [(t, kind, seq) for t, kind, seq in log[mark:] if t >= cleared_at]
        cleared = [t for t, kind, _ in after if kind == confirmed]
        before_cleared = sum(n for t, kind, n in after if kind == "audio" and (not cleared or t < cleared[0]))
        after_cleared = sum(n for t, kind, n in after if kind == "audio" and cleared and t > cleared[0])
        print(f"  after the interruption: {confirmed} received={bool(cleared)}"
              + (f" ({cleared[0] - cleared_at:.2f} s later)" if cleared else ""))
        print(f"  audio bytes after it but before {confirmed}: {before_cleared}; after {confirmed}: {after_cleared}")
        print(f"  Flushed after the interruption: {[seq for _, kind, seq in after if kind == 'Flushed']}")

        # 3. Interrupt with nothing queued: does the confirmation still come?
        mark = len(log)
        await interrupt()
        await asyncio.sleep(1.5)
        print(f"  interruption with nothing queued -> {confirmed} received="
              f"{any(k == confirmed for _, k, _ in log[mark:])}")

        # 3b. A Flux voice: Aura caps flushes at 20 a minute; does this one?
        if flux:
            mark = len(log)
            for _ in range(25):
                await speak("हाँ।")
            await asyncio.sleep(6)
            counts = Counter(kind for _, kind, _ in log[mark:] if kind != "audio")
            warnings = [label for _, kind, label in log[mark:] if kind in ("Warning", "Error")]
            print(f"  25 quick flushes -> {dict(counts)}; warnings/errors: {warnings or 'none'}")

        # 4. Idle: is the socket closed?
        if idle_seconds:
            print(f"  waiting {idle_seconds} s idle ...")
            await asyncio.sleep(idle_seconds)
            mark = len(log)
            try:
                await speak("Still there?")
                await asyncio.sleep(2)
                kinds = {kind for _, kind, _ in log[mark:]}
                state = "still open" if "audio" in kinds else f"no audio: {kinds}"
                print(f"  after {idle_seconds} s idle: {state}")
            except ConnectionClosed:
                print(f"  after {idle_seconds} s idle: the socket had been closed")
        try:
            await socket.send_close()
        except ConnectionClosed:
            pass
        await asyncio.wait_for(reader, 10)


async def adapters_end_to_end(client: AsyncDeepgramClient, *, hindi: bool = False) -> float:
    global tts_chars
    voice, phrases = (config.HINDI_TTS_VOICE, HINDI_PHRASES) if hindi else (config.TTS_VOICE, PHRASES)
    print(f"\n=== Part 2: our adapters, {voice} -> {config.HINDI_STT_MODEL if hindi else config.LIVE_STT_MODEL}")
    speaker_factory = DeepgramLiveTTS(client, voice=voice, sample_rate=RATE)
    clips: list[bytes] = []
    async with speaker_factory.open() as speaker:
        output = speaker.output()
        for phrase in phrases:
            started = time.perf_counter()
            await speaker.say(phrase)
            await speaker.end_reply()
            tts_chars += len(phrase)
            audio, first = b"", None
            async for item in output:
                if isinstance(item, SpokenAudio):
                    first = first or time.perf_counter() - started
                    audio += item.data
                elif isinstance(item, SegmentDone):
                    break
            clips.append(audio)
            heard = f"first audio after {first:.2f} s" if first is not None else "NO AUDIO"
            print(f"  spoke {len(audio) / 2 / RATE:.1f} s, {heard}: {phrase!r}")

    if not hindi:
        listener_factory = DeepgramLiveSTT(
            client, model=config.LIVE_STT_MODEL, eot_threshold=config.EOT_THRESHOLD,
            eot_timeout_ms=config.EOT_TIMEOUT_MS,
        )
        return await hear(listener_factory, clips, KEYTERMS)
    listener_factory = DeepgramLiveSTT(
        client, model=config.HINDI_STT_MODEL, eot_threshold=config.EOT_THRESHOLD,
        eot_timeout_ms=config.EOT_TIMEOUT_MS, language_hints=config.HINDI_LANGUAGE_HINTS,
    )
    print(f"  -- with the account names as keyterms {HINDI_KEYTERMS}:")
    try:
        streamed = await hear(listener_factory, clips, HINDI_KEYTERMS)
    except ApiError as exc:
        print(f"  the multilingual listener refused keyterms: HTTP {exc.status_code}")
        streamed = 0.0
    print("  -- without keyterms:")
    return streamed + await hear(listener_factory, clips, [])


async def hear(listener_factory: DeepgramLiveSTT, clips: list[bytes], keyterms: list[str]) -> float:
    """Stream the clips into one listener at real-time pace and print its turns."""
    silence = bytes(FRAME_BYTES)
    phrase_ends: list[float] = []
    streamed_seconds = 0.0
    async with listener_factory.open(sample_rate=RATE, keyterms=keyterms) as listener:
        started = time.perf_counter()

        async def send(frames: bytes) -> None:
            nonlocal streamed_seconds
            for offset in range(0, len(frames), FRAME_BYTES):
                await listener.send_audio(frames[offset:offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\0"))
                streamed_seconds += 0.08
                await asyncio.sleep(0.08)  # real-time pace

        async def stream() -> None:
            await send(silence * 6)
            for clip in clips:
                await send(clip)
                phrase_ends.append(time.perf_counter() - started)
                await send(silence * 25)  # 2 s of quiet between turns
            await listener.finish()

        sender = asyncio.create_task(stream())
        async for event in listener.events():
            now = time.perf_counter() - started
            if event.kind == "end_of_turn":
                last_end = max((e for e in phrase_ends if e <= now), default=None)
                lag = f" ({now - last_end:.2f} s after the audio ended)" if last_end is not None else ""
                confidences = event.word_confidences
                stats = (f" mean {statistics.mean(confidences):.2f}, min {min(confidences):.2f}"
                         if confidences else "")
                print(f"  {now:6.2f} s END OF TURN{lag}: {event.text!r}{stats}")
            elif event.kind == "speech_started":
                print(f"  {now:6.2f} s speech started")
        await sender
    return streamed_seconds


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--idle", type=int, default=30, help="seconds to leave the speak socket idle (0 to skip)")
    parser.add_argument("--hindi", action="store_true", help="check a Hindi call's voice and listener instead")
    args = parser.parse_args()
    client = AsyncDeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])
    await voice_facts(client, config.HINDI_TTS_VOICE if args.hindi else config.TTS_VOICE, args.idle)
    streamed = await adapters_end_to_end(client, hindi=args.hindi)
    print(f"\nDeepgram use: {tts_chars} characters spoken (~${deepgram_tts_cost(tts_chars):.3f}), "
          f"{streamed:.0f} s streamed to Flux (~${streamed / 60 * FLUX_PER_MINUTE:.3f}); from the free credit")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
