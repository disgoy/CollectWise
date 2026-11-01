"""Phone calls through Twilio (M7.1): only Twilio can start one, a stream needs
our one-time pass for that call, phone audio goes both ways in μ-law 8 kHz
without conversion, and Twilio's marks drive heard-tracking, including after
an interruption."""

import asyncio
import base64
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from backend.app import config
from backend.app.agent.sessions import SessionStore
from backend.app.api import telephony
from backend.app.api.providers import get_db_factory, get_live_llm, get_live_stt, get_live_tts, get_session_store
from backend.app.db import models
from backend.app.main import app
from backend.app.telephony import stream_tokens, verification
from backend.app.telephony.stream_tokens import InvalidStreamToken, StreamTokens, issue
from backend.app.telephony.twilio_link import TwilioLink
from backend.app.voice.live_call import (
    AgentAudio,
    AgentSentence,
    Caption,
    PlaybackStopped,
    ReplyDone,
    SegmentEnd,
    SegmentPlayed,
    StopPlayback,
    run_live_call,
)
from backend.app.voice.live_speech import PHONE_AUDIO
from backend.tests.fake_live_speech import FakeLiveSTT, FakeLiveTTS, ScriptedListener
from backend.tests.fake_llm import FakeLLM, say, use
from backend.tests.test_live_call import PROPOSE, READ_BACK, db_factory, payment_records  # noqa: F401  (fixture)

TWILIO_TOKEN = "test-twilio-auth-token"
BASE = "https://cw.example.test"
VOICE_URL = f"{BASE}/telephony/twilio/voice"
STREAM_URL = "wss://cw.example.test/telephony/twilio/stream"
SECRET = b"test-only-session-secret-0123456789abcdef"
MEDIA = base64.b64encode(b"\xff" * 160).decode()  # 20 ms of μ-law silence
CALLER = "+15555550100"
PIN = "482916"


def sign(url: str, params: dict) -> str:
    return RequestValidator(TWILIO_TOKEN).compute_signature(url, params)


@pytest.fixture
def phone(client: TestClient, monkeypatch):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", TWILIO_TOKEN)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(telephony, "_stream_tokens", StreamTokens())
    assert client.post("/customers", json={"customer_id": "cust_a", "display_name": "Synthetic"}).status_code == 201
    assert client.post("/accounts", json={"account_id": "acct_a_1", "customer_id": "cust_a", "balance": "500.00",
                                          "currency": "USD", "label": "main"}).status_code == 201
    assert client.put("/customers/cust_a/phone", json={"phone_number": CALLER, "pin": PIN}).status_code == 204

    class Phone:
        store = SessionStore()
        llm = FakeLLM([say("Your main account has 500 US dollars.")])
        stt = FakeLiveSTT(ScriptedListener({3: "What's my balance?"}))
        tts = FakeLiveTTS()

    app.dependency_overrides[get_session_store] = lambda: Phone.store
    app.dependency_overrides[get_live_llm] = lambda: Phone.llm
    app.dependency_overrides[get_live_stt] = lambda: Phone.stt
    app.dependency_overrides[get_live_tts] = lambda: Phone.tts
    return Phone


def ring(client, call_sid="CA0001", *, signature=None, caller=CALLER):
    params = {"CallSid": call_sid, "From": caller, "To": "+15555550199"}
    headers = {"X-Twilio-Signature": sign(VOICE_URL, params) if signature is None else signature}
    return client.post("/telephony/twilio/voice", data=params, headers=headers)


def stream_pass(twiml: str) -> str:
    return re.search(r'<Parameter name="pass" value="([^"]+)"', twiml).group(1)


def start_frame(call_sid: str, pass_: str) -> str:
    return json.dumps({"event": "start", "streamSid": "MZ0001", "start": {
        "streamSid": "MZ0001", "callSid": call_sid, "customParameters": {"pass": pass_},
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}})


def hear_prompts(ws, *prompts: str) -> list[dict]:
    """Twilio's side: play the prompts (the fake voice: 10 bytes a character)
    and send back each prompt's mark, before the caller goes on."""
    frames, heard, marks = [], 0, 0
    expected = 10 * sum(len(p) for p in prompts)
    while heard < expected or marks < len(prompts):
        frame = json.loads(ws.receive_text())
        frames.append(frame)
        if frame["event"] == "media":
            heard += len(base64.b64decode(frame["media"]["payload"]))
        elif frame["event"] == "mark":
            marks += 1
            ws.send_text(json.dumps({"event": "mark", "mark": frame["mark"]}))
    return frames


def until_agent_mark(ws) -> list[dict]:
    """Frames up to the agent's first segment mark (prompt marks are played
    back as Twilio would)."""
    frames = []
    while True:
        frame = json.loads(ws.receive_text())
        frames.append(frame)
        if frame["event"] == "mark":
            if not frame["mark"]["name"].startswith("prompt:"):
                return frames
            ws.send_text(json.dumps({"event": "mark", "mark": frame["mark"]}))


def type_pin(ws, digits: str) -> None:
    for digit in digits:
        ws.send_text(json.dumps({"event": "dtmf", "streamSid": "MZ0001", "dtmf": {"track": "inbound", "digit": digit}}))


def open_stream(client, *, signature=None):
    return client.websocket_connect("/telephony/twilio/stream",
                                    headers={"x-twilio-signature": sign(STREAM_URL, {}) if signature is None else signature})


# --- the webhook ---------------------------------------------------------------------------


def test_a_signed_call_gets_a_media_stream_with_a_pass(phone, client) -> None:
    response = ring(client)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert f'<Stream url="{STREAM_URL}">' in response.text
    assert response.text.endswith("</Connect><Hangup /></Response>")  # the call ends with the stream
    assert stream_tokens.StreamTokens().redeem(stream_pass(response.text), "CA0001", secret=SECRET) == CALLER


@pytest.mark.parametrize("signature", ["", "bm90IHRoZSByaWdodCBvbmU="])
def test_an_unsigned_or_wrongly_signed_call_is_refused(phone, client, signature) -> None:
    assert ring(client, signature=signature).status_code == 403
    assert len(phone.store) == 0


def test_a_signature_for_other_parameters_is_refused(phone, client) -> None:
    signed_for_another = sign(VOICE_URL, {"CallSid": "CA9999", "From": "+15555550100", "To": "+15555550199"})

    assert ring(client, "CA0001", signature=signed_for_another).status_code == 403


def test_the_webhook_needs_no_demo_mode_and_creates_no_conversation(phone, client, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEMO_LOGIN", False)

    response = ring(client)

    assert "<Stream" in response.text  # who is calling is decided on the stream, by PIN (M7.2)
    assert len(phone.store) == 0


def test_a_withheld_number_still_gets_the_stream_and_will_fail_the_pin(phone, client) -> None:
    response = ring(client, caller="anonymous")

    assert stream_tokens.StreamTokens().redeem(stream_pass(response.text), "CA0001", secret=SECRET) == ""


@pytest.mark.parametrize("missing", ["TWILIO_AUTH_TOKEN", "PUBLIC_BASE_URL"])
def test_phone_calls_are_off_unless_configured(phone, client, monkeypatch, missing) -> None:
    monkeypatch.setattr(config, missing, None)

    assert ring(client).status_code == 503


# --- the stream: who may open one ------------------------------------------------------------


def test_an_unsigned_stream_gets_no_websocket(phone, client) -> None:
    with pytest.raises(WebSocketDisconnect), open_stream(client, signature="bm9wZQ=="):
        pass


@pytest.mark.parametrize("problem", ["another_call", "made_up", "reused"])
def test_a_stream_needs_our_pass_for_that_call(phone, client, problem) -> None:
    pass_ = stream_pass(ring(client, "CA0001").text)
    if problem == "reused":
        assert stream_tokens.StreamTokens().redeem(pass_, "CA0001", secret=SECRET)  # a different process's copy
        telephony._stream_tokens.redeem(pass_, "CA0001", secret=SECRET)  # this one: now used
    call_sid = "CA0002" if problem == "another_call" else "CA0001"
    sent = "CA0001.+15555550100.9999999999.forged" if problem == "made_up" else pass_

    with open_stream(client) as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        ws.send_text(start_frame(call_sid, sent))
        closed = ws.receive()

    assert closed == {"type": "websocket.close", "code": telephony.CLOSE_BAD_PASS, "reason": ""}


def test_passes_expire_are_single_use_and_tied_to_the_call() -> None:
    now = [1_000_000.0]
    tokens = StreamTokens(clock=lambda: now[0])
    pass_ = issue("CA1", CALLER, secret=SECRET, now=now[0])

    with pytest.raises(InvalidStreamToken, match="another call"):
        tokens.redeem(pass_, "CA2", secret=SECRET)
    with pytest.raises(InvalidStreamToken, match="bad signature"):
        tokens.redeem(pass_, "CA1", secret=b"another-secret-of-thirty-two-bytes!!")
    with pytest.raises(InvalidStreamToken, match="malformed"):
        tokens.redeem("not-a-pass", "CA1", secret=SECRET)
    assert tokens.redeem(pass_, "CA1", secret=SECRET) == CALLER
    with pytest.raises(InvalidStreamToken, match="used"):
        tokens.redeem(pass_, "CA1", secret=SECRET)

    late = issue("CA3", CALLER, secret=SECRET, now=now[0])
    now[0] += stream_tokens.TTL_S
    with pytest.raises(InvalidStreamToken, match="expired"):
        tokens.redeem(late, "CA3", secret=SECRET)
    assert stream_tokens.TTL_S == 60


# --- a whole call through Twilio -----------------------------------------------------------------


def test_a_whole_phone_call_in_mulaw_both_ways(phone, client) -> None:
    pass_ = stream_pass(ring(client, "CA0001").text)
    sent_to_twilio = []

    with open_stream(client) as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        ws.send_text(start_frame("CA0001", pass_))
        type_pin(ws, PIN)
        sent_to_twilio += hear_prompts(ws, f"{verification.GREETING} {verification.ASK_PIN}", verification.VERIFIED)
        for _ in range(3):  # the caller speaks once verified; the third frame ends the turn
            ws.send_text(json.dumps({"event": "media", "streamSid": "MZ0001", "media": {"payload": MEDIA}}))
        sent_to_twilio += until_agent_mark(ws)
        ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ0001", "mark": sent_to_twilio[-1]["mark"]}))
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ0001", "stop": {"callSid": "CA0001"}}))
        closed = ws.receive()

    assert phone.stt.opened_encoding == "mulaw" and phone.stt.opened_with["sample_rate"] == 8000
    assert phone.tts.opened_audio == PHONE_AUDIO
    assert {f["event"] for f in sent_to_twilio} == {"media", "mark"}  # no captions on a phone
    assert all(f["streamSid"] == "MZ0001" for f in sent_to_twilio)
    audio = b"".join(base64.b64decode(f["media"]["payload"]) for f in sent_to_twilio if f["event"] == "media")
    spoken = [f"{verification.GREETING} {verification.ASK_PIN}", verification.VERIFIED,
              "Your main account has 500 US dollars."]
    assert len(audio) == 10 * sum(len(s) for s in spoken)  # the fake speaker's bytes, untouched
    assert sent_to_twilio[-1]["mark"]["name"].startswith("0:")  # reply 0 (segments count on from the prompts)
    assert closed["code"] == 1000


# --- the link: marks, clears and audio -------------------------------------------------------------


class FakeTwilioSocket:
    def __init__(self) -> None:
        self.frames: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def receive(self):
        return await self.frames.get()

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def twilio(self, event: str, **fields) -> None:
        self.frames.put_nowait({"type": "websocket.receive", "text": json.dumps({"event": event, **fields})})

    def hang_up(self) -> None:
        self.frames.put_nowait({"type": "websocket.disconnect"})


async def collect(link, count: int) -> list:
    items = []
    async for item in link.incoming():
        items.append(item)
        if len(items) == count:
            break
    return items


def test_marks_before_a_clear_are_played_and_after_it_are_not() -> None:
    async def go():
        socket = FakeTwilioSocket()
        link = TwilioLink(socket, "MZ1")
        await link.send(AgentAudio(0, 0, b"\x01\x02"))
        await link.send(SegmentEnd(0, 0))
        await link.send(SegmentEnd(0, 1))
        socket.twilio("mark", mark={"name": "0:0"})  # played
        items = await collect(link, 1)
        await link.send(StopPlayback(0))
        socket.twilio("mark", mark={"name": "0:1"})  # returned by the clear: never played
        socket.twilio("mark", mark={"name": "1:0"})  # a later reply: played
        items += await collect(link, 2)
        return socket.sent, items

    sent, items = asyncio.run(go())

    assert [f["event"] for f in sent] == ["media", "mark", "mark", "clear"]
    assert sent[0]["media"]["payload"] == base64.b64encode(b"\x01\x02").decode()
    assert items == [SegmentPlayed(0, 0), PlaybackStopped(0, (0,)), SegmentPlayed(1, 0)]


def test_screen_events_are_dropped_and_keypad_digits_never_kept() -> None:
    async def go():
        socket = FakeTwilioSocket()
        link = TwilioLink(socket, "MZ1")
        for event in (Caption("hi"), AgentSentence(0, 0, "Hello."), ReplyDone(0)):
            await link.send(event)
        socket.twilio("dtmf", dtmf={"track": "inbound", "digit": "7"})
        socket.twilio("media", media={"payload": MEDIA})
        items = await collect(link, 1)
        return socket.sent, items, link

    sent, items, link = asyncio.run(go())

    assert sent == []
    assert items == [b"\xff" * 160]
    assert link.keypad_presses == 1
    assert "7" not in json.dumps({k: v for k, v in vars(link).items() if isinstance(v, (str, int, list, dict))})


def test_audio_faster_than_real_time_ends_the_stream() -> None:
    async def go():
        socket = FakeTwilioSocket()
        link = TwilioLink(socket, "MZ1", clock=lambda: 0.0)  # no time passes
        for _ in range(16):  # 160 ms each: 2.56 s, past the 2 s allowance
            socket.twilio("media", media={"payload": base64.b64encode(b"\xff" * 1280).decode()})
        items = [item async for item in link.incoming()]
        return items, link

    items, link = asyncio.run(go())

    assert link.violation == "audio_faster_than_real_time"
    assert len(items) == 12  # 1.92 s let through, then cut off


@pytest.mark.parametrize(("frame", "violation"), [("not json", "not_json"),
                                                  ('{"event": "media", "media": {"payload": "%%%"}}', "bad_media")])
def test_a_broken_frame_ends_the_stream(frame, violation) -> None:
    async def go():
        socket = FakeTwilioSocket()
        link = TwilioLink(socket, "MZ1")
        socket.frames.put_nowait({"type": "websocket.receive", "text": frame})
        return [item async for item in link.incoming()], link

    items, link = asyncio.run(go())

    assert items == [] and link.violation == violation


# --- heard-tracking on the phone, through the engine ---------------------------------------------------


async def _phone_call(db_factory, llm, play):
    """Run a call over TwilioLink; `play(socket, listener)` acts as Twilio and the caller."""
    from backend.app.agent.sessions import SessionStore as Store

    session = Store().create("cust_a")
    socket = FakeTwilioSocket()
    stt, tts = FakeLiveSTT(), FakeLiveTTS()
    task = asyncio.create_task(run_live_call(
        session, TwilioLink(socket, "MZ1"), stt=stt, tts=tts, llm=llm, db_factory=db_factory, system="s",
        caller_audio=PHONE_AUDIO, reply_audio=PHONE_AUDIO))
    await play(socket, stt.listener)
    socket.hang_up()
    return await asyncio.wait_for(task, 5), socket


async def _until(socket, event: str, count: int = 1) -> None:
    for _ in range(200):
        if sum(f["event"] == event for f in socket.sent) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Twilio never got {count} {event}")


def test_a_read_back_marked_as_played_can_be_confirmed_on_the_phone(db_factory) -> None:
    llm = FakeLLM([use(PROPOSE), say(READ_BACK), use(("t2", "confirm_payment", {})), say("Recorded.")])

    async def play(socket, listener):
        listener.say_turn("Pay 120 from my account")
        await _until(socket, "mark", 2)  # the read-back's two sentences
        await asyncio.sleep(0.05)
        for frame in [f for f in socket.sent if f["event"] == "mark"]:
            socket.twilio("mark", mark=frame["mark"])  # all of it played
        await asyncio.sleep(0.05)
        listener.say_turn("Yes, go ahead")
        await _until(socket, "mark", 3)

    asyncio.run(_phone_call(db_factory, llm, play))

    assert payment_records(db_factory) == 1


def test_a_read_back_cleared_by_an_interruption_cant_be_confirmed_on_the_phone(db_factory) -> None:
    llm = FakeLLM([use(PROPOSE), say(READ_BACK), use(("t2", "confirm_payment", {})), say("I'll read it again.")])

    async def play(socket, listener):
        listener.say_turn("Pay 120 from my account")
        await _until(socket, "mark")
        listener.hear("update", "Yes yes go ahead")  # over the read-back, before it played
        await _until(socket, "clear")
        socket.twilio("mark", mark={"name": socket.sent[-2]["mark"]["name"]})  # returned by the clear
        listener.hear("end_of_turn", "Yes yes go ahead", (0.95, 0.95, 0.95, 0.95))
        await _until(socket, "mark", 2)

    asyncio.run(_phone_call(db_factory, llm, play))

    assert payment_records(db_factory) == 0
    assert '"error": "readback_not_heard"' in llm.requests[-1]["messages"][-1]["content"][0]["content"]


# --- the last words play out before a phone call ends ------------------------------------


def still_open_until(ws, release, wait_s: float = 0.3):
    """The stream stays open (no close arrives) until `release()` — Twilio
    saying the last audio has played — and then closes."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        closing = pool.submit(ws.receive)
        try:
            closing.result(timeout=wait_s)
            raise AssertionError("the stream closed before its last words had played")
        except TimeoutError:
            pass
        release()
        return closing.result(timeout=5)


def test_drain_waits_until_every_mark_is_back() -> None:
    async def go():
        socket = FakeTwilioSocket()
        link = TwilioLink(socket, "MZ1")
        await link.send(SegmentEnd(0, 0))
        await link.play(b"\x01")  # a prompt gets a mark of its own
        waiting = asyncio.create_task(link.drain(5))
        await asyncio.sleep(0.05)
        before_any = not waiting.done()
        socket.twilio("mark", mark={"name": "0:0"})
        await asyncio.sleep(0.05)
        after_one = not waiting.done()  # one still to play
        socket.twilio("mark", mark={"name": "prompt:1"})
        return before_any, after_one, await waiting, [f["mark"]["name"] for f in socket.sent if f["event"] == "mark"]

    before_any, after_one, drained, marks = asyncio.run(go())

    assert before_any and after_one and drained is True
    assert marks == ["0:0", "prompt:1"]


def test_drain_counts_marks_returned_by_a_clear() -> None:
    async def go():
        socket = FakeTwilioSocket()
        link = TwilioLink(socket, "MZ1")
        await link.send(SegmentEnd(0, 0))
        await link.send(StopPlayback(0))
        socket.twilio("mark", mark={"name": "0:0"})  # returned by the clear: never played, but done
        return await link.drain(5)

    assert asyncio.run(go()) is True


def test_drain_gives_up_at_the_cap_or_when_the_caller_leaves() -> None:
    async def go():
        socket = FakeTwilioSocket()
        link = TwilioLink(socket, "MZ1")
        await link.send(SegmentEnd(0, 0))
        capped = await link.drain(0.1)  # the mark never comes back
        socket.hang_up()
        return capped, await link.drain(5)

    capped, after_hang_up = asyncio.run(go())

    assert capped is False
    assert after_hang_up is True  # nothing more will play: no point waiting
    assert telephony.DRAIN_S == 15.0


def test_a_phone_handoff_closes_only_after_its_last_words_played(phone, client) -> None:
    from backend.tests.fake_llm import use

    phone.llm.script[:] = [use(("t1", "handoff_to_human", {"reason": "caller_requested"})),
                           say("Your conversation has been passed to a member of our team.")]
    pass_ = stream_pass(ring(client).text)

    with open_stream(client) as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(start_frame("CA0001", pass_))
        type_pin(ws, PIN)
        hear_prompts(ws, f"{verification.GREETING} {verification.ASK_PIN}", verification.VERIFIED)
        for _ in range(3):
            ws.send_text(json.dumps({"event": "media", "media": {"payload": MEDIA}}))
        last = until_agent_mark(ws)[-1]
        closed = still_open_until(ws, lambda: ws.send_text(json.dumps({"event": "mark", "mark": last["mark"]})))

    assert closed == {"type": "websocket.close", "code": 1000, "reason": ""}
    assert phone.store and next(iter(phone.store._sessions.values())).state.handoff.reason == "caller_requested"
