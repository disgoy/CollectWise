"""Phone calls through Twilio (M7.1): the number's voice webhook and its
media stream, bridged to the same live call engine as the browser.

Both routes are open to the internet but answer only Twilio: every request
must carry a valid `X-Twilio-Signature` for `COLLECTWISE_PUBLIC_BASE_URL`,
and a stream must also present the one-time pass our webhook issued for that
exact call (stream_tokens). Off unless TWILIO_AUTH_TOKEN and the public base
URL are set.

Who is calling (M7.2): the stream starts by verifying the caller in code —
their number must belong to a customer and they must type that customer's
phone PIN — before any conversation exists. Only then is the CallerContext
created, and the same live agent takes over the same stream. The account
holder's signed-in page can watch the call's captions (GET /telephony/watch).
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from backend.app import config
from backend.app.agent.llm import LLMClient
from backend.app.agent.sessions import SessionStore, SessionStoreFullError
from backend.app.api.live import _account_labels, _live_sessions, _start_token, encode_event
from backend.app.api.providers import get_db_factory, get_live_llm, get_live_stt, get_live_tts, get_session_store
from backend.app.api.rate_limits import SlidingWindowLimiter, get_turn_limiter
from backend.app.auth.dependencies import SessionSecretNotConfigured, customer_id_for_token
from backend.app.auth.session_tokens import InvalidSessionToken
from backend.app.services.conversation_events import ConversationEvent, record_conversation_events
from backend.app.services.phone_identity import InvalidPhoneNumberError, check_phone_pin, normalize_phone
from backend.app.telephony.stream_tokens import InvalidStreamToken, StreamTokens, issue
from backend.app.telephony.twilio_link import TwilioLink
from backend.app.telephony.verification import NOT_VERIFIED, verify_caller
from backend.app.telephony.watchers import watchers
from backend.app.voice.live_call import run_live_call
from backend.app.voice.live_speech import PHONE_AUDIO, LiveSpeechToText, LiveTextToSpeech, SegmentDone, SpokenAudio
from backend.app.voice.speech import SpeechUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telephony/twilio", tags=["Telephony"])
# Watching a phone call isn't tied to the provider.
watch_router = APIRouter(prefix="/telephony", tags=["Telephony"])

VOICE_PATH = "/telephony/twilio/voice"
STREAM_PATH = "/telephony/twilio/stream"
START_TIMEOUT_S = 10.0
DRAIN_S = 15.0  # the longest the end of a call waits for its last words to play
LINE_UNAVAILABLE = "Sorry, this line isn't available right now. Goodbye."

# Stream close codes, as for the browser's live calls (api/live.py).
CLOSE_NORMAL = 1000
CLOSE_MALFORMED = 4400
CLOSE_BAD_PASS = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_START_TIMEOUT = 4408
CLOSE_ALREADY_LIVE = 4409
CLOSE_NOT_CONFIGURED = 4503

_stream_tokens = StreamTokens()


def telephony_configured() -> bool:
    return bool(config.TWILIO_AUTH_TOKEN and config.PUBLIC_BASE_URL and config.SESSION_TOKEN_SECRET)


def public_url(path: str, *, websocket: bool = False) -> str:
    base = config.PUBLIC_BASE_URL.rstrip("/")
    if websocket:
        base = "wss://" + base.split("://", 1)[1]
    return base + path


def signed_by_twilio(url: str, params: dict, signature: str | None) -> bool:
    return bool(signature) and RequestValidator(config.TWILIO_AUTH_TOKEN).validate(url, params, signature)


def _twiml(response: VoiceResponse) -> Response:
    return Response(str(response), media_type="application/xml")


def _line_unavailable() -> Response:
    response = VoiceResponse()
    response.say(LINE_UNAVAILABLE)
    response.hangup()
    return _twiml(response)


@router.post("/voice")
async def voice_webhook(request: Request) -> Response:
    """Twilio calls this when the number rings: answer with a media stream.
    Who is calling is decided on the stream, by their phone PIN."""
    if not telephony_configured():
        return Response(status_code=503)
    params = dict(await request.form())
    if not signed_by_twilio(public_url(VOICE_PATH), params, request.headers.get("x-twilio-signature")):
        logger.info("Rejected an unsigned or wrongly signed telephony webhook")
        return Response(status_code=403)

    call_sid = str(params.get("CallSid", ""))
    if not call_sid or "." in call_sid:
        return _line_unavailable()
    try:
        caller_number = normalize_phone(str(params.get("From", "")))
    except InvalidPhoneNumberError:
        caller_number = ""  # withheld or not a phone: the PIN check will fail, the same way

    stream = Stream(url=public_url(STREAM_PATH, websocket=True))
    stream.parameter(name="pass", value=issue(call_sid, caller_number, secret=config.SESSION_TOKEN_SECRET.encode()))
    connect = Connect()
    connect.append(stream)
    response = VoiceResponse()
    response.append(connect)
    response.hangup()  # when the stream ends, so does the call
    return _twiml(response)


async def _start_frame(websocket: WebSocket) -> dict | int:
    """Twilio's `start` message, or the close code to use."""
    try:
        while True:
            message = await asyncio.wait_for(websocket.receive(), START_TIMEOUT_S)
            if message["type"] == "websocket.disconnect":
                return CLOSE_NORMAL
            frame = json.loads(message.get("text") or "")
            if isinstance(frame, dict) and frame.get("event") == "start":
                return frame
            if not (isinstance(frame, dict) and frame.get("event") == "connected"):
                return CLOSE_MALFORMED
    except TimeoutError:
        return CLOSE_START_TIMEOUT
    except ValueError:
        return CLOSE_MALFORMED


@router.websocket("/stream")
async def media_stream(
    websocket: WebSocket,
    store: SessionStore = Depends(get_session_store),
    llm: LLMClient | None = Depends(get_live_llm),
    stt: LiveSpeechToText | None = Depends(get_live_stt),
    tts: LiveTextToSpeech | None = Depends(get_live_tts),
    db_factory: Callable = Depends(get_db_factory),
    turn_limiter: SlidingWindowLimiter = Depends(get_turn_limiter),
) -> None:
    # Checked before accepting: an unsigned handshake gets no WebSocket at all.
    if not telephony_configured():
        await websocket.close()
        return
    if not signed_by_twilio(public_url(STREAM_PATH, websocket=True), {}, websocket.headers.get("x-twilio-signature")):
        # Said out loud: on a real line this is otherwise silence, then a hang-up.
        logger.info("Rejected an unsigned or wrongly signed telephony stream")
        await websocket.close()
        return
    await websocket.accept()

    start = await _start_frame(websocket)
    if isinstance(start, int):
        await websocket.close(code=start)
        return
    details = start.get("start") or {}
    call_sid = str(details.get("callSid", ""))
    try:
        caller_number = _stream_tokens.redeem(
            str((details.get("customParameters") or {}).get("pass", "")), call_sid,
            secret=config.SESSION_TOKEN_SECRET.encode(),
        )
    except InvalidStreamToken as exc:
        logger.info("Rejected a telephony stream pass: %s", exc)  # the reason, never the pass
        await websocket.close(code=CLOSE_BAD_PASS)
        return
    if llm is None or stt is None or tts is None:
        await websocket.close(code=CLOSE_NOT_CONFIGURED)
        return

    link = TwilioLink(websocket, str(start.get("streamSid") or details.get("streamSid") or ""), verifying=True)
    try:
        await _answer(link, websocket, store, llm, stt, tts, db_factory, turn_limiter, caller_number,
                      f"call_{call_sid}"[:64])
    finally:
        link.stop_reading()


async def _answer(link: TwilioLink, websocket: WebSocket, store: SessionStore, llm: LLMClient,
                  stt: LiveSpeechToText, tts: LiveTextToSpeech, db_factory: Callable,
                  turn_limiter: SlidingWindowLimiter, caller_number: str, call_ref: str) -> None:
    """Verify the caller, then run the agent on the same stream."""
    customer_id = await _verify(link, tts, db_factory, caller_number, call_ref)
    session = None
    if customer_id is not None:
        try:
            session = store.create(customer_id)
        except SessionStoreFullError:
            logger.warning("Session store full: a verified phone caller couldn't be served")
    if session is None:
        if not link.ended:
            await websocket.close(code=CLOSE_NORMAL)
        return

    await asyncio.to_thread(_record, db_factory, session.session_id, customer_id, "caller_verified", "verified")
    _live_sessions.add(session.session_id)
    link.watch = lambda event: watchers.publish(customer_id, encode_event(event))
    watchers.publish(customer_id, json.dumps({"type": "phone_call_started"}))
    try:
        keyterms = await asyncio.to_thread(_account_labels, db_factory, customer_id)
        await run_live_call(
            session, link, stt=stt, tts=tts, llm=llm, db_factory=db_factory,
            keyterms=keyterms, turn_limiter=turn_limiter, caller_audio=PHONE_AUDIO, reply_audio=PHONE_AUDIO,
        )
        # The agent's last words (a handoff, a time-limit goodbye) are still in
        # Twilio's buffer: let them play before the stream, and the call, ends.
        await link.drain(DRAIN_S)
        if not link.closed:
            await websocket.close(code=CLOSE_MALFORMED if link.violation else CLOSE_NORMAL)
    finally:
        _live_sessions.discard(session.session_id)


def _record(db_factory: Callable, session_id: str, customer_id: str, event_type: str, outcome: str) -> None:
    db = db_factory()
    try:
        record_conversation_events(db, [ConversationEvent(
            session_id=session_id, customer_id=customer_id, turn=0, event_type=event_type, outcome=outcome)])
    finally:
        db.close()


async def _verify(link: TwilioLink, tts: LiveTextToSpeech, db_factory: Callable, caller_number: str,
                  call_ref: str) -> str | None:
    """The verified customer, or None after the goodbye (M7.2)."""
    try:
        async with tts.open(PHONE_AUDIO) as speaker:
            async def speak(text: str) -> None:
                await speaker.say(text)
                await speaker.end_reply()
                async for item in speaker.output():
                    if isinstance(item, SpokenAudio):
                        await link.play(item.data)
                    elif isinstance(item, SegmentDone):
                        break
                if text == NOT_VERIFIED:  # let the goodbye play before the stream closes
                    await link.drain(DRAIN_S)

            def check(pin: str):
                db = db_factory()
                try:
                    return check_phone_pin(db, caller_number, pin, now=datetime.now(timezone.utc))
                finally:
                    db.close()

            async def failed(reason: str) -> None:
                logger.info("Phone verification failed for %s: %s", call_ref[-8:], reason)
                await asyncio.to_thread(_record, db_factory, call_ref, "unverified", "verification_failed", reason)

            return await verify_caller(
                keypad=link.keypad, speak=speak, check=lambda pin: asyncio.to_thread(check, pin),
                hung_up=lambda: link.ended, failed=failed, verified=link.start_talking,
            )
    except SpeechUnavailableError:
        logger.warning("Phone verification couldn't speak (voice unavailable) for %s", call_ref[-8:])
        return None


@watch_router.websocket("/watch")
async def watch_phone_calls(websocket: WebSocket) -> None:
    """The account holder's page follows their own verified phone calls:
    captions and sentences live, nothing stored. Signs in by its first
    message, like the live call."""
    await websocket.accept()
    token = await _start_token(websocket)
    if isinstance(token, int):
        await websocket.close(code=token)
        return
    try:
        customer_id = customer_id_for_token(token)
    except (InvalidSessionToken, SessionSecretNotConfigured):
        await websocket.close(code=CLOSE_BAD_PASS)
        return
    queue = watchers.subscribe(customer_id)
    await websocket.send_text(json.dumps({"type": "watching"}))
    leaving = asyncio.create_task(websocket.receive())
    try:
        while True:
            message = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({message, leaving}, return_when=asyncio.FIRST_COMPLETED)
            if leaving in done:
                message.cancel()
                return
            await websocket.send_text(message.result())
    finally:
        leaving.cancel()
        watchers.unsubscribe(customer_id, queue)
