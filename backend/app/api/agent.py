from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.agent.claude import build_claude_client
from backend.app.agent.llm import LLMClient
from backend.app.agent.loop import run_turn
from backend.app.agent.prompts import SYSTEM_PROMPT
from backend.app.agent.sessions import SessionStore
from backend.app.auth.dependencies import authenticated_customer_id
from backend.app.db.session import get_db
from backend.app.services.conversation_events import (
    ConversationEvent,
    record_conversation_events,
)
from backend.app.services.customers import customer_exists

router = APIRouter(prefix="/agent", tags=["Agent"])

_session_store = SessionStore()


def get_session_store() -> SessionStore:
    return _session_store


@lru_cache(maxsize=1)
def _claude_client(model: str, effort: str) -> LLMClient:
    return build_claude_client(model=model, effort=effort)


def get_llm() -> LLMClient:
    # Off unless COLLECTWISE_AGENT_LLM=anthropic. Tests override this
    # dependency with a scripted fake.
    if config.AGENT_LLM != "anthropic":
        raise HTTPException(status_code=503, detail="Agent is not configured")
    return _claude_client(config.AGENT_MODEL, config.AGENT_EFFORT)


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


@router.post("/sessions", response_model=SessionCreatedResponse, status_code=201)
def create_session(
    customer_id: str = Depends(authenticated_customer_id),
    db: Session = Depends(get_db),
    store: SessionStore = Depends(get_session_store),
) -> SessionCreatedResponse:
    if not customer_exists(db, customer_id):
        raise HTTPException(status_code=404, detail="Customer not found")

    session = store.create(customer_id)
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


@router.post("/sessions/{session_id}/turns", response_model=TurnResponse)
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

    result = run_turn(session, body.text, llm=llm, db=db, system=SYSTEM_PROMPT)
    return TurnResponse(reply=result.reply, handed_off=result.handed_off)
