"""A conversation's language (M7.6 B1): chosen when it starts and fixed for
it; the Hindi system prompt is the English one word for word plus how to
speak, and every fixed reply, from the agent loop, the guard, a voice turn
and a live call, is in the conversation's language."""

import asyncio
import re

import pytest
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent import loop
from backend.app.agent.languages import ENGLISH, HINDI, LANGUAGES, REPLY_NAMES, language
from backend.app.agent.llm import LLMUnavailableError
from backend.app.agent.loop import run_turn
from backend.app.agent.prompts import SYSTEM_PROMPT
from backend.app.agent.sessions import SessionStore
from backend.app.api.providers import get_llm, get_session_store
from backend.app.main import app
from backend.app.tools.base import Handoff
from backend.app.voice import guard, live_call
from backend.app.voice.guard import check_transcript
from backend.app.voice.live_call import run_live_call
from backend.tests.fake_live_speech import FakeLink, FakeLiveSTT, FakeLiveTTS
from backend.tests.fake_llm import FakeLLM, say
from backend.tests.helpers import seed_customer
from backend.tests.test_live_call import ManualClock, db_factory  # noqa: F401  (fixture)

DEVANAGARI = re.compile(r"[ऀ-ॿ]")


# --- the languages ------------------------------------------------------------------------


def test_the_hindi_prompt_is_the_english_one_word_for_word_plus_how_to_speak() -> None:
    assert ENGLISH.system_prompt == SYSTEM_PROMPT
    assert HINDI.system_prompt.startswith(SYSTEM_PROMPT)
    section = HINDI.system_prompt[len(SYSTEM_PROMPT):]
    assert "Devanagari" in section
    assert "in digits" in section and "never in words" in section
    assert "applies unchanged" in section


def test_every_fixed_reply_exists_in_both_languages() -> None:
    assert set(LANGUAGES) == {"en", "hi"}
    for name in REPLY_NAMES:
        english, hindi = getattr(ENGLISH.replies, name), getattr(HINDI.replies, name)
        assert english and not DEVANAGARI.search(english), name
        assert DEVANAGARI.search(hindi), name


OLD_NAMES = {
    loop: ["cannot_help", "unavailable", "stuck", "empty", "handed_off", "turn_limit"],
    guard: ["not_understood", "too_long", "cant_hear"],
    live_call: ["goodbye_cant_hear", "time_limit", "silence", "slow_down"],
}


def test_the_english_replies_are_still_importable_by_their_old_names() -> None:
    # Existing tests compare against these names, so each must be its own reply.
    assert sorted(name for names in OLD_NAMES.values() for name in names) == sorted(REPLY_NAMES)
    for module, names in OLD_NAMES.items():
        for name in names:
            assert getattr(module, f"REPLY_{name.upper()}") == getattr(ENGLISH.replies, name), name
    assert len({getattr(ENGLISH.replies, name) for name in REPLY_NAMES}) == len(REPLY_NAMES)


def test_no_fixed_reply_promises_a_transfer() -> None:
    # A handoff is recorded for the team, not a live transfer (D103).
    for name in REPLY_NAMES:
        assert not re.search(r"connect|hold", getattr(ENGLISH.replies, name), re.IGNORECASE), name
        assert not re.search(r"जोड़|होल्ड|रुकिए", getattr(HINDI.replies, name)), name
    for replies in (ENGLISH.replies, HINDI.replies):
        assert "team" in replies.handed_off and "team" in replies.stuck and "team" in replies.turn_limit


def test_an_unknown_language_is_refused() -> None:
    with pytest.raises(ValueError):
        language("fr")


def test_a_conversation_in_an_unknown_language_is_refused_before_anything_changes() -> None:
    store = SessionStore(max_per_customer=1)
    existing = store.create("cust_a")

    with pytest.raises(ValueError):
        store.create("cust_a", "fr")

    assert store.get_for_customer(existing.session_id, "cust_a") is existing  # not replaced
    assert len(store) == 1


# --- starting a conversation over the API ---------------------------------------------------


@pytest.fixture
def chat(client):
    store = SessionStore()
    llm = FakeLLM([])
    app.dependency_overrides[get_session_store] = lambda: store
    app.dependency_overrides[get_llm] = lambda: llm
    assert client.post("/customers", json={"customer_id": "cust_a", "display_name": "Synthetic"}).status_code == 201
    from backend.tests.test_agent_api import token_for

    headers = {"Authorization": f"Bearer {token_for('cust_a')}"}

    class Chat:
        pass

    Chat.store, Chat.llm, Chat.headers = store, llm, headers
    return Chat


@pytest.mark.parametrize(("body", "expected"), [(None, "en"), ({}, "en"), ({"language": "en"}, "en"),
                                                 ({"language": "hi"}, "hi")])
def test_a_conversation_starts_in_the_chosen_language(client, chat, body, expected) -> None:
    response = client.post("/agent/sessions", headers=chat.headers, **({} if body is None else {"json": body}))

    assert response.status_code == 201
    assert chat.store.get_for_customer(response.json()["session_id"], "cust_a").language == expected


@pytest.mark.parametrize("body", [{"language": "fr"}, {"language": "HI"}, {"language": "hi", "customer_id": "x"}])
def test_an_unknown_language_or_extra_field_is_refused(client, chat, body) -> None:
    assert client.post("/agent/sessions", headers=chat.headers, json=body).status_code == 422
    assert len(chat.store) == 0


def test_a_hindi_typed_conversation_gets_the_hindi_prompt_and_replies(client, chat) -> None:
    session_id = client.post("/agent/sessions", headers=chat.headers, json={"language": "hi"}).json()["session_id"]
    chat.llm.script.extend([say("नमस्ते!"), LLMUnavailableError("timeout")])

    first = client.post(f"/agent/sessions/{session_id}/turns", headers=chat.headers, json={"text": "नमस्ते"})
    second = client.post(f"/agent/sessions/{session_id}/turns", headers=chat.headers, json={"text": "balance?"})

    assert first.json()["reply"] == "नमस्ते!"
    assert chat.llm.requests[0]["system"] == HINDI.system_prompt
    assert second.json()["reply"] == HINDI.replies.unavailable


def test_an_english_typed_conversation_is_unchanged(client, chat) -> None:
    session_id = client.post("/agent/sessions", headers=chat.headers).json()["session_id"]
    chat.llm.script.append(LLMUnavailableError("timeout"))

    reply = client.post(f"/agent/sessions/{session_id}/turns", headers=chat.headers, json={"text": "hi"})

    assert chat.llm.requests[0]["system"] == SYSTEM_PROMPT
    assert reply.json()["reply"] == ENGLISH.replies.unavailable


# --- fixed replies follow the language --------------------------------------------------------


@pytest.fixture
def hindi_session(db_session: Session):
    seed_customer(db_session, "cust_a", [("acct_a_1", "INR", "active")])
    return SessionStore().create("cust_a", "hi")


def test_the_agent_loops_fixed_replies_are_in_hindi(db_session, hindi_session, monkeypatch) -> None:
    llm = FakeLLM([say(""), say("ठीक है।")])
    assert run_turn(hindi_session, "हम्म", llm=llm, db=db_session).reply == HINDI.replies.empty

    hindi_session.state.handoff = Handoff(reason="requested_human", turn=1)
    assert run_turn(hindi_session, "hello?", llm=llm, db=db_session).reply == HINDI.replies.handed_off

    monkeypatch.setattr(config, "MAX_CALLER_TURNS", 0)
    other = SessionStore().create("cust_a", "hi")
    assert run_turn(other, "hello", llm=llm, db=db_session).reply == HINDI.replies.turn_limit


def test_an_explicit_prompt_still_overrides_the_languages(db_session, hindi_session) -> None:
    llm = FakeLLM([say("ok")])

    run_turn(hindi_session, "hi", llm=llm, db=db_session, system="eval prompt")

    assert llm.requests[0]["system"] == "eval prompt"


def test_the_guard_answers_in_the_given_language() -> None:
    assert check_transcript("", None, min_confidence=0.6, replies=HINDI.replies).reply == HINDI.replies.not_understood
    assert check_transcript("x" * 2001, 0.99, min_confidence=0.6, replies=HINDI.replies).reply == HINDI.replies.too_long
    assert check_transcript("", None, min_confidence=0.6).reply == ENGLISH.replies.not_understood


def test_a_hindi_voice_turn_says_it_cant_hear_in_hindi(hindi_session, db_session) -> None:
    from backend.app.voice.speech import SpeechUnavailableError
    from backend.app.voice.turn import run_voice_turn
    from backend.tests.fake_speech import FakeSTT, FakeTTS
    from backend.tests.test_voice_turn import CLIP

    result = run_voice_turn(hindi_session, CLIP, stt=FakeSTT([SpeechUnavailableError("down")]), tts=FakeTTS(),
                            llm=FakeLLM([]), db=db_session)

    assert result.reply == HINDI.replies.cant_hear


def test_a_hindi_live_call_speaks_its_fixed_replies_in_hindi(db_factory, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(live_call, "WATCHDOG_TICK_S", 0.01)
    session = SessionStore().create("cust_a", "hi")
    llm = FakeLLM([])
    stt, tts, link, clock = FakeLiveSTT(), FakeLiveTTS(), FakeLink(), ManualClock()

    async def go():
        task = asyncio.create_task(run_live_call(session, link, stt=stt, tts=tts, llm=llm, db_factory=db_factory,
                                                 clock=clock))
        await asyncio.sleep(0.03)
        stt.listener.hear("end_of_turn", "pay fifty", (0.2,))  # misheard
        await asyncio.sleep(0.05)
        clock.now = 15 * 60
        return await task

    assert asyncio.run(asyncio.wait_for(go(), timeout=5)) == "time_limit"
    assert tts.speaker.said == [HINDI.replies.not_understood, HINDI.replies.time_limit]
    assert session.unheard_by_model == [HINDI.replies.not_understood]


# --- the routes no longer force the English prompt --------------------------------------------

from backend.tests.test_live_api import FRAME, live_api, start, until  # noqa: E402, F401  (live_api is a fixture)
from backend.tests.test_voice_api import auth, fakes, speak, voice_client  # noqa: E402, F401  (fixtures)


def test_a_hindi_live_call_uses_the_hindi_prompt(live_api) -> None:  # noqa: F811
    from backend.app.api.providers import get_hindi_live_stt, get_hindi_live_tts
    from backend.tests.fake_live_speech import ScriptedListener

    app.dependency_overrides[get_hindi_live_stt] = lambda: FakeLiveSTT(ScriptedListener({1: "मेरा balance?"}))
    app.dependency_overrides[get_hindi_live_tts] = lambda: FakeLiveTTS()
    session_id = live_api.store.create("cust_a", "hi").session_id

    with live_api.connect(session_id) as ws:
        start(ws)
        ws.send_bytes(FRAME)
        until(ws, lambda m: isinstance(m, dict) and m.get("type") == "reply_done")
        ws.send_text('{"type": "hang_up"}')

    assert live_api.llm.requests[0]["system"] == HINDI.system_prompt


def test_turn_based_voice_refuses_a_hindi_conversation(voice_client, fakes) -> None:  # noqa: F811
    # It hears and speaks English only; Hindi is for live calls (B2).
    session_id = voice_client.post("/agent/sessions", headers=auth(), json={"language": "hi"}).json()["session_id"]

    response = speak(voice_client, session_id)

    assert (response.status_code, response.json()["detail"]) == (422, "english_only")
    assert fakes.stt.clips == [] and fakes.llm.requests == []
