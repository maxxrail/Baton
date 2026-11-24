"""Conversation endpoints: start a conversation, send a customer message, read state and traces.

- `POST /conversations` `{channel, customer_hint?}` -> the new conversation
- `POST /conversations/{id}/messages` `{text}` -> `AgentReply` (reply text, status, trace id)
- `GET /conversations/{id}` -> status, state and messages
- `GET /conversations/{id}/traces` -> one trace per agent turn, in order

The agent is built on first use (see `get_agent`), so the app starts without an LLM key; a
message sent with no LLM configured gets a 503 that says how to fix it.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from baton.db.models import Message, Trace
from baton.runtime.agent import Agent, AgentReply
from baton.runtime.factory import AgentUnavailableError
from baton.runtime.state import (
    Channel,
    ConversationBusyError,
    ConversationNotFoundError,
    ConversationState,
    load_state,
)

MAX_MESSAGE_CHARS = 4000

router = APIRouter(prefix="/conversations", tags=["conversations"])


def get_agent(request: Request) -> Agent:
    try:
        agent: Agent = request.app.state.get_agent()
    except AgentUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return agent


def get_sessions(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory()
    return factory


AgentDep = Annotated[Agent, Depends(get_agent)]
SessionsDep = Annotated[sessionmaker[Session], Depends(get_sessions)]


class ConversationIn(BaseModel):
    channel: Channel = "web"
    customer_hint: str | None = Field(
        default=None, max_length=254, description="e.g. the email a logged-in widget knows"
    )


class ConversationOut(BaseModel):
    id: str
    channel: str
    status: str


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime


class ConversationDetailOut(ConversationOut):
    state: ConversationState
    messages: list[MessageOut]


class TraceOut(BaseModel):
    id: str
    message_id: int | None
    created_at: datetime
    payload: dict[str, object]


def _state_or_404(session: Session, conversation_id: str) -> ConversationState:
    try:
        return load_state(session, conversation_id)
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="conversation not found") from None


@router.post("", status_code=201)
def create_conversation(body: ConversationIn, agent: AgentDep) -> ConversationOut:
    cid = agent.start_conversation(body.channel, body.customer_hint)
    return ConversationOut(id=cid, channel=body.channel, status="open")


@router.post("/{conversation_id}/messages")
def post_message(
    conversation_id: str, body: MessageIn, agent: AgentDep, sessions: SessionsDep
) -> AgentReply:
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="message is empty")
    with sessions() as session:
        _state_or_404(session, conversation_id)
    try:
        return agent.handle_turn(conversation_id, body.text)
    except ConversationBusyError:
        raise HTTPException(
            status_code=409, detail="a reply to the previous message is still being written"
        ) from None


@router.get("/{conversation_id}")
def get_conversation(conversation_id: str, sessions: SessionsDep) -> ConversationDetailOut:
    with sessions() as session:
        state = _state_or_404(session, conversation_id)
        rows = session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
        ).all()
        messages = [
            MessageOut(id=m.id, role=m.role, content=m.content, created_at=m.created_at)
            for m in rows
        ]
    return ConversationDetailOut(
        id=conversation_id,
        channel=state.channel,
        status=state.status,
        state=state,
        messages=messages,
    )


@router.get("/{conversation_id}/traces")
def get_traces(conversation_id: str, sessions: SessionsDep) -> list[TraceOut]:
    with sessions() as session:
        _state_or_404(session, conversation_id)
        rows = session.scalars(select(Trace).where(Trace.conversation_id == conversation_id)).all()
        traces = [
            TraceOut(id=t.id, message_id=t.message_id, created_at=t.created_at, payload=t.payload)
            for t in rows
        ]
    return sorted(traces, key=lambda t: int(str(t.payload.get("turn", 0))))
