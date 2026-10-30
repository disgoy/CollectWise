import asyncio
import base64

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.app import config
from backend.app.agent.sessions import SessionStore
from backend.app.api.agent import _read_body_capped, get_llm, get_session_store, get_stt, get_tts
from backend.app.auth.session_tokens import issue_session_token
from backend.app.main import app
from backend.app.voice.audio import MAX_WAV_BYTES, AudioClip, read_wav, write_wav
from backend.app.voice.speech import SpeechUnavailableError, Transcript
from backend.app.voice.turn import REPLY_NOT_UNDERSTOOD
from backend.tests.fake_llm import FakeLLM, say, use
from backend.tests.fake_speech import FakeSTT, FakeTTS

WAV = write_wav(AudioClip(data=bytes(2 * 1_600), sample_rate=16_000))  # 100 ms


def auth(customer_id: str = "cust_a") -> dict[str, str]:
    token = issue_session_token(
        customer_id, secret=config.SESSION_TOKEN_SECRET.encode("utf-8"), ttl_seconds=600,
    )
    return {"Authorization": f"Bearer {token}"}


class Voice:
    """The fakes behind one test's app."""

    def __init__(self) -> None:
        self.llm = FakeLLM([])
        self.stt = FakeSTT([])
        self.tts = FakeTTS()


@pytest.fixture
def fakes() -> Voice:
    return Voice()


@pytest.fixture
def voice_client(client: TestClient, fakes: Voice) -> TestClient:
    # `client` provides an isolated database and clears overrides afterwards.
    store = SessionStore()
    app.dependency_overrides[get_session_store] = lambda: store
    app.dependency_overrides[get_llm] = lambda: fakes.llm
    app.dependency_overrides[get_stt] = lambda: fakes.stt
    app.dependency_overrides[get_tts] = lambda: fakes.tts
    for customer_id in ("cust_a", "cust_b"):
        assert client.post(
            "/customers", json={"customer_id": customer_id, "display_name": "Synthetic Customer"},
        ).status_code == 201
    assert client.post(
        "/accounts",
        json={"account_id": "acct_a_1", "customer_id": "cust_a", "balance": "500.00", "currency": "USD"},
    ).status_code == 201
    return client


def new_session(client: TestClient, customer_id: str = "cust_a") -> str:
    response = client.post("/agent/sessions", headers=auth(customer_id))
    assert response.status_code == 201
    return response.json()["session_id"]


def speak(client, session_id, body=WAV, *, content_type="audio/wav", customer_id="cust_a", headers=None):
    return client.post(
        f"/agent/sessions/{session_id}/voice-turns",
        content=body,
        headers={**auth(customer_id), "Content-Type": content_type, **(headers or {})},
    )


# --- the normal path -----------------------------------------------------------


def test_spoken_turn_returns_transcript_reply_and_audio(voice_client, fakes) -> None:
    session_id = new_session(voice_client)
    fakes.stt.script.append(Transcript("What accounts do I have?", 0.95))
    fakes.llm.script += [use(("t1", "list_accounts", {})), say("You have one account.")]

    response = speak(voice_client, session_id)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"transcript", "reply", "handed_off", "audio", "audio_format"}
    assert body["transcript"] == "What accounts do I have?"
    assert body["reply"] == "You have one account."
    assert body["handed_off"] is False
    assert body["audio_format"] == "wav"
    reply_audio = read_wav(base64.b64decode(body["audio"]))
    assert reply_audio.duration_ms == len("You have one account.") * 60
    # The uploaded clip is exactly what the listener received.
    assert fakes.stt.clips == [read_wav(WAV)]


@pytest.mark.parametrize("content_type", ["audio/x-wav", "audio/wave", "Audio/WAV; codecs=1"])
def test_common_wav_content_types_are_accepted(voice_client, fakes, content_type) -> None:
    session_id = new_session(voice_client)
    fakes.stt.script.append(Transcript("hello", 0.9))
    fakes.llm.script.append(say("Hi!"))

    assert speak(voice_client, session_id, content_type=content_type).status_code == 200


def test_text_and_voice_share_one_conversation(voice_client, fakes) -> None:
    session_id = new_session(voice_client)
    fakes.llm.script += [
        use(("t1", "propose_payment", {"account_id": "acct_a_1", "amount": "120"})),
        say("120.00 USD from your account. Shall I go ahead?"),
        use(("t2", "confirm_payment", {})),
        say("Your payment request is recorded."),
    ]
    typed = voice_client.post(
        f"/agent/sessions/{session_id}/turns", json={"text": "Pay 120"}, headers=auth(),
    )
    assert typed.status_code == 200

    fakes.stt.script.append(Transcript("Yes, go ahead", 0.97))
    spoken = speak(voice_client, session_id)

    assert spoken.json()["reply"] == "Your payment request is recorded."


def test_speech_failure_returns_text_without_audio(voice_client, fakes) -> None:
    session_id = new_session(voice_client)
    fakes.stt.script.append(Transcript("hello", 0.9))
    fakes.llm.script.append(say("Hi!"))
    fakes.tts.failures.append(SpeechUnavailableError("busy"))

    body = speak(voice_client, session_id).json()

    assert (body["reply"], body["audio"], body["audio_format"]) == ("Hi!", None, None)


def test_guarded_turn_is_a_normal_200(voice_client, fakes) -> None:
    session_id = new_session(voice_client)
    fakes.stt.script.append(Transcript("mumble", 0.1))

    body = speak(voice_client, session_id).json()

    assert body["reply"] == REPLY_NOT_UNDERSTOOD
    assert fakes.llm.requests == []


# --- rejected before any speech service is used --------------------------------


def assert_rejected(response, status: int, detail: str, fakes: Voice) -> None:
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert fakes.stt.clips == []
    assert fakes.llm.requests == []


@pytest.mark.parametrize("content_type", ["application/json", "audio/mpeg", "text/plain", ""])
def test_wrong_content_type(voice_client, fakes, content_type) -> None:
    response = speak(voice_client, new_session(voice_client), content_type=content_type)

    assert_rejected(response, 415, "unsupported_media_type", fakes)


def test_oversized_upload_declared_up_front(voice_client, fakes) -> None:
    response = speak(voice_client, new_session(voice_client), body=b"\x00" * (MAX_WAV_BYTES + 1))

    assert_rejected(response, 413, "too_large", fakes)


def test_oversized_upload_without_a_declared_size(voice_client, fakes) -> None:
    # Sent in chunks with no Content-Length. The test client buffers the whole
    # body before the app runs, so this checks the outcome only; the
    # stop-reading-early behaviour is tested on the reader itself below.
    def chunks():
        for _ in range(MAX_WAV_BYTES // 65_536 + 2):
            yield b"\x00" * 65_536

    response = speak(voice_client, new_session(voice_client), body=chunks())

    assert_rejected(response, 413, "too_large", fakes)


# --- the capped reader, driven directly ------------------------------------------


def upload(chunks: list[bytes], content_length: int | None = None):
    """A Request whose body arrives in `chunks`; `pulled` counts reads."""
    state = {"pulled": 0}

    async def receive():
        index = state["pulled"]
        state["pulled"] += 1
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}

    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return Request({"type": "http", "method": "POST", "headers": headers}, receive), state


def read(request, limit: int) -> bytes:
    return asyncio.run(_read_body_capped(request, limit))


def test_reader_returns_a_body_within_the_limit() -> None:
    request, state = upload([b"a" * 1_000, b"b" * 1_000, b"c" * 500])

    assert read(request, 2_500) == b"a" * 1_000 + b"b" * 1_000 + b"c" * 500
    assert state["pulled"] == 3


def test_reader_rejects_a_declared_oversize_without_reading() -> None:
    request, state = upload([b"a" * 1_000], content_length=5_000)

    with pytest.raises(HTTPException) as caught:
        read(request, 2_500)
    assert (caught.value.status_code, caught.value.detail) == (413, "too_large")
    assert state["pulled"] == 0


@pytest.mark.parametrize("content_length", [None, 100], ids=["undeclared", "understated"])
def test_reader_stops_as_soon_as_the_limit_is_passed(content_length) -> None:
    request, state = upload([b"x" * 1_000] * 50, content_length=content_length)

    with pytest.raises(HTTPException) as caught:
        read(request, 2_500)
    assert caught.value.status_code == 413
    assert state["pulled"] == 3  # 3,000 bytes: stopped at the first chunk over 2,500


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (b"not audio at all", 415, "not_wav"),
        (write_wav(AudioClip(data=b"", sample_rate=16_000)), 422, "empty"),
        (WAV[: len(WAV) - 100], 422, "truncated"),
        (write_wav(AudioClip(data=bytes(2 * 8_000 * 31), sample_rate=8_000)), 422, "too_long"),
    ],
    ids=["not-wav", "empty", "truncated", "too-long"],
)
def test_unusable_audio(voice_client, fakes, body, status, detail) -> None:
    response = speak(voice_client, new_session(voice_client), body=body)

    assert_rejected(response, status, detail, fakes)


def test_stereo_is_a_format_error(voice_client, fakes) -> None:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(bytes(4 * 1_600))

    response = speak(voice_client, new_session(voice_client), body=buffer.getvalue())

    assert_rejected(response, 415, "unsupported_channels", fakes)


# --- identity, sessions and configuration ----------------------------------------


def test_bad_token_gets_the_generic_401(voice_client, fakes) -> None:
    session_id = new_session(voice_client)

    response = voice_client.post(
        f"/agent/sessions/{session_id}/voice-turns",
        content=WAV,
        headers={"Authorization": "Bearer nonsense", "Content-Type": "audio/wav"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired session token"}
    assert fakes.stt.clips == []


def test_other_customers_session_looks_missing(voice_client, fakes) -> None:
    session_id = new_session(voice_client, "cust_a")

    response = speak(voice_client, session_id, customer_id="cust_b")

    assert_rejected(response, 404, "Session not found", fakes)


def test_unknown_session(voice_client, fakes) -> None:
    assert_rejected(speak(voice_client, "sess_missing"), 404, "Session not found", fakes)


@pytest.mark.parametrize("dependency", [get_stt, get_tts])
def test_voice_not_configured(voice_client, dependency) -> None:
    session_id = new_session(voice_client)
    del app.dependency_overrides[dependency]

    response = speak(voice_client, session_id)

    assert response.status_code == 503
    assert response.json() == {"detail": "Voice is not configured"}


def test_after_handoff_voice_gets_the_fixed_reply(voice_client, fakes) -> None:
    session_id = new_session(voice_client)
    fakes.stt.script.append(Transcript("A person please", 0.9))
    fakes.llm.script += [use(("t1", "handoff_to_human", {"reason": "caller_requested"})), say("Connecting you.")]
    assert speak(voice_client, session_id).json()["handed_off"] is True

    body = speak(voice_client, session_id).json()  # no transcript scripted: none needed

    assert body["handed_off"] is True
    assert body["transcript"] is None
