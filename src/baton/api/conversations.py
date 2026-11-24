"""Conversation endpoints.

Customer-facing (the widget; a conversation id is an unguessable capability):
- `POST /conversations` `{channel, customer_hint?}` -> the new conversation
- `POST /conversations/{id}/messages` `{text}` -> the agent's reply (`ChatReply`); the HTTP
  fallback for the widget's WebSocket (`baton.api.chat`)
- `GET /conversations/{id}/messages` -> the transcript the customer may see (no system notes,
  no state), with bodies formatted for the web

Operator-only (console login or `Authorization: Bearer <BATON_CONSOLE_SECRET>`):
- `GET /conversations/{id}` -> status, full state and every message (system notes included)
- `GET /conversations/{id}/traces` -> one trace per agent turn, in order

The agent is built on first use, so the app starts without an LLM key; a message sent with no
LLM configured gets a 503 that says how to fix it.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from baton.api.auth import Operator
from baton.api.deps import AgentDep, ServiceDep, SessionsDep
from baton.channels.base import OutboundContext, format_reply
from baton.channels.links import valid_transcript_token
from baton.db.models import Conversation, Message, MessageRole, Trace
from baton.runtime.agent import AgentReply, Citation
from baton.runtime.state import (
    Channel,
    ConversationBusyError,
    ConversationNotFoundError,
    ConversationState,
    LeaseLostError,
    load_state,
)

MAX_MESSAGE_CHARS = 4000
CUSTOMER_VISIBLE = (
    MessageRole.CUSTOMER.value,
    MessageRole.AGENT.value,
    MessageRole.HUMAN_AGENT.value,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


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
    meta: dict[str, object] | None = None
    created_at: datetime


class ConversationDetailOut(ConversationOut):
    contact: str | None
    subject: str | None
    state: ConversationState
    messages: list[MessageOut]


class TraceOut(BaseModel):
    id: str
    message_id: int | None
    created_at: datetime
    payload: dict[str, object]


class ChatReply(AgentReply):
    """`AgentReply` plus `body`: the reply formatted for the widget (markdown links)."""

    body: str = ""


class TranscriptMessage(BaseModel):
    id: int
    role: str
    body: str
    citations: list[Citation] = Field(default_factory=list)
    created_at: datetime


class TranscriptOut(BaseModel):
    id: str
    status: str
    messages: list[TranscriptMessage]


def _state_or_404(session: Session, conversation_id: str) -> ConversationState:
    try:
        return load_state(session, conversation_id)
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="conversation not found") from None


def message_citations(meta: dict[str, object] | None) -> list[Citation]:
    raw = (meta or {}).get("citations")
    if not isinstance(raw, list):
        return []
    return [Citation.model_validate(c) for c in raw]


@router.post("", status_code=201)
def create_conversation(
    body: ConversationIn, agent: AgentDep, service: ServiceDep
) -> ConversationOut:
    del agent  # required only so a missing LLM fails here with a 503, not on first message
    cid = service.start(body.channel, body.customer_hint)
    return ConversationOut(id=cid, channel=body.channel, status="open")


@router.post("/{conversation_id}/messages")
def post_message(
    conversation_id: str,
    body: MessageIn,
    agent: AgentDep,
    service: ServiceDep,
    sessions: SessionsDep,
) -> ChatReply:
    del agent
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="message is empty")
    with sessions() as session:
        state = _state_or_404(session, conversation_id)
    if state.channel != "web":
        # Email and SMS conversations take messages only through their signed webhooks;
        # otherwise a leaked id would let anyone write into (and read replies from) them.
        raise HTTPException(status_code=404, detail="conversation not found")
    try:
        reply, _ = service.customer_message(conversation_id, body.text)
    except (ConversationBusyError, LeaseLostError):
        raise HTTPException(
            status_code=409, detail="a reply to the previous message is still being written"
        ) from None
    ctx = OutboundContext(conversation_id, web_url=service.web_url)
    rendered = format_reply("web", reply.text, reply.citations, ctx) if reply.replied else ""
    return ChatReply(**reply.model_dump(), body=rendered)


@router.get("/{conversation_id}/messages")
def get_transcript(
    conversation_id: str, sessions: SessionsDep, service: ServiceDep, t: str | None = None
) -> TranscriptOut:
    """A web conversation's id is the widget's own capability. Any other channel's transcript
    needs the signed `t` token from an SMS "full answer" link (baton.channels.links)."""
    with sessions() as session:
        state = _state_or_404(session, conversation_id)
        secret = service.link_secret or ""
        if state.channel != "web" and not valid_transcript_token(secret, conversation_id, t):
            raise HTTPException(status_code=404, detail="conversation not found")
        rows = session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.role.in_(CUSTOMER_VISIBLE))
            .order_by(Message.id)
        ).all()
        ctx = OutboundContext(conversation_id, web_url=service.web_url)
        messages = []
        for m in rows:
            cites = message_citations(m.meta)
            body = (
                m.content
                if m.role == MessageRole.CUSTOMER.value
                else format_reply("web", m.content, cites, ctx)
            )
            messages.append(
                TranscriptMessage(
                    id=m.id, role=m.role, body=body, citations=cites, created_at=m.created_at
                )
            )
    return TranscriptOut(id=conversation_id, status=state.status, messages=messages)


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: str, sessions: SessionsDep, operator: Operator
) -> ConversationDetailOut:
    del operator
    with sessions() as session:
        state = _state_or_404(session, conversation_id)
        convo = session.get(Conversation, conversation_id)
        assert convo is not None
        rows = session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
        ).all()
        messages = [
            MessageOut(
                id=m.id, role=m.role, content=m.content, meta=m.meta, created_at=m.created_at
            )
            for m in rows
        ]
        contact, subject = convo.contact, convo.subject
    return ConversationDetailOut(
        id=conversation_id,
        channel=state.channel,
        status=state.status,
        contact=contact,
        subject=subject,
        state=state,
        messages=messages,
    )


@router.get("/{conversation_id}/traces")
def get_traces(conversation_id: str, sessions: SessionsDep, operator: Operator) -> list[TraceOut]:
    del operator
    with sessions() as session:
        _state_or_404(session, conversation_id)
        rows = session.scalars(select(Trace).where(Trace.conversation_id == conversation_id)).all()
        traces = [
            TraceOut(id=t.id, message_id=t.message_id, created_at=t.created_at, payload=t.payload)
            for t in rows
        ]
    return sorted(traces, key=lambda t: int(str(t.payload.get("turn", 0))))
