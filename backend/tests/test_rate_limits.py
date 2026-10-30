"""Per-customer rate limits and conversation limits on the agent routes (D72)."""

import pytest
from fastapi.testclient import TestClient

from backend.app import config
from backend.app.agent.sessions import SessionStore
from backend.app.api.providers import get_llm, get_session_store, get_stt, get_tts
from backend.app.api.rate_limits import SlidingWindowLimiter, get_session_start_limiter, get_turn_limiter
from backend.app.auth.session_tokens import issue_session_token
from backend.app.main import app
from backend.app.voice.audio import AudioClip, write_wav
from backend.app.voice.speech import Transcript
from backend.tests.fake_llm import FakeLLM, say
from backend.tests.fake_speech import FakeSTT, FakeTTS

WAV = write_wav(AudioClip(data=bytes(2 * 1_600), sample_rate=16_000))


class Clock:
    def __init__(self) -> None:
        self.now = 5_000.0

    def __call__(self) -> float:
        return self.now


# --- the limiter itself -------------------------------------------------------


def test_allows_the_limit_then_says_how_long_to_wait() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(3, 60, clock=clock)

    assert [limiter.hit("a") for _ in range(3)] == [None, None, None]
    clock.now += 20
    assert limiter.hit("a") == pytest.approx(40)


def test_a_hit_leaves_the_window_exactly_window_seconds_later() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 60, clock=clock)
    assert limiter.hit("a") is None

    clock.now += 59.5
    assert limiter.hit("a") == pytest.approx(0.5)
    clock.now += 0.5
    assert limiter.hit("a") is None


def test_refused_hits_are_not_counted() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(2, 60, clock=clock)
    limiter.hit("a")
    clock.now += 10
    limiter.hit("a")
    for _ in range(5):
        clock.now += 1
        assert limiter.hit("a") is not None

    clock.now = 5_060.0  # the first hit has left the window
    assert limiter.hit("a") is None
    # Full again: the next slot frees when the t=10 hit leaves, not later.
    assert limiter.hit("a") == pytest.approx(10)


def test_keys_are_independent() -> None:
    limiter = SlidingWindowLimiter(1, 60, clock=Clock())
    assert limiter.hit("a") is None
    assert limiter.hit("a") is not None
    assert limiter.hit("b") is None


def test_customers_with_no_recent_hits_are_forgotten() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(5, 60, clock=clock)
    for key in ("a", "b", "c"):
        limiter.hit(key)
    clock.now += 30
    limiter.hit("d")
    assert len(limiter) == 4

    clock.now += 30  # a, b and c are a full window old; d isn't
    limiter.hit("e")

    assert len(limiter) == 2


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (1, 0)])
def test_limits_must_be_positive(limit, window) -> None:
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit, window)


def test_shipped_limits() -> None:
    assert (config.TURNS_PER_MINUTE, config.SESSION_STARTS_PER_MINUTE, config.MAX_CALLER_TURNS) == (10, 5, 30)


# --- through the routes -------------------------------------------------------


def auth(customer_id: str = "cust_a") -> dict[str, str]:
    token = issue_session_token(
        customer_id, secret=config.SESSION_TOKEN_SECRET.encode("utf-8"), ttl_seconds=600,
    )
    return {"Authorization": f"Bearer {token}"}


class World:
    def __init__(self) -> None:
        self.clock = Clock()
        self.llm = FakeLLM([say("ok")] * 100)
        self.stt = FakeSTT([Transcript("hello", 0.95)] * 100)
        self.store = SessionStore(clock=self.clock)
        self.turns = SlidingWindowLimiter(config.TURNS_PER_MINUTE, clock=self.clock)
        self.starts = SlidingWindowLimiter(config.SESSION_STARTS_PER_MINUTE, clock=self.clock)


@pytest.fixture
def world(client: TestClient) -> World:
    w = World()
    app.dependency_overrides[get_session_store] = lambda: w.store
    app.dependency_overrides[get_llm] = lambda: w.llm
    app.dependency_overrides[get_stt] = lambda: w.stt
    app.dependency_overrides[get_tts] = lambda: FakeTTS()
    app.dependency_overrides[get_turn_limiter] = lambda: w.turns
    app.dependency_overrides[get_session_start_limiter] = lambda: w.starts
    for customer_id in ("cust_a", "cust_b"):
        assert client.post(
            "/customers", json={"customer_id": customer_id, "display_name": "Synthetic Customer"},
        ).status_code == 201
    w.client = client
    return w


def start(w: World, customer_id: str = "cust_a"):
    return w.client.post("/agent/sessions", headers=auth(customer_id))


def text_turn(w: World, session_id: str, customer_id: str = "cust_a"):
    return w.client.post(f"/agent/sessions/{session_id}/turns", json={"text": "hi"}, headers=auth(customer_id))


def voice_turn(w: World, session_id: str, body: bytes = WAV):
    return w.client.post(
        f"/agent/sessions/{session_id}/voice-turns",
        content=body,
        headers={**auth(), "Content-Type": "audio/wav"},
    )


def test_the_eleventh_turn_in_a_minute_is_refused_before_the_model(world) -> None:
    session_id = start(world).json()["session_id"]
    for _ in range(10):
        assert text_turn(world, session_id).status_code == 200
    calls = len(world.llm.requests)

    response = text_turn(world, session_id)

    assert response.status_code == 429
    assert response.json() == {"detail": "Too many requests"}
    assert response.headers["Retry-After"] == "60"
    assert len(world.llm.requests) == calls

    world.clock.now += 60
    assert text_turn(world, session_id).status_code == 200


def test_retry_after_rounds_up_so_retrying_then_succeeds(world) -> None:
    session_id = start(world).json()["session_id"]
    for _ in range(10):
        text_turn(world, session_id)
    world.clock.now += 0.5  # 59.5 s to wait

    response = text_turn(world, session_id)

    assert response.headers["Retry-After"] == "60"
    world.clock.now += 60
    assert text_turn(world, session_id).status_code == 200


def test_typed_and_spoken_turns_share_the_allowance(world) -> None:
    session_id = start(world).json()["session_id"]
    for _ in range(5):
        assert text_turn(world, session_id).status_code == 200
        assert voice_turn(world, session_id).status_code == 200

    assert text_turn(world, session_id).status_code == 429
    assert voice_turn(world, session_id).status_code == 429


def test_a_refused_spoken_turn_is_neither_read_nor_transcribed(world) -> None:
    session_id = start(world).json()["session_id"]
    for _ in range(10):
        text_turn(world, session_id)

    # Not even a WAV: a refusal must come before the body is looked at.
    response = voice_turn(world, session_id, body=b"not audio")

    assert response.status_code == 429
    assert world.stt.clips == []


def test_requests_for_missing_sessions_count_too(world) -> None:
    for _ in range(10):
        assert text_turn(world, "sess_guess").status_code == 404

    assert text_turn(world, "sess_guess").status_code == 429


def test_one_customers_limit_doesnt_touch_anothers(world) -> None:
    session_a = start(world).json()["session_id"]
    session_b = start(world, "cust_b").json()["session_id"]
    for _ in range(10):
        text_turn(world, session_a)

    assert text_turn(world, session_a).status_code == 429
    assert text_turn(world, session_b, "cust_b").status_code == 200


def test_anonymous_requests_dont_use_up_a_customers_allowance(world) -> None:
    session_id = start(world).json()["session_id"]
    for _ in range(20):
        assert world.client.post(f"/agent/sessions/{session_id}/turns", json={"text": "hi"}).status_code == 401

    assert text_turn(world, session_id).status_code == 200


def test_the_sixth_conversation_start_in_a_minute_is_refused(world) -> None:
    for _ in range(5):
        assert start(world).status_code == 201

    response = start(world)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert len(world.store) == 3  # the per-customer cap kept only three


def test_a_full_server_refuses_new_conversations(world) -> None:
    world.store = SessionStore(clock=world.clock, max_total=1)
    assert start(world).status_code == 201

    response = start(world, "cust_b")

    assert response.status_code == 503
    assert response.json() == {"detail": "Service busy, please try again later"}


def test_an_idle_conversation_is_gone(world) -> None:
    session_id = start(world).json()["session_id"]
    world.clock.now += 15 * 60

    assert text_turn(world, session_id).status_code == 404
