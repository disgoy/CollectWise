"""Who is on the phone (M7.2): caller ID narrows to one customer, the keypad
PIN decides, every failure sounds the same, repeated guessing locks the PIN,
digits are never kept, and only the account holder's page sees captions."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.db import models
from backend.app.services import phone_identity
from backend.app.services.phone_identity import (
    InvalidPhoneNumberError,
    PhoneNumberInUseError,
    WeakPinError,
    check_phone_pin,
    check_pin_strength,
    normalize_phone,
    set_phone_and_pin,
)
from backend.app.telephony import verification
from backend.app.telephony.verification import verify_caller
from backend.app.telephony.watchers import CallWatchers
from backend.tests.helpers import seed_customer
from backend.tests.test_telephony import (  # noqa: F401  (phone is a fixture)
    CALLER,
    MEDIA,
    PIN,
    hear_prompts,
    open_stream,
    phone,
    ring,
    start_frame,
    stream_pass,
    still_open_until,
    type_pin,
    until_agent_mark,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
MARKER_PIN = "583920"  # appears nowhere it shouldn't


@pytest.fixture
def customer(db_session):
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    set_phone_and_pin(db_session, "cust_a", phone_number=CALLER, pin=MARKER_PIN)
    return db_session


# --- numbers and PINs ---------------------------------------------------------------------


def test_numbers_are_normalized_to_e164() -> None:
    assert normalize_phone("+1 (555) 555-0100") == "+15555550100"
    for bad in ("5555550100", "+0123456789", "anonymous", "", "+12"):
        with pytest.raises(InvalidPhoneNumberError):
            normalize_phone(bad)


@pytest.mark.parametrize("pin", ["000000", "111111", "123456", "654321", "12345", "1234567", "12a456", "４８２９１６"])
def test_easy_or_malformed_pins_are_refused(pin) -> None:
    with pytest.raises(WeakPinError):
        check_pin_strength(pin)


def test_only_a_salted_hash_is_stored(customer) -> None:
    credential = customer.get(models.PhoneCredentialRecord, "cust_a")

    assert MARKER_PIN not in json.dumps({k: str(v) for k, v in vars(credential).items()})
    assert len(credential.pin_hash) == 64 and len(credential.pin_salt) == 32
    first_salt, first_hash = credential.pin_salt, credential.pin_hash
    set_phone_and_pin(customer, "cust_a", phone_number=CALLER, pin=MARKER_PIN)  # the same PIN again
    again = customer.get(models.PhoneCredentialRecord, "cust_a")
    assert (again.pin_salt, again.pin_hash) != (first_salt, first_hash)  # a new salt each time


def test_a_number_belongs_to_one_customer(customer) -> None:
    seed_customer(customer, "cust_b", [("acct_b_1", "USD", "active")])

    with pytest.raises(PhoneNumberInUseError):
        set_phone_and_pin(customer, "cust_b", phone_number=CALLER, pin="482916")


# --- the check -------------------------------------------------------------------------------


def test_the_right_number_and_pin_verify(customer) -> None:
    result = check_phone_pin(customer, CALLER, MARKER_PIN, now=NOW)

    assert (result.customer_id, result.reason) == ("cust_a", "verified")


@pytest.mark.parametrize(("number", "reason"), [("+15555550199", "unknown_number"), ("", "unknown_number")])
def test_an_unknown_or_withheld_number_never_verifies(customer, number, reason) -> None:
    result = check_phone_pin(customer, number, MARKER_PIN, now=NOW)

    assert (result.customer_id, result.reason) == (None, reason)


def test_three_wrong_pins_lock_the_pin_across_calls_until_it_expires(customer) -> None:
    for attempt in range(phone_identity.MAX_FAILED_ATTEMPTS):
        assert check_phone_pin(customer, CALLER, "482916", now=NOW).reason == "wrong_pin"

    assert check_phone_pin(customer, CALLER, MARKER_PIN, now=NOW).reason == "locked"  # even the right one
    later = NOW + phone_identity.LOCKOUT
    assert check_phone_pin(customer, CALLER, MARKER_PIN, now=later).reason == "verified"
    credential = customer.get(models.PhoneCredentialRecord, "cust_a")
    assert (credential.failed_attempts, credential.locked_until) == (0, None)
    assert (phone_identity.MAX_FAILED_ATTEMPTS, phone_identity.LOCKOUT) == (3, timedelta(minutes=15))


def test_a_right_pin_resets_the_count(customer) -> None:
    for _ in range(2):
        check_phone_pin(customer, CALLER, "482916", now=NOW)
    check_phone_pin(customer, CALLER, MARKER_PIN, now=NOW)  # resets the count to 0
    for _ in range(2):
        check_phone_pin(customer, CALLER, "482916", now=NOW)

    assert check_phone_pin(customer, CALLER, MARKER_PIN, now=NOW).reason == "verified"  # 2 in a row, not 4: not locked


def test_a_customer_without_a_pin_never_verifies(db_session) -> None:
    seed_customer(db_session, "cust_c", [("acct_c_1", "USD", "active")])
    db_session.get(models.CustomerRecord, "cust_c").phone_number = "+15555550177"
    db_session.commit()

    assert check_phone_pin(db_session, "+15555550177", "482916", now=NOW).reason == "no_pin"


# --- the back office ---------------------------------------------------------------------------


def test_the_back_office_sets_a_pin_and_never_shows_it(client, caplog) -> None:
    client.post("/customers", json={"customer_id": "cust_a", "display_name": "Synthetic"})

    with caplog.at_level(logging.DEBUG):
        response = client.put("/customers/cust_a/phone", json={"phone_number": CALLER, "pin": MARKER_PIN})
        shown = client.get("/customers/cust_a").json()
        weak = client.put("/customers/cust_a/phone", json={"phone_number": CALLER, "pin": "123456"})

    assert response.status_code == 204 and response.content == b""
    assert "pin" not in shown and "phone_number" not in shown
    assert weak.status_code == 422 and "123456" not in weak.text
    assert MARKER_PIN not in caplog.text
    assert client.put("/customers/cust_missing/phone", json={"phone_number": "+15555550111", "pin": "482916"}).status_code == 404
    assert client.put("/customers/cust_a/phone", json={"phone_number": "5550100", "pin": "482916"}).status_code == 422


# --- the verification dialogue ---------------------------------------------------------------------


def dialogue(keys: list[str | None], results: list[str], *, hang_up_after: int | None = None):
    """Run verify_caller with scripted keys and check results; return what it said and recorded."""
    said, failures, checks, presses = [], [], [], iter(keys)

    async def keypad(timeout):
        return next(presses, None)

    async def speak(text):
        said.append(text)

    async def check(pin):
        checks.append(pin)
        reason = results.pop(0)
        return phone_identity.PinCheck("cust_a" if reason == "verified" else None, reason)

    async def failed(reason):
        failures.append(reason)

    customer = asyncio.run(verify_caller(keypad=keypad, speak=speak, check=check, failed=failed,
                                         hung_up=lambda: hang_up_after is not None and len(checks) >= hang_up_after))
    return customer, said, failures, checks


def test_the_right_pin_first_time() -> None:
    customer, said, failures, checks = dialogue(list(PIN), ["verified"])

    assert customer == "cust_a"
    assert said == [f"{verification.GREETING} {verification.ASK_PIN}", verification.VERIFIED]
    assert (failures, checks) == ([], [PIN])


@pytest.mark.parametrize("reason", ["wrong_pin", "unknown_number", "locked", "no_pin"])
def test_every_failure_sounds_the_same(reason) -> None:
    customer, said, failures, _ = dialogue(list(PIN) * 3, [reason] * 3)

    assert customer is None
    assert said == [f"{verification.GREETING} {verification.ASK_PIN}", verification.TRY_AGAIN, verification.TRY_AGAIN,
                    verification.NOT_VERIFIED]
    assert failures == [reason] * 3


def test_a_wrong_pin_then_the_right_one() -> None:
    customer, said, failures, _ = dialogue(list("482917") + list(PIN), ["wrong_pin", "verified"])

    assert customer == "cust_a"
    assert said[1:] == [verification.TRY_AGAIN, verification.VERIFIED]


def test_not_typing_is_no_entry_and_never_checked() -> None:
    customer, said, failures, checks = dialogue([], [])

    assert (customer, checks) == (None, [])
    assert failures == ["no_entry"] * verification.ATTEMPTS


def test_star_starts_again_and_other_keys_are_ignored() -> None:
    _, _, _, checks = dialogue(["1", "2", "*", "4", "#", "8", "2", "9", "1", "6"], ["verified"])

    assert checks == ["482916"]


def test_hanging_up_stops_at_once() -> None:
    customer, said, failures, checks = dialogue(list(PIN), ["wrong_pin"], hang_up_after=0)

    assert (customer, checks, failures) == (None, [], [])
    assert said == [f"{verification.GREETING} {verification.ASK_PIN}"]


def test_the_shipped_attempts_and_timeouts() -> None:
    assert (verification.ATTEMPTS, verification.FIRST_KEY_S, verification.NEXT_KEY_S) == (3, 15.0, 5.0)


# --- over the phone ----------------------------------------------------------------------------------


def events(client, event_type):
    from backend.app.api.providers import get_db_factory
    from backend.app.main import app

    with app.dependency_overrides[get_db_factory]()() as db:
        return db.scalars(select(models.ConversationEventRecord).where(
            models.ConversationEventRecord.event_type == event_type)).all()


def test_nothing_said_before_verification_reaches_speech_to_text(phone, client) -> None:
    pass_ = stream_pass(ring(client).text)

    with open_stream(client) as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(start_frame("CA0001", pass_))
        for _ in range(5):  # talking over the PIN prompt
            ws.send_text(json.dumps({"event": "media", "media": {"payload": MEDIA}}))
        type_pin(ws, PIN)
        hear_prompts(ws, f"{verification.GREETING} {verification.ASK_PIN}", verification.VERIFIED)
        ws.send_text(json.dumps({"event": "stop"}))
        ws.receive()

    assert phone.stt.listener.frames == 0
    [verified] = events(client, "caller_verified")
    assert (verified.customer_id, verified.outcome) == ("cust_a", "verified")


def test_three_wrong_pins_on_the_phone_end_the_call_and_keep_no_digits(phone, client, caplog) -> None:
    pass_ = stream_pass(ring(client).text)

    with caplog.at_level(logging.DEBUG), open_stream(client) as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(start_frame("CA0001", pass_))
        for _ in range(3):
            type_pin(ws, MARKER_PIN)
        hear_prompts(ws, f"{verification.GREETING} {verification.ASK_PIN}", verification.TRY_AGAIN,
                     verification.TRY_AGAIN)
        goodbye = []
        while not any(f["event"] == "mark" for f in goodbye):  # the goodbye's audio and its mark
            goodbye.append(json.loads(ws.receive_text()))
        mark = next(f for f in goodbye if f["event"] == "mark")
        closed = still_open_until(ws, lambda: ws.send_text(json.dumps({"event": "mark", "mark": mark["mark"]})))

    assert closed["code"] == 1000
    assert len(phone.store) == 0  # no conversation, no agent
    failures = events(client, "verification_failed")
    assert [f.outcome for f in failures] == ["wrong_pin"] * 3
    assert all(f.customer_id == "unverified" for f in failures)
    assert MARKER_PIN not in caplog.text
    assert MARKER_PIN not in json.dumps([[f.session_id, f.outcome, f.name] for f in failures])
    assert phone.llm.requests == []


def test_after_verification_the_agent_never_sees_the_digits(phone, client) -> None:
    pass_ = stream_pass(ring(client).text)

    with open_stream(client) as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(start_frame("CA0001", pass_))
        type_pin(ws, PIN)
        hear_prompts(ws, f"{verification.GREETING} {verification.ASK_PIN}", verification.VERIFIED)
        for _ in range(3):
            ws.send_text(json.dumps({"event": "media", "media": {"payload": MEDIA}}))
        until_agent_mark(ws)
        ws.send_text(json.dumps({"event": "stop"}))
        ws.receive()

    assert len(phone.llm.requests) == 1
    assert PIN not in json.dumps(phone.llm.requests[0]["messages"], default=str)


# --- phone captions on screen ---------------------------------------------------------------------------


def watch(client, customer_id):
    from backend.tests.test_live_api import token_for

    ws = client.websocket_connect("/telephony/watch")
    socket = ws.__enter__()
    socket.send_text(json.dumps({"type": "start", "token": token_for(customer_id)}))
    assert socket.receive_json() == {"type": "watching"}
    return ws, socket


def test_the_account_holders_page_sees_the_phone_call(phone, client) -> None:
    client.post("/customers", json={"customer_id": "cust_b", "display_name": "Synthetic"})
    mine, mine_socket = watch(client, "cust_a")
    theirs, theirs_socket = watch(client, "cust_b")
    pass_ = stream_pass(ring(client).text)

    with open_stream(client) as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(start_frame("CA0001", pass_))
        type_pin(ws, PIN)
        hear_prompts(ws, f"{verification.GREETING} {verification.ASK_PIN}", verification.VERIFIED)
        for _ in range(3):
            ws.send_text(json.dumps({"event": "media", "media": {"payload": MEDIA}}))
        until_agent_mark(ws)
        ws.send_text(json.dumps({"event": "stop"}))
        ws.receive()

    seen = []
    while not seen or seen[-1]["type"] != "call_ended":
        seen.append(mine_socket.receive_json())
    mine.__exit__(None, None, None)
    theirs.__exit__(None, None, None)

    types = [m["type"] for m in seen]
    assert types[0] == "phone_call_started"
    assert "caller_said" in types and "agent_sentence" in types
    assert next(m for m in seen if m["type"] == "agent_sentence")["text"] == "Your main account has 500 US dollars."
    assert all(m["type"] != "agent_audio" for m in seen)  # the caller hears it; the page doesn't play it
    assert PIN not in json.dumps(seen)


def test_a_wrong_token_cant_watch(phone, client) -> None:
    with client.websocket_connect("/telephony/watch") as ws:
        ws.send_text(json.dumps({"type": "start", "token": "not-a-token"}))
        closed = ws.receive()

    assert closed["type"] == "websocket.close" and closed["code"] == 4401


def test_the_relay_keeps_nothing_without_watchers_and_drops_for_a_slow_one() -> None:
    from backend.app.telephony import watchers as relay

    hub = CallWatchers()
    hub.publish("cust_a", "nobody watching")  # simply dropped

    queue = hub.subscribe("cust_a")
    for n in range(relay.MAX_WAITING + 5):
        hub.publish("cust_a", str(n))
    hub.unsubscribe("cust_a", queue)

    assert queue.qsize() == relay.MAX_WAITING
    assert hub.watching("cust_a") == 0


# --- the demo seed ----------------------------------------------------------------------------


def test_the_demo_seed_sets_a_phone_and_pin_without_echoing_it(monkeypatch, capsys, db_session) -> None:
    from scripts import seed_demo

    class Borrowed:
        def __enter__(self):
            return db_session

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(seed_demo, "SessionLocal", Borrowed)
    monkeypatch.setattr(seed_demo, "require_current_schema", lambda: None)
    answers = iter([MARKER_PIN, MARKER_PIN])

    assert seed_demo.main(["--phone", CALLER], ask=lambda prompt: next(answers)) == 0
    printed = capsys.readouterr().out

    assert MARKER_PIN not in printed
    assert check_phone_pin(db_session, CALLER, MARKER_PIN, now=NOW).customer_id == "cust_demo"
    mismatched = iter(["482916", "482917"])
    assert seed_demo.main(["--phone", CALLER], ask=lambda prompt: next(mismatched)) == 1
    assert check_phone_pin(db_session, CALLER, MARKER_PIN, now=NOW).customer_id == "cust_demo"  # unchanged


def test_keys_pressed_after_verification_go_nowhere() -> None:
    from backend.app.telephony.twilio_link import TwilioLink
    from backend.tests.test_telephony import FakeTwilioSocket

    async def go():
        socket = FakeTwilioSocket()
        link = TwilioLink(socket, "MZ1", verifying=True)
        socket.twilio("dtmf", dtmf={"digit": "4"})
        first = await link.keypad(1.0)
        link.start_talking()
        socket.twilio("dtmf", dtmf={"digit": "8"})
        socket.twilio("media", media={"payload": MEDIA})
        async for item in link.incoming():
            break  # the audio after verification: the digit before it has been read
        return first, link

    first, link = asyncio.run(go())

    assert first == "4"
    assert [d for d in link._digits._queue if d is not None] == []  # only the end-of-call marker, never "8"
    assert link.keypad_presses == 2
