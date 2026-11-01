"""A Twilio media stream as the live engine's CallerLink (M7.1).

Twilio sends the caller's audio as base64 μ-law 8 kHz `media` messages and
plays ours in order. A `mark` we send after a segment comes back once the
audio before it has played: that is the phone's "segment played" report, so
heard-tracking and the read-back rule work on a phone call. On an
interruption we send `clear`; Twilio then returns every mark still queued,
and any mark arriving after our `clear` counts as not played (the safe side:
an unsure read-back is read again).

Until the caller is verified (M7.2) the link is `verifying`: caller audio is
dropped, never reaching speech-to-text, and keypad digits go only to
`keypad()` for the PIN check; they are never logged or kept. Screen events
(captions, sentences) go to `watch` when set, so the account holder's
signed-in page can follow the call; otherwise they are dropped.
"""

import asyncio
import base64
import binascii
import json
import logging
import time
from collections.abc import AsyncIterator, Callable

from backend.app.voice.live_call import (
    AgentAudio,
    CallEvent,
    PlaybackStopped,
    SegmentEnd,
    SegmentPlayed,
    StopPlayback,
)

logger = logging.getLogger(__name__)

PHONE_BYTES_PER_SECOND = 8_000  # μ-law: one byte a sample
AUDIO_RATE_LIMIT = 1.25  # as for the browser (api/live.py)
AUDIO_SLACK_S = 2.0
_END = object()


def mark_name(reply: int, segment: int) -> str:
    return f"{reply}:{segment}"


def _parse_mark(name: object) -> tuple[int, int] | None:
    try:
        reply, segment = str(name).split(":")
        return int(reply), int(segment)
    except ValueError:
        return None


class TwilioLink:
    def __init__(self, websocket, stream_sid: str, clock: Callable[[], float] = time.monotonic, *,
                 verifying: bool = False, watch: Callable[[CallEvent], None] | None = None) -> None:
        self._websocket = websocket
        self._stream_sid = stream_sid
        self._clock = clock
        self._started = clock()
        self._audio_seconds = 0.0
        self._items: asyncio.Queue = asyncio.Queue()
        self._played: dict[int, set[int]] = {}  # reply -> segments whose marks came back before a clear
        self._cleared: set[int] = set()
        self._send_lock = asyncio.Lock()
        self.closed = False
        self.violation: str | None = None
        self.keypad_presses = 0
        self.ended = False  # Twilio's side has gone (hang-up, stop, broken frame)
        self.verifying = verifying
        self.watch = watch
        self._digits: asyncio.Queue = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        # Marks sent and not yet back: audio Twilio still has to play.
        self._unplayed_marks: set[str] = set()
        self._all_played = asyncio.Event()
        self._all_played.set()
        self._prompts = 0

    # --- from Twilio ---

    async def _read(self) -> None:
        try:
            while True:
                message = await self._websocket.receive()
                if message["type"] == "websocket.disconnect":
                    self.closed = True
                    return
                try:
                    frame = json.loads(message.get("text") or "")
                except ValueError:
                    self.violation = "not_json"
                    return
                event = frame.get("event") if isinstance(frame, dict) else None
                if event == "media":
                    try:
                        audio = base64.b64decode(frame["media"]["payload"], validate=True)
                    except (KeyError, TypeError, binascii.Error):
                        self.violation = "bad_media"
                        return
                    self._audio_seconds += len(audio) / PHONE_BYTES_PER_SECOND
                    if self._audio_seconds > (self._clock() - self._started) * AUDIO_RATE_LIMIT + AUDIO_SLACK_S:
                        self.violation = "audio_faster_than_real_time"
                        return
                    if not self.verifying:  # nothing said before verification is transcribed
                        self._items.put_nowait(audio)
                elif event == "mark":
                    name = (frame.get("mark") or {}).get("name")
                    self._unplayed_marks.discard(str(name))  # played, or returned by a clear: either way done
                    if not self._unplayed_marks:
                        self._all_played.set()
                    parsed = _parse_mark(name)
                    if parsed is not None and parsed[0] not in self._cleared:
                        reply, segment = parsed
                        self._played.setdefault(reply, set()).add(segment)
                        self._items.put_nowait(SegmentPlayed(reply, segment))
                elif event == "dtmf":
                    self.keypad_presses += 1
                    if self.verifying:  # only the PIN check sees digits, and only once
                        self._digits.put_nowait(str((frame.get("dtmf") or {}).get("digit", "")))
                elif event == "stop":
                    return
                # "connected", "start" (read by the route) and anything newer: ignored
        finally:
            self.ended = True
            self._all_played.set()  # nothing more will play
            self._items.put_nowait(_END)
            self._digits.put_nowait(None)

    def _ensure_reader(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read())

    async def keypad(self, timeout: float) -> str | None:
        """The next key pressed while verifying; None on timeout or hang-up."""
        self._ensure_reader()
        try:
            return await asyncio.wait_for(self._digits.get(), timeout)
        except TimeoutError:
            return None

    def start_talking(self) -> None:
        """Verified: from now on caller audio goes to the call."""
        self.verifying = False
        while not self._digits.empty():  # nothing typed earlier lingers
            self._digits.get_nowait()

    async def incoming(self) -> AsyncIterator[bytes | SegmentPlayed | PlaybackStopped]:
        # The reader outlives this: after the engine stops listening, `drain`
        # still needs Twilio's marks. The route calls `stop_reading` at the end.
        self._ensure_reader()
        while (item := await self._items.get()) is not _END:
            yield item

    def stop_reading(self) -> None:
        if self._reader is not None:
            self._reader.cancel()

    # --- to Twilio ---

    async def _send(self, message: dict) -> None:
        async with self._send_lock:
            if self.closed:
                return
            try:
                await self._websocket.send_text(json.dumps({**message, "streamSid": self._stream_sid}))
            except Exception:  # the call is gone; the engine finds out via incoming()
                logger.debug("Twilio stream gone; dropping a message")
                self.closed = True

    async def play(self, audio: bytes) -> None:
        """Audio outside a call turn (the verification prompts), with a mark of
        its own so `drain` can wait for it."""
        await self._send({"event": "media", "media": {"payload": base64.b64encode(audio).decode()}})
        self._prompts += 1
        await self._mark(f"prompt:{self._prompts}")

    async def _mark(self, name: str) -> None:
        self._unplayed_marks.add(name)
        self._all_played.clear()
        await self._send({"event": "mark", "mark": {"name": name}})

    async def drain(self, timeout: float) -> bool:
        """Wait until Twilio has played everything sent (every mark is back),
        so closing the stream doesn't cut off the last words. False if it gave
        up after `timeout` seconds."""
        self._ensure_reader()
        try:
            await asyncio.wait_for(self._all_played.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def send(self, event: CallEvent) -> None:
        if not isinstance(event, AgentAudio) and self.watch is not None:
            self.watch(event)
        if isinstance(event, AgentAudio):
            await self._send({"event": "media", "media": {"payload": base64.b64encode(event.pcm).decode()}})
        elif isinstance(event, SegmentEnd):
            await self._mark(mark_name(event.reply, event.segment))
        elif isinstance(event, StopPlayback):
            self._cleared.add(event.reply)
            await self._send({"event": "clear"})
            # What played is known now: marks that came back before the clear.
            played = tuple(sorted(self._played.get(event.reply, ())))
            self._items.put_nowait(PlaybackStopped(event.reply, played))
        # Captions, sentences and the rest are for a screen: only `watch` gets them.
