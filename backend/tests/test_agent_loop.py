import json
import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent.llm import LLMUnavailableError
from backend.app.agent.loop import (
    MAX_MODEL_CALLS,
    REPLY_CANNOT_HELP,
    REPLY_EMPTY,
    REPLY_HANDED_OFF,
    REPLY_STUCK,
    REPLY_TURN_LIMIT,
    REPLY_UNAVAILABLE,
    ToolCallRecord,
    run_turn,
)
from backend.app.agent.sessions import ConversationSession, SessionStore
from backend.app.db import models
from backend.app.tools.handoff import HandoffToHumanArgs
from backend.app.tools.registry import tool_schemas
from backend.tests.fake_llm import FakeLLM, FakeResponse, TextBlock, say, use
from backend.tests.helpers import seed_customer

SYSTEM = "test system prompt"


@pytest.fixture
def session(db_session: Session) -> ConversationSession:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    return SessionStore().create("cust_a")


def turn(session: ConversationSession, db: Session, llm: FakeLLM, text: str = "hi"):
    return run_turn(session, text, llm=llm, db=db, system=SYSTEM)


def payment_count(db: Session) -> int:
    return db.scalar(
        select(func.count())
        .select_from(models.ServicingRequestRecord)
        .where(models.ServicingRequestRecord.operation == "request_payment")
    )


def tool_results(message: dict) -> list[dict]:
    assert message["role"] == "user"
    return message["content"]


def test_plain_answer(session, db_session) -> None:
    llm = FakeLLM([say("Hello! How can I help?")])

    result = turn(session, db_session, llm, "hi")

    assert result.reply == "Hello! How can I help?"
    assert result.handed_off is False
    assert result.tool_calls == []
    assert session.state.turn == 1
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


def test_model_receives_system_prompt_and_registered_tools(session, db_session) -> None:
    llm = FakeLLM([say("ok")])

    turn(session, db_session, llm)

    assert llm.requests[0]["system"] == SYSTEM
    assert llm.requests[0]["tools"] == tool_schemas()


def test_tool_round_trip(session, db_session) -> None:
    llm = FakeLLM([use(("t1", "list_accounts", {})), say("You have one account.")])

    result = turn(session, db_session, llm, "what accounts do I have?")

    assert result.reply == "You have one account."
    assert result.tool_calls == [ToolCallRecord("list_accounts", True, None)]

    # The second model call saw the tool result, linked to its tool_use id.
    [tool_result] = tool_results(llm.requests[1]["messages"][-1])
    assert tool_result["tool_use_id"] == "t1"
    assert tool_result["is_error"] is False
    payload = json.loads(tool_result["content"])
    assert payload["data"]["accounts"][0]["account_id"] == "acct_a_1"


def test_parallel_tool_calls_return_in_one_message(session, db_session) -> None:
    llm = FakeLLM([
        use(("t1", "list_accounts", {}), ("t2", "list_recent_payment_requests", {})),
        say("done"),
    ])

    turn(session, db_session, llm)

    results = tool_results(session.messages[2])
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert [m["role"] for m in session.messages] == ["user", "assistant", "user", "assistant"]


def test_failed_tool_call_is_marked_as_error(session, db_session) -> None:
    llm = FakeLLM([use(("t1", "propose_payment", {"account_id": "acct_a_1"})), say("?")])

    result = turn(session, db_session, llm)

    [tool_result] = tool_results(session.messages[2])
    assert tool_result["is_error"] is True
    assert json.loads(tool_result["content"])["error"] == "invalid_arguments"
    assert result.tool_calls == [ToolCallRecord("propose_payment", False, "invalid_arguments")]


def test_payment_needs_a_second_caller_turn(session, db_session) -> None:
    propose = ("t1", "propose_payment", {"account_id": "acct_a_1", "amount": "200.00"})
    llm = FakeLLM([
        # Turn 1: the model proposes, then tries to confirm immediately.
        use(propose),
        use(("t2", "confirm_payment", {})),
        say("Shall I request a payment of 200.00 USD?"),
        # Turn 2: the caller says yes.
        use(("t3", "confirm_payment", {})),
        say("Your payment request is recorded."),
    ])

    first = turn(session, db_session, llm, "pay 200")

    assert first.tool_calls[1] == ToolCallRecord("confirm_payment", False, "confirmation_required")
    assert payment_count(db_session) == 0

    second = turn(session, db_session, llm, "yes")

    assert second.tool_calls == [ToolCallRecord("confirm_payment", True, None)]
    assert second.reply == "Your payment request is recorded."
    assert payment_count(db_session) == 1


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_unusable_response_gets_a_safe_reply(session, db_session, stop_reason) -> None:
    llm = FakeLLM([FakeResponse(stop_reason=stop_reason, content=[TextBlock("partial")])])

    result = turn(session, db_session, llm)

    assert result.reply == REPLY_CANNOT_HELP


def test_tool_use_without_tool_blocks_never_sends_empty_results(session, db_session) -> None:
    llm = FakeLLM([FakeResponse(stop_reason="tool_use", content=[TextBlock("hmm")])])

    result = turn(session, db_session, llm)

    assert result.reply == REPLY_CANNOT_HELP
    assert session.messages[-1]["role"] == "assistant"


def test_empty_final_answer(session, db_session) -> None:
    llm = FakeLLM([FakeResponse(stop_reason="end_turn", content=[])])

    assert turn(session, db_session, llm).reply == REPLY_EMPTY


def test_model_call_limit_hands_off(session, db_session) -> None:
    llm = FakeLLM([use((f"t{i}", "list_accounts", {})) for i in range(MAX_MODEL_CALLS)])

    result = turn(session, db_session, llm)

    assert len(llm.requests) == MAX_MODEL_CALLS
    assert result.reply == REPLY_STUCK
    assert result.handed_off is True
    assert result.tool_calls[-1] == ToolCallRecord("handoff_to_human", True, None)
    assert session.state.handoff.reason == "unresolved_issue"


def test_llm_outage_gives_safe_reply_and_next_turn_still_works(session, db_session) -> None:
    llm = FakeLLM([LLMUnavailableError("timeout"), say("I'm back.")])

    first = turn(session, db_session, llm, "hello?")
    second = turn(session, db_session, llm, "hello again")

    assert first.reply == REPLY_UNAVAILABLE
    assert second.reply == "I'm back."
    # Both caller messages reached the model; the history stayed valid.
    assert [m["content"] for m in llm.requests[1]["messages"] if m["role"] == "user"] == [
        "hello?",
        "hello again",
    ]


def test_bugs_in_our_code_are_not_hidden(session, db_session) -> None:
    llm = FakeLLM([RuntimeError("bug")])

    with pytest.raises(RuntimeError):
        turn(session, db_session, llm)


def test_after_handoff_the_model_is_not_called(session, db_session) -> None:
    llm = FakeLLM([use(("t1", "handoff_to_human", {"reason": "caller_requested"})), say("Transferring you.")])
    first = turn(session, db_session, llm, "a person please")
    assert first.handed_off is True

    second = turn(session, db_session, llm, "hello?")

    assert second == type(second)(reply=REPLY_HANDED_OFF, handed_off=True)
    assert len(llm.requests) == 2
    assert session.state.turn == 1


def test_turns_in_one_session_never_overlap(session, db_session) -> None:
    active = 0
    max_active = 0
    guard = threading.Lock()

    class SlowLLM:
        def create(self, **_):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return say("ok")

    threads = [
        threading.Thread(target=run_turn, args=(session, f"msg {i}"),
                         kwargs={"llm": SlowLLM(), "db": db_session, "system": SYSTEM})
        for i in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_active == 1
    assert session.state.turn == 3


def test_session_store_scopes_sessions_to_their_customer() -> None:
    store = SessionStore()
    mine = store.create("cust_a")
    other = store.create("cust_a")

    assert mine.session_id != other.session_id
    assert mine.session_id.startswith("sess_")
    assert store.get_for_customer(mine.session_id, "cust_a") is mine
    assert store.get_for_customer(mine.session_id, "cust_b") is None
    assert store.get_for_customer("sess_missing", "cust_a") is None


# --- turn limit (D72) ---------------------------------------------------------


def test_the_turn_after_the_limit_goes_to_a_person_without_a_model_call(
    session, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(config, "MAX_CALLER_TURNS", 3)
    llm = FakeLLM([say("ok"), say("ok"), say("ok")])
    for _ in range(3):
        assert turn(session, db_session, llm).reply == "ok"
    messages_before = list(session.messages)

    result = turn(session, db_session, llm, "one more thing")

    assert result.reply == REPLY_TURN_LIMIT
    assert result.handed_off is True
    assert result.tool_calls == []
    assert len(llm.requests) == 3
    assert session.state.turn == 3  # not counted
    assert session.state.handoff.reason == "turn_limit"
    assert session.messages == messages_before

    after = turn(session, db_session, llm)
    assert after.reply == REPLY_HANDED_OFF
    assert len(llm.requests) == 3


def test_the_shipped_limit_is_30_turns(session, db_session) -> None:
    assert config.MAX_CALLER_TURNS == 30
    llm = FakeLLM([say("ok")] * 30)
    for _ in range(30):
        assert turn(session, db_session, llm).reply == "ok"

    assert turn(session, db_session, llm).reply == REPLY_TURN_LIMIT
    assert len(llm.requests) == 30


def test_the_turn_limit_cancels_a_pending_payment(session, db_session, monkeypatch) -> None:
    monkeypatch.setattr(config, "MAX_CALLER_TURNS", 1)
    propose = ("t1", "propose_payment", {"account_id": "acct_a_1", "amount": "200.00"})
    llm = FakeLLM([use(propose), say("Shall I submit 200 USD?")])
    turn(session, db_session, llm, "pay 200")
    assert session.state.pending_payment is not None

    turn(session, db_session, llm, "yes")

    assert session.state.pending_payment is None
    assert payment_count(db_session) == 0


def test_the_turn_limit_is_recorded(session, db_session, monkeypatch) -> None:
    monkeypatch.setattr(config, "MAX_CALLER_TURNS", 0)

    turn(session, db_session, FakeLLM([]))

    events = db_session.scalars(
        select(models.ConversationEventRecord).where(
            models.ConversationEventRecord.session_id == session.session_id
        )
    ).all()
    assert [(e.event_type, e.outcome) for e in events] == [("turn_completed", "turn_limit")]


def test_the_model_cannot_choose_the_turn_limit_reason() -> None:
    reasons = HandoffToHumanArgs.model_json_schema()["properties"]["reason"]["enum"]

    assert "turn_limit" not in reasons
