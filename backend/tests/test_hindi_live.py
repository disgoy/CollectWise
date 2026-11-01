"""Hindi on live browser calls (M7.6 B2): the conversation's language picks
the listener and the voice; Hindi speech switched off closes the call as not
configured even when English speech is on; account names reach the Hindi
listener only when switched on; the real Hindi providers are the
multilingual listener with hints and Rahul's voice."""

import json

import pytest

from backend.app import config
from backend.app.api import providers
from backend.app.api.providers import get_hindi_live_stt, get_hindi_live_tts
from backend.app.main import app
from backend.app.voice.deepgram_live import FLUX_VOICE, DeepgramLiveSTT, DeepgramLiveTTS
from backend.tests.fake_live_speech import FakeLiveSTT, FakeLiveTTS, ScriptedListener
from backend.tests.test_live_api import FRAME, close_code, live_api, receive, start, until  # noqa: F401  (fixture)


@pytest.fixture
def hindi(live_api):  # noqa: F811
    class Hindi:
        stt = FakeLiveSTT(ScriptedListener({1: "मेरा balance कितना है?"}))
        tts = FakeLiveTTS()

    app.dependency_overrides[get_hindi_live_stt] = lambda: Hindi.stt
    app.dependency_overrides[get_hindi_live_tts] = lambda: Hindi.tts
    Hindi.session_id = live_api.store.create("cust_a", "hi").session_id
    return Hindi


def call(live_api, session_id) -> None:  # noqa: F811
    with live_api.connect(session_id) as ws:
        start(ws)
        assert receive(ws)["type"] == "ready"
        ws.send_bytes(FRAME)
        until(ws, lambda m: isinstance(m, dict) and m.get("type") == "reply_done")
        ws.send_text(json.dumps({"type": "hang_up"}))
        assert close_code(ws) == 1000


def test_a_hindi_call_hears_and_speaks_with_the_hindi_pair(live_api, hindi) -> None:  # noqa: F811
    call(live_api, hindi.session_id)

    assert hindi.stt.opened_with is not None and hindi.stt.listener.audio == [FRAME]
    assert hindi.tts.speaker.said == ["Your main account has 500 US dollars."]  # the scripted model reply
    assert live_api.stt.opened_with is None and live_api.tts.opens == 0  # English untouched


def test_an_english_call_still_uses_the_english_pair(live_api, hindi) -> None:  # noqa: F811
    call(live_api, live_api.session_id)

    assert live_api.stt.opened_with is not None
    assert hindi.stt.opened_with is None


@pytest.mark.parametrize("missing", [get_hindi_live_stt, get_hindi_live_tts], ids=["listening", "voice"])
def test_a_hindi_call_without_hindi_speech_is_not_configured(live_api, hindi, missing) -> None:  # noqa: F811
    app.dependency_overrides[missing] = lambda: None

    with live_api.connect(hindi.session_id) as ws:
        start(ws)
        assert close_code(ws) == 4503
    assert live_api.stt.opened_with is None  # never falls back to English


@pytest.mark.parametrize(("switch", "keyterms"), [(False, []), (True, ["main"])])
def test_account_names_reach_the_hindi_listener_only_when_switched_on(live_api, hindi, monkeypatch, switch,  # noqa: F811
                                                                       keyterms) -> None:
    monkeypatch.setattr(config, "HINDI_KEYTERMS", switch)

    call(live_api, hindi.session_id)

    assert hindi.stt.opened_with["keyterms"] == keyterms


def test_english_calls_always_get_account_names(live_api, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(config, "HINDI_KEYTERMS", False)

    call(live_api, live_api.session_id)

    assert live_api.stt.opened_with["keyterms"] == ["main"]


def test_the_hindi_settings_shipped() -> None:
    assert (config.HINDI_STT_MODEL, config.HINDI_LANGUAGE_HINTS, config.HINDI_TTS_VOICE, config.HINDI_KEYTERMS) == (
        "flux-general-multi", ("hi", "en"), "flux-rahul-hi", True)


def test_the_real_hindi_pair_is_the_multilingual_listener_and_rahul(monkeypatch) -> None:
    monkeypatch.setattr(config, "SPEECH_PROVIDER", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key-not-real")
    providers._deepgram_live_speech.cache_clear()
    try:
        hindi_stt, hindi_tts = providers._live_speech("hi")
        english_stt, english_tts = providers._live_speech("en")
    finally:
        providers._deepgram_live_speech.cache_clear()

    assert isinstance(hindi_stt, DeepgramLiveSTT) and isinstance(hindi_tts, DeepgramLiveTTS)
    assert (hindi_stt._model, hindi_stt._language_hints) == ("flux-general-multi", ("hi", "en"))
    assert (hindi_tts._voice, hindi_tts._protocol) == ("flux-rahul-hi", FLUX_VOICE)
    assert (english_stt._model, english_stt._language_hints) == (config.LIVE_STT_MODEL, ())
    assert english_tts._voice == config.TTS_VOICE and english_tts._protocol is not FLUX_VOICE


def test_load_tests_simulated_speech_serves_hindi_too(monkeypatch) -> None:
    monkeypatch.setattr(config, "SPEECH_PROVIDER", "simulated")
    monkeypatch.setattr(config, "LOAD_TEST", True)
    providers._simulated_live_speech.cache_clear()
    try:
        assert providers._live_speech("hi") == providers._live_speech("en")
    finally:
        providers._simulated_live_speech.cache_clear()


# --- the rupee demo customer ------------------------------------------------------------------


def test_the_seed_creates_the_rupee_demo_customer_once(monkeypatch, capsys, db_session) -> None:
    from scripts import seed_demo
    from backend.app.db.models import CustomerRecord
    from backend.app.services.accounts import list_customer_account_details
    from backend.tests.test_live_report import _Borrowed

    monkeypatch.setattr(seed_demo, "SessionLocal", lambda: _Borrowed(db_session))
    monkeypatch.setattr(seed_demo, "require_current_schema", lambda: None)

    assert seed_demo.main([]) == 0
    first = capsys.readouterr().out
    assert seed_demo.main([]) == 0
    second = capsys.readouterr().out

    assert db_session.get(CustomerRecord, "cust_demo_in").display_name == "Demo Customer (India, rupees)"
    assert db_session.get(CustomerRecord, "cust_demo").display_name == "Demo Customer (US, dollars)"
    accounts = {details.label: (str(details.balance), details.currency)
                for details in list_customer_account_details(db_session, "cust_demo_in")}
    assert accounts == {"savings": ("25000.00", "INR"), "current": ("1200.00", "INR")}
    assert db_session.get(CustomerRecord, "cust_demo") is not None  # the English one too
    assert "created customer cust_demo_in" in first and "created" not in second


def test_both_demo_customers_are_offered_by_default(client, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEMO_LOGIN", True)
    for customer_id, name in (("cust_demo", "Demo Customer"), ("cust_demo_in", "Demo Customer (India)")):
        assert client.post("/customers", json={"customer_id": customer_id, "display_name": name}).status_code == 201

    offered = client.get("/demo/customers").json()

    assert [c["customer_id"] for c in offered] == ["cust_demo", "cust_demo_in"]


@pytest.mark.parametrize(("value", "on"), [(None, True), ("1", True), ("", True), ("0", False)])
def test_account_names_for_hindi_are_on_unless_switched_off(monkeypatch, value, on) -> None:
    import importlib

    if value is None:
        monkeypatch.delenv("COLLECTWISE_HINDI_KEYTERMS", raising=False)
    else:
        monkeypatch.setenv("COLLECTWISE_HINDI_KEYTERMS", value)
    try:
        assert importlib.reload(config).HINDI_KEYTERMS is on
    finally:
        monkeypatch.delenv("COLLECTWISE_HINDI_KEYTERMS", raising=False)
        importlib.reload(config)


def test_the_seed_renames_demo_customers_seeded_under_older_names(monkeypatch, capsys, db_session) -> None:
    from scripts import seed_demo
    from backend.app.db.models import CustomerRecord
    from backend.tests.test_live_report import _Borrowed

    db_session.add_all([CustomerRecord(customer_id="cust_demo", display_name="Demo Customer"),
                        CustomerRecord(customer_id="cust_demo_in", display_name="Demo Customer (India)")])
    db_session.commit()
    monkeypatch.setattr(seed_demo, "SessionLocal", lambda: _Borrowed(db_session))
    monkeypatch.setattr(seed_demo, "require_current_schema", lambda: None)

    assert seed_demo.main([]) == 0

    assert db_session.get(CustomerRecord, "cust_demo").display_name == "Demo Customer (US, dollars)"
    assert db_session.get(CustomerRecord, "cust_demo_in").display_name == "Demo Customer (India, rupees)"
    out = capsys.readouterr().out
    assert "renamed customer cust_demo to 'Demo Customer (US, dollars)'" in out
