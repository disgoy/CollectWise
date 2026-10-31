from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent.llm import LLMClient
from backend.app.agent.loop import SessionBusyError, run_turn
from backend.app.agent.prompts import SYSTEM_PROMPT
from backend.app.agent.sessions import SessionStore, SessionStoreFullError
from backend.app.api.providers import get_llm, get_session_store
from backend.app.api.rate_limits import limit_session_starts, limit_turns
from backend.app.auth.dependencies import authenticated_customer_id
from backend.app.db.session import get_db
from backend.app.services.conversation_events import (
    ConversationEvent,
    record_conversation_events,
)
from backend.app.services.customers import customer_exists

router = APIRouter(prefix="/agent", tags=["Agent"])


class SessionCreatedResponse(BaseModel):
    session_id: str


class TurnRequest(BaseModel):
    # Only the caller's text: identity comes from the session token (D39).
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class TurnResponse(BaseModel):
    # No tool-call details: the caller's app has no use for internal tool
    # names or error codes.
    reply: str
    handed_off: bool


# Rate limits run first: route dependencies are resolved before parameters (D72).
@router.post(
    "/sessions",
    response_model=SessionCreatedResponse,
    status_code=201,
    dependencies=[Depends(limit_session_starts)],
)
def create_session(
    customer_id: str = Depends(authenticated_customer_id),
    db: Session = Depends(get_db),
    store: SessionStore = Depends(get_session_store),
) -> SessionCreatedResponse:
    if not customer_exists(db, customer_id):
        raise HTTPException(status_code=404, detail="Customer not found")

    try:
        session = store.create(customer_id)
    except SessionStoreFullError:
        raise HTTPException(status_code=503, detail="Service busy, please try again later") from None
    record_conversation_events(
        db,
        [
            ConversationEvent(
                session_id=session.session_id,
                customer_id=customer_id,
                turn=0,
                event_type="session_started",
            )
        ],
    )
    return SessionCreatedResponse(session_id=session.session_id)


@router.post(
    "/sessions/{session_id}/turns",
    response_model=TurnResponse,
    dependencies=[Depends(limit_turns)],
)
def send_turn(
    session_id: str,
    body: TurnRequest,
    customer_id: str = Depends(authenticated_customer_id),
    db: Session = Depends(get_db),
    store: SessionStore = Depends(get_session_store),
    llm: LLMClient = Depends(get_llm),
) -> TurnResponse:
    # Someone else's session looks exactly like a missing one.
    session = store.get_for_customer(session_id, customer_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        result = run_turn(session, body.text, llm=llm, db=db, system=SYSTEM_PROMPT,
                          lock_timeout=config.TURN_LOCK_WAIT_S)
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="Still answering your last message") from None
    return TurnResponse(reply=result.reply, handed_off=result.handed_off)
