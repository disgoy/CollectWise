"""The live call endpoint (M5.5): sign-in by first message, the message format
both ways, close codes, and the cost guard."""

import asyncio
import json
import logging
import struct

import pytest
from fastapi.testclient import TestClient

from backend.app import config
from backend.app.agent.sessions import SessionStore
from backend.app.api import live, providers
from backend.app.api.live import _WebSocketLink, encode_event
from backend.app.api.providers import get_live_llm, get_live_stt, get_live_tts, get_session_store
from backend.app.api.rate_limits import SlidingWindowLimiter, get_turn_limiter
from backend.app.auth.session_tokens import issue_session_token
from backend.app.main import app
from backend.app.voice.live_call import (
    REPLY_SLOW_DOWN,
    AgentAudio,
    AgentSentence,
    CallEnded,
    CallerSaid,
    CallerSpeaking,
    Caption,
    PlaybackStopped,
    ReplyDone,
    SegmentEnd,
    SegmentPlayed,
    StopPlayback,
)
from backend.tests.fake_live_speech import FakeLiveSTT, FakeLiveTTS, ScriptedListener
from backend.tests.fake_llm import FakeLLM, say

FRAME = b"\x01\x00" * 1280  # 80 ms at 16 kHz


def token_for(customer_id: str) -> str:
    return issue_session_token(customer_id, secret=config.SESSION_TOKEN_SECRET.encode("utf-8"), ttl_seconds=600)


class Live:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.store = SessionStore()
        self.llm = FakeLLM([say("Your main account has 500 US dollars.")])
        self.stt = FakeLiveSTT(ScriptedListener({1: "What's my balance?"}))
        self.tts = FakeLiveTTS()
        app.dependency_overrides[get_session_store] = lambda: self.store
        app.dependency_overrides[get_live_llm] = lambda: self.llm
        app.dependency_overrides[get_live_stt] = lambda: self.stt
        app.dependency_overrides[get_live_tts] = lambda: self.tts
        self.session_id = self.store.create("cust_a").session_id

    def connect(self, session_id: str | None = None):
        return self.client.websocket_connect(f"/agent/sessions/{session_id or self.session_id}/live")


@pytest.fixture
def live_api(client: TestClient) -> Live:
    for customer_id in ("cust_a", "cust_b"):
        created = client.post("/customers", json={"customer_id": customer_id, "display_name": "Synthetic"})
        assert created.status_code == 201
    assert client.post("/accounts", json={
        "account_id": "acct_a_1", "customer_id": "cust_a", "balance": "500.00", "currency": "USD", "label": "main",
    }).status_code == 201
    return Live(client)


def start(ws, customer_id: str = "cust_a") -> None:
    ws.send_text(json.dumps({"type": "start", "token": token_for(customer_id)}))


def receive(ws):
    message = ws.receive()
    if message["type"] == "websocket.close":
        return ("closed", message["code"])
    if message.get("bytes") is not None:
        reply, segment = struct.unpack_from("<II", message["bytes"])
        return ("audio", reply, segment, len(message["bytes"]) - 8)
    return json.loads(message["text"])


def until(ws, predicate) -> list:
    messages = []
    while True:
        message = receive(ws)
        messages.append(message)
        if predicate(message):
            return messages


def is_close(message) -> bool:
    return isinstance(message, tuple) and message[0] == "closed"


def close_code(ws) -> int:
    return until(ws, is_close)[-1][1]


# --- a whole call ---------------------------------------------------------------------


def test_a_call_over_the_websocket(live_api) -> None:
    with live_api.connect() as ws:
        start(ws)
        assert receive(ws) == {"type": "ready", "sample_rate_in": 16_000, "sample_rate_out": 24_000}
        ws.send_bytes(FRAME)
        during = until(ws, lambda m: isinstance(m, dict) and m["type"] == "reply_done")
        ws.send_text(json.dumps({"type": "segment_played", "reply": 0, "segment": 0}))
        ws.send_text(json.dumps({"type": "hang_up"}))
        after = until(ws, is_close)

    assert during.index({"type": "caller_speaking"}) < during.index({"type": "caption", "text": "What's my balance?"})
    assert {"type": "caller_said", "text": "What's my balance?"} in during
    sentence = {"type": "agent_sentence", "reply": 0, "sentence": 0, "text": "Your main account has 500 US dollars."}
    assert sentence in during
    audio = [m for m in during if isinstance(m, tuple) and m[0] == "audio"]
    assert audio and all(m[1:3] == (0, 0) for m in audio) and all(m[3] > 0 for m in audio)
    assert {"type": "segment_end", "reply": 0, "segment": 0} in during
    assert after == [{"type": "call_ended", "reason": "caller_hung_up"}, ("closed", 1000)]
    assert live_api.stt.listener.audio == [FRAME]


def test_the_callers_account_names_reach_the_listener(live_api) -> None:
    with live_api.connect() as ws:
        start(ws)
        receive(ws)
        ws.send_text(json.dumps({"type": "hang_up"}))
        close_code(ws)

    assert live_api.stt.opened_with == {"sample_rate": 16_000, "keyterms": ["main"]}


def test_live_turns_share_the_customers_turn_limit(live_api) -> None:
    limiter = SlidingWindowLimiter(10)
    for _ in range(10):
        limiter.hit("cust_a")
    app.dependency_overrides[get_turn_limiter] = lambda: limiter

    with live_api.connect() as ws:
        start(ws)
        receive(ws)
        ws.send_bytes(FRAME)
        messages = until(ws, lambda m: isinstance(m, dict) and m["type"] == "reply_done")
        ws.send_text(json.dumps({"type": "hang_up"}))
        close_code(ws)

    assert {"type": "agent_sentence", "reply": 0, "sentence": 0, "text": REPLY_SLOW_DOWN} in messages
    assert live_api.llm.requests == []


def test_leaving_mid_call_frees_the_session_for_a_new_call(live_api) -> None:
    with live_api.connect() as ws:
        start(ws)
        receive(ws)
    # The caller closed the socket without hanging up.
    with live_api.connect() as ws:
        start(ws)
        assert receive(ws)["type"] == "ready"
        ws.send_text(json.dumps({"type": "hang_up"}))
        assert close_code(ws) == 1000


# --- signing in -------------------------------------------------------------------------


def test_no_start_message_in_time(live_api, monkeypatch) -> None:
    monkeypatch.setattr(live, "AUTH_TIMEOUT_S", 0.1)

    with live_api.connect() as ws:
        assert close_code(ws) == 4408


@pytest.mark.parametrize(
    "first",
    ["not json", json.dumps(["start"]), json.dumps({"type": "hello"}), json.dumps({"type": "start"}),
     json.dumps({"type": "start", "token": 123})],
    ids=["not-json", "not-an-object", "wrong-type", "no-token", "token-not-a-string"],
)
def test_a_malformed_start(live_api, first) -> None:
    with live_api.connect() as ws:
        ws.send_text(first)
        assert close_code(ws) == 4400


def test_a_valid_token_in_the_wrong_message_type_is_refused(live_api) -> None:
    with live_api.connect() as ws:
        ws.send_text(json.dumps({"type": "hello", "token": token_for("cust_a")}))
        assert close_code(ws) == 4400


def test_audio_before_signing_in(live_api) -> None:
    with live_api.connect() as ws:
        ws.send_bytes(FRAME)
        assert close_code(ws) == 4400


def test_a_bad_token_is_refused_and_never_logged(live_api, caplog) -> None:
    forged = "eyJleHAiOjEsInN1YiI6ImN1c3RfYSJ9.forged-signature"
    with caplog.at_level(logging.DEBUG), live_api.connect() as ws:
        ws.send_text(json.dumps({"type": "start", "token": forged}))
        assert close_code(ws) == 4401

    assert forged not in caplog.text
    assert "forged-signature" not in caplog.text


def test_someone_elses_session_looks_missing(live_api) -> None:
    with live_api.connect() as ws:
        start(ws, "cust_b")
        assert close_code(ws) == 4404
    with live_api.connect("sess_does_not_exist") as ws:
        start(ws)
        assert close_code(ws) == 4404


def test_one_live_call_per_session(live_api) -> None:
    with live_api.connect() as first:
        start(first)
        assert receive(first)["type"] == "ready"
        with live_api.connect() as second:
            start(second)
            assert close_code(second) == 4409
        first.send_text(json.dumps({"type": "hang_up"}))
        assert close_code(first) == 1000


@pytest.mark.parametrize("missing", [get_live_llm, get_live_stt, get_live_tts], ids=["agent", "listening", "voice"])
def test_not_configured(live_api, missing) -> None:
    app.dependency_overrides[missing] = lambda: None

    with live_api.connect() as ws:
        start(ws)
        assert close_code(ws) == 4503


def test_no_session_secret_means_not_configured(live_api, monkeypatch) -> None:
    token = token_for("cust_a")
    monkeypatch.setattr(config, "SESSION_TOKEN_SECRET", None)

    with live_api.connect() as ws:
        ws.send_text(json.dumps({"type": "start", "token": token}))
        assert close_code(ws) == 4503


def test_the_real_providers_are_off_unless_switched_on() -> None:
    assert config.AGENT_LLM is None and config.SPEECH_PROVIDER is None
    assert providers.get_live_llm() is None
    assert providers.get_live_stt() is None
    assert providers.get_live_tts() is None


# --- the caller breaking the rules --------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [b"", b"\x00" * 2561, b"\x00" * (live.MAX_AUDIO_FRAME_BYTES + 2)],
    ids=["empty", "odd-length", "over-200-ms"],
)
def test_a_bad_audio_frame_ends_the_call(live_api, frame) -> None:
    with live_api.connect() as ws:
        start(ws)
        receive(ws)
        ws.send_bytes(frame)
        assert close_code(ws) == 4400


def test_audio_faster_than_real_time_ends_the_call(live_api) -> None:
    big = b"\x00" * live.MAX_AUDIO_FRAME_BYTES  # 200 ms each
    live_api.stt = FakeLiveSTT(ScriptedListener({}))
    app.dependency_overrides[get_live_stt] = lambda: live_api.stt

    with live_api.connect() as ws:
        start(ws)
        receive(ws)
        for _ in range(20):  # 4 s of audio in an instant
            ws.send_bytes(big)
        assert close_code(ws) == 4400

    # The slack (2 s) let ten frames through before the cut.
    assert 10 <= len(live_api.stt.listener.audio) <= 12


@pytest.mark.parametrize(
    "report",
    ["{not json", json.dumps({"type": "segment_played", "reply": "0", "segment": 0}),
     json.dumps({"type": "segment_played", "reply": True, "segment": 0}),
     json.dumps({"type": "playback_stopped", "reply": 0, "played": [0, "1"]})],
    ids=["broken-json", "string-number", "bool-number", "bad-played-list"],
)
def test_a_malformed_report_ends_the_call(live_api, report) -> None:
    with live_api.connect() as ws:
        start(ws)
        receive(ws)
        ws.send_text(report)
        assert close_code(ws) == 4400


def test_unknown_messages_are_ignored(live_api) -> None:
    with live_api.connect() as ws:
        start(ws)
        receive(ws)
        ws.send_text(json.dumps({"type": "volume", "level": 3}))
        ws.send_text(json.dumps({"type": "start", "token": "again"}))
        ws.send_text(json.dumps({"type": "hang_up"}))
        assert close_code(ws) == 1000


# --- the message format ------------------------------------------------------------------


def test_every_event_has_a_wire_form() -> None:
    assert json.loads(encode_event(CallerSpeaking())) == {"type": "caller_speaking"}
    assert json.loads(encode_event(Caption("hi"))) == {"type": "caption", "text": "hi"}
    assert json.loads(encode_event(CallerSaid("hi"))) == {"type": "caller_said", "text": "hi"}
    assert json.loads(encode_event(AgentSentence(1, 4, "Hello."))) == {
        "type": "agent_sentence", "reply": 1, "sentence": 4, "text": "Hello."}
    assert json.loads(encode_event(SegmentEnd(1, 2))) == {"type": "segment_end", "reply": 1, "segment": 2}
    assert json.loads(encode_event(StopPlayback(1))) == {"type": "stop_playback", "reply": 1}
    assert json.loads(encode_event(ReplyDone(1))) == {"type": "reply_done", "reply": 1}
    assert json.loads(encode_event(CallEnded("silence"))) == {"type": "call_ended", "reason": "silence"}
    assert encode_event(AgentAudio(3, 7, b"\x01\x02")) == struct.pack("<II", 3, 7) + b"\x01\x02"


class ScriptedSocket:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.sent: list = []
        self.gone = False

    async def receive(self) -> dict:
        return self.messages.pop(0) if self.messages else {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        if self.gone:
            raise RuntimeError("closed")
        self.sent.append(text)

    send_bytes = send_text


def test_playback_reports_reach_the_engine() -> None:
    socket = ScriptedSocket([
        {"type": "websocket.receive", "bytes": FRAME},
        {"type": "websocket.receive", "text": json.dumps({"type": "segment_played", "reply": 2, "segment": 5})},
        {"type": "websocket.receive", "text": json.dumps({"type": "playback_stopped", "reply": 2, "played": [5, 6]})},
        {"type": "websocket.receive", "text": json.dumps({"type": "hang_up"})},
        {"type": "websocket.receive", "bytes": FRAME},  # never read: the caller hung up
    ])

    async def collect():
        return [item async for item in _WebSocketLink(socket).incoming()]

    assert asyncio.run(collect()) == [FRAME, SegmentPlayed(2, 5), PlaybackStopped(2, (5, 6))]


def test_sending_after_the_caller_left_is_quiet() -> None:
    socket = ScriptedSocket([])
    socket.gone = True
    link = _WebSocketLink(socket)

    asyncio.run(link.send(Caption("hello")))
    asyncio.run(link.send(Caption("again")))

    assert socket.sent == []


class SteppingClock:
    """Advances by `step` seconds each time a frame is received."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def frames_accepted(seconds_per_frame: float, frames: int) -> int:
    """How many 200 ms frames get through when one arrives every
    `seconds_per_frame` of wall time."""
    big = b"\x00" * live.MAX_AUDIO_FRAME_BYTES
    clock = SteppingClock(seconds_per_frame)

    class Socket(ScriptedSocket):
        async def receive(self) -> dict:
            clock.now += clock.step
            return await super().receive()

    socket = Socket([{"type": "websocket.receive", "bytes": big} for _ in range(frames)])

    async def count():
        return len([item async for item in _WebSocketLink(socket, clock).incoming()])

    return asyncio.run(count())


def test_real_time_audio_is_never_cut_off() -> None:
    assert frames_accepted(0.2, 500) == 500  # 100 s of audio over 100 s


def test_audio_at_twice_real_time_is_cut_off_once_past_the_allowance() -> None:
    # 200 ms of audio every 100 ms: after n frames, 0.2n s of audio against an
    # allowance of 0.1n x 1.25 + 2 s, so frame 27 is the first one refused.
    assert frames_accepted(0.1, 100) == 26
