"""Conversation lifetime and caps in the in-memory session store (D72)."""

import pytest

from backend.app import config
from backend.app.agent.sessions import SessionStore, SessionStoreFullError

IDLE = 15 * 60


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


def store_with(clock: Clock, **limits) -> SessionStore:
    return SessionStore(clock=clock, **{"idle_timeout_s": IDLE, "max_per_customer": 3, "max_total": 100} | limits)


def is_live(store: SessionStore, session) -> bool:
    return store.get_for_customer(session.session_id, session.caller.customer_id) is session


def test_defaults_come_from_config(monkeypatch, clock) -> None:
    monkeypatch.setattr(config, "SESSION_IDLE_MINUTES", 1)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_CUSTOMER", 1)
    store = SessionStore(clock=clock)
    first = store.create("cust_a")
    second = store.create("cust_a")  # replaces the first: the limit is 1

    assert not is_live(store, first)
    clock.advance(60)
    assert not is_live(store, second)


def test_shipped_defaults() -> None:
    # Tests run with the shipped defaults (conftest sets none of these).
    assert (config.SESSION_IDLE_MINUTES, config.MAX_SESSIONS_PER_CUSTOMER, config.MAX_SESSIONS) == (15, 3, 1000)


@pytest.mark.parametrize(
    "limits",
    [{"idle_timeout_s": 0}, {"max_per_customer": 0}, {"max_total": 0}],
)
def test_limits_must_be_positive(clock, limits) -> None:
    with pytest.raises(ValueError):
        store_with(clock, **limits)


# --- idle expiry ------------------------------------------------------------


def test_a_session_expires_after_the_idle_timeout(clock) -> None:
    store = store_with(clock)
    session = store.create("cust_a")

    clock.advance(IDLE - 1)
    assert is_live(store, session)  # this use restarts the idle time

    clock.advance(IDLE - 1)
    assert is_live(store, session)

    clock.advance(IDLE)  # exactly the timeout
    assert not is_live(store, session)
    assert len(store) == 0


def test_an_expired_session_stays_gone(clock) -> None:
    store = store_with(clock)
    session = store.create("cust_a")
    clock.advance(IDLE)
    assert not is_live(store, session)

    clock.now -= IDLE  # even if time looked earlier again
    assert not is_live(store, session)


def test_starting_a_session_sweeps_expired_ones(clock) -> None:
    store = store_with(clock)
    old = [store.create(customer) for customer in ("cust_a", "cust_b", "cust_c")]
    clock.advance(IDLE - 1)
    recent = store.create("cust_d")
    clock.advance(1)  # the first three are now exactly IDLE old

    store.create("cust_e")

    assert len(store) == 2
    assert is_live(store, recent)
    assert not any(is_live(store, session) for session in old)


def test_someone_elses_lookup_neither_refreshes_nor_ends_a_session(clock) -> None:
    store = store_with(clock)
    session = store.create("cust_a")

    clock.advance(IDLE - 1)
    assert store.get_for_customer(session.session_id, "cust_b") is None
    clock.advance(1)
    # cust_b's lookup didn't count as use, so it expired on schedule.
    assert not is_live(store, session)

    other = store.create("cust_a")
    clock.advance(IDLE)
    assert store.get_for_customer(other.session_id, "cust_b") is None
    assert len(store) == 1  # left for its owner's request (or the next sweep)


# --- per-customer cap --------------------------------------------------------


def test_a_new_session_replaces_the_customers_least_recently_used(clock) -> None:
    store = store_with(clock)
    first = store.create("cust_a")
    clock.advance(1)
    second = store.create("cust_a")
    clock.advance(1)
    third = store.create("cust_a")
    clock.advance(1)
    assert is_live(store, first)  # now the most recently used

    clock.advance(1)
    fourth = store.create("cust_a")

    assert not is_live(store, second)
    assert all(is_live(store, s) for s in (first, third, fourth))


def test_the_cap_is_per_customer(clock) -> None:
    store = store_with(clock)
    others = [store.create("cust_b") for _ in range(3)]

    for _ in range(5):
        store.create("cust_a")

    assert all(is_live(store, session) for session in others)
    assert len(store) == 6


def test_a_lowered_cap_trims_down_to_it(clock) -> None:
    store = store_with(clock)
    sessions = []
    for _ in range(3):
        sessions.append(store.create("cust_a"))
        clock.advance(1)
    store._max_per_customer = 1

    newest = store.create("cust_a")

    assert [is_live(store, s) for s in sessions] == [False, False, False]
    assert is_live(store, newest)


# --- total cap ---------------------------------------------------------------


def test_the_server_refuses_a_session_when_full(clock) -> None:
    store = store_with(clock, max_total=3)
    sessions = [store.create(customer) for customer in ("cust_a", "cust_b", "cust_c")]

    with pytest.raises(SessionStoreFullError):
        store.create("cust_d")

    assert len(store) == 3
    assert all(is_live(store, session) for session in sessions)


def test_a_customer_at_their_cap_can_replace_even_when_full(clock) -> None:
    store = store_with(clock, max_total=3, max_per_customer=1)
    store.create("cust_a")
    store.create("cust_b")
    store.create("cust_c")

    replacement = store.create("cust_a")

    assert is_live(store, replacement)


def test_expired_sessions_dont_count_towards_the_total(clock) -> None:
    store = store_with(clock, max_total=2)
    store.create("cust_a")
    store.create("cust_b")
    clock.advance(IDLE)

    store.create("cust_c")

    assert len(store) == 1
