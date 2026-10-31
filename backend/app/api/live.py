"""The live call endpoint (M5.5, D83): a WebSocket that connects the caller's
browser to the call engine.

The first message must be `{"type": "start", "token": ...}` (a browser can't
set headers on a WebSocket, and a token in the URL would end up in logs).
Then the caller sends 16 kHz PCM as binary frames and playback reports as
JSON; the server sends JSON events and audio as binary frames that start with
an 8-byte header (reply, segment as little-endian uint32).
"""

import asyncio
import dataclasses
import json
import logging
import struct
import time
from collections.abc import AsyncIterator, Callable

from fastapi import APIRouter, Depends, WebSocket

from backend.app.agent.llm import LLMClient
from backend.app.agent.prompts import SYSTEM_PROMPT
from backend.app.agent.sessions import SessionStore
from backend.app.api.providers import (
    get_db_factory,
    get_live_llm,
    get_live_stt,
    get_live_tts,
    get_session_store,
)
from backend.app.api.rate_limits import SlidingWindowLimiter, get_turn_limiter
from backend.app.auth.dependencies import SessionSecretNotConfigured, customer_id_for_token
from backend.app.auth.session_tokens import InvalidSessionToken
from backend.app.services.accounts import list_customer_account_details
from backend.app.voice.live_call import (
    CALLER_SAMPLE_RATE,
    AgentAudio,
    AgentSentence,
    CallEnded,
    CallerSaid,
    Caption,
    PlaybackStopped,
    ReplyDone,
    SegmentEnd,
    SegmentPlayed,
    StopPlayback,
    run_live_call,
)
from backend.app.voice.live_speech import LiveSpeechToText, LiveTextToSpeech

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["Agent"])

# Close codes. Each tells the caller's app what happened, and nothing else.
CLOSE_NORMAL = 1000
CLOSE_MALFORMED = 4400
CLOSE_BAD_TOKEN = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_AUTH_TIMEOUT = 4408
CLOSE_ALREADY_LIVE = 4409
CLOSE_NOT_CONFIGURED = 4503

AUTH_TIMEOUT_S = 5.0
# 200 ms of 16-bit mono at 16 kHz; 80 ms frames are recommended.
MAX_AUDIO_FRAME_BYTES = CALLER_SAMPLE_RATE * 2 * 200 // 1000
# Flux bills audio length, not wall time: a client may send at most this much
# faster than real time (plus some slack for network bursts).
AUDIO_RATE_LIMIT = 1.25
AUDIO_SLACK_S = 2.0
AUDIO_HEADER = struct.Struct("<II")

_EVENT_TYPES = {
    Caption: "caption",
    CallerSaid: "caller_said",
    AgentSentence: "agent_sentence",
    SegmentEnd: "segment_end",
    StopPlayback: "stop_playback",
    ReplyDone: "reply_done",
    CallEnded: "call_ended",
}

# Sessions with a live call on this server (one at a time per session).
_live_sessions: set[str] = set()


def encode_event(event) -> str | bytes:
    """What the caller's app receives for an engine event."""
    if isinstance(event, AgentAudio):
        return AUDIO_HEADER.pack(event.reply, event.segment) + event.pcm
    return json.dumps({"type": _EVENT_TYPES[type(event)], **dataclasses.asdict(event)})


def _is_int(value) -> bool:
    return type(value) is int and value >= 0  # bool is an int subclass: excluded


class _WebSocketLink:
    """The engine's CallerLink over a FastAPI WebSocket."""

    def __init__(self, websocket: WebSocket, clock: Callable[[], float] = time.monotonic) -> None:
        self._websocket = websocket
        self._clock = clock
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._started = clock()
        self._audio_seconds = 0.0
        # Why the caller's app was cut off, if it broke the protocol.
        self.violation: str | None = None

    def _violate(self, reason: str) -> None:
        logger.info("Live caller broke the protocol: %s", reason)
        self.violation = reason

    async def incoming(self) -> AsyncIterator[bytes | SegmentPlayed | PlaybackStopped]:
        while True:
            message = await self._websocket.receive()
            if message["type"] == "websocket.disconnect":
                self._closed = True
                return
            data, text = message.get("bytes"), message.get("text")
            if data is not None:
                if not data or len(data) % 2 or len(data) > MAX_AUDIO_FRAME_BYTES:
                    self._violate("bad_audio_frame")
                    return
                self._audio_seconds += len(data) / 2 / CALLER_SAMPLE_RATE
                allowed = (self._clock() - self._started) * AUDIO_RATE_LIMIT + AUDIO_SLACK_S
                if self._audio_seconds > allowed:
                    self._violate("audio_faster_than_real_time")
                    return
                yield data
            elif text is not None:
                try:
                    report = json.loads(text)
                except ValueError:
                    self._violate("not_json")
                    return
                if not isinstance(report, dict):
                    self._violate("not_an_object")
                    return
                kind = report.get("type")
                if kind == "hang_up":
                    return
                if kind == "segment_played":
                    if not (_is_int(report.get("reply")) and _is_int(report.get("segment"))):
                        self._violate("bad_segment_played")
                        return
                    yield SegmentPlayed(report["reply"], report["segment"])
                elif kind == "playback_stopped":
                    played = report.get("played")
                    if not (_is_int(report.get("reply")) and isinstance(played, list) and all(map(_is_int, played))):
                        self._violate("bad_playback_stopped")
                        return
                    yield PlaybackStopped(report["reply"], tuple(played))
                # Anything else is ignored, so a newer app works with this server.

    async def send(self, event) -> None:
        payload = encode_event(event)
        async with self._send_lock:
            if self._closed:
                return
            try:
                if isinstance(payload, bytes):
                    await self._websocket.send_bytes(payload)
                else:
                    await self._websocket.send_text(payload)
            except Exception:  # the caller left; the engine finds out via incoming()
                logger.debug("Live caller gone; dropping a message")
                self._closed = True


async def _start_token(websocket: WebSocket) -> str | int:
    """The token from the first message, or the close code to use."""
    try:
        message = await asyncio.wait_for(websocket.receive(), AUTH_TIMEOUT_S)
    except TimeoutError:
        return CLOSE_AUTH_TIMEOUT
    if message["type"] == "websocket.disconnect":
        return CLOSE_NORMAL
    try:
        start = json.loads(message.get("text") or "")
    except ValueError:
        return CLOSE_MALFORMED
    if not isinstance(start, dict) or start.get("type") != "start" or not isinstance(start.get("token"), str):
        return CLOSE_MALFORMED
    return start["token"]


def _account_labels(db_factory: Callable, customer_id: str) -> list[str]:
    db = db_factory()
    try:
        return [d.label for d in list_customer_account_details(db, customer_id) if d.label]
    finally:
        db.close()


@router.websocket("/sessions/{session_id}/live")
async def live_call(
    websocket: WebSocket,
    session_id: str,
    store: SessionStore = Depends(get_session_store),
    llm: LLMClient | None = Depends(get_live_llm),
    stt: LiveSpeechToText | None = Depends(get_live_stt),
    tts: LiveTextToSpeech | None = Depends(get_live_tts),
    db_factory: Callable = Depends(get_db_factory),
    turn_limiter: SlidingWindowLimiter = Depends(get_turn_limiter),
) -> None:
    await websocket.accept()

    token = await _start_token(websocket)
    if isinstance(token, int):
        await websocket.close(code=token)
        return
    try:
        customer_id = customer_id_for_token(token)
    except InvalidSessionToken as exc:
        logger.info("Rejected live session token: %s", exc)  # the reason, never the token
        await websocket.close(code=CLOSE_BAD_TOKEN)
        return
    except SessionSecretNotConfigured:
        await websocket.close(code=CLOSE_NOT_CONFIGURED)
        return

    # Someone else's session looks exactly like a missing one.
    session = store.get_for_customer(session_id, customer_id)
    if session is None:
        await websocket.close(code=CLOSE_NOT_FOUND)
        return
    if session_id in _live_sessions:
        await websocket.close(code=CLOSE_ALREADY_LIVE)
        return
    if llm is None or stt is None or tts is None:
        await websocket.close(code=CLOSE_NOT_CONFIGURED)
        return

    _live_sessions.add(session_id)
    try:
        keyterms = await asyncio.to_thread(_account_labels, db_factory, customer_id)
        link = _WebSocketLink(websocket)
        await websocket.send_text(json.dumps({
            "type": "ready", "sample_rate_in": CALLER_SAMPLE_RATE, "sample_rate_out": tts.sample_rate,
        }))
        await run_live_call(
            session, link, stt=stt, tts=tts, llm=llm, db_factory=db_factory, system=SYSTEM_PROMPT,
            keyterms=keyterms, turn_limiter=turn_limiter,
        )
        if not link._closed:
            await websocket.close(code=CLOSE_MALFORMED if link.violation else CLOSE_NORMAL)
    finally:
        _live_sessions.discard(session_id)

