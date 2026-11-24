"""Operator endpoints for the console (`/ops/...`) and its live inbox socket (`/ws/ops`).

- `POST /ops/login` `{name, secret}` sets the session cookie; `POST /ops/logout`; `GET /ops/me`
- `GET /ops/conversations?status=&channel=&limit=` the inbox, most recently updated first
- `POST /ops/conversations/{id}/takeover` the operator owns the conversation; the agent stops
- `POST /ops/conversations/{id}/reply` `{text}` sent as `human_agent` through the original
  channel (409 unless the conversation was taken over / escalated)
- `POST /ops/conversations/{id}/handback` `{action: "resolve" | "return_to_agent"}`
- `WS /ws/ops` pushes `{"type": "updated" | "activity", "conversation_id", ...}` for every
  change; the console re-reads what it shows. Authenticated by the session cookie.

Conversation detail and traces are `GET /conversations/{id}` and `/traces` (operator-only).
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from baton.api.auth import (
    COOKIE,
    SESSION_TTL_S,
    Operator,
    cookie_secure,
    secret_matches,
    session_token,
    valid_name,
    websocket_operator,
)
from baton.api.deps import ServiceDep, SessionsDep
from baton.channels.events import INBOX, EventHub
from baton.config import Settings
from baton.db.models import Conversation, Message, MessageRole
from baton.runtime.handoff import MAX_HUMAN_REPLY_CHARS, HandBack, NotHumanOwnedError
from baton.runtime.state import (
    ConversationBusyError,
    ConversationNotFoundError,
    ConversationState,
)

router = APIRouter(prefix="/ops", tags=["console"])
socket_router = APIRouter(tags=["console"])

CLOSE_UNAUTHORIZED = 4401
PREVIEW_CHARS = 140


class LoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    secret: str = Field(min_length=1, max_length=500)


class MeOut(BaseModel):
    name: str


class InboxItem(BaseModel):
    id: str
    channel: str
    status: str
    contact: str | None
    subject: str | None
    customer_ref: str | None
    aop_ref: str | None
    assignee: str | None
    escalation_reason: str | None
    last_role: str | None
    last_message: str | None
    message_count: int
    updated_at: datetime


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_HUMAN_REPLY_CHARS)


class ReplyOut(BaseModel):
    delivered: bool
    error: str | None = None


class HandBackIn(BaseModel):
    action: HandBack


class StateOut(BaseModel):
    status: str
    state: ConversationState


@router.post("/login", status_code=204)
def login(body: LoginIn, request: Request, response: Response) -> None:
    settings: Settings = request.app.state.settings
    secret = (
        settings.baton_console_secret.get_secret_value() if settings.baton_console_secret else ""
    )
    if not secret:
        raise HTTPException(
            status_code=503, detail="set BATON_CONSOLE_SECRET to enable the console"
        )
    if not valid_name(body.name):
        raise HTTPException(status_code=422, detail="name: letters, digits, space, . _ - only")
    if not secret_matches(secret, body.secret):
        raise HTTPException(status_code=401, detail="wrong secret")
    response.set_cookie(
        COOKIE,
        session_token(secret, body.name),
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(settings, request.url.scheme),
    )


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(COOKIE)


@router.get("/me")
def me(operator: Operator) -> MeOut:
    return MeOut(name=operator)


Status = Literal["open", "escalated", "resolved"]
ChannelFilter = Literal["web", "email", "sms", "cli"]


@router.get("/conversations")
def inbox(
    operator: Operator,
    sessions: SessionsDep,
    status: Status | None = None,
    channel: ChannelFilter | None = None,
    limit: int = 100,
) -> list[InboxItem]:
    del operator
    limit = max(1, min(limit, 500))
    with sessions() as session:
        return _inbox(session, status, channel, limit)


def _inbox(
    session: Session, status: str | None, channel: str | None, limit: int
) -> list[InboxItem]:
    query = select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
    if status:
        query = query.where(Conversation.status == status)
    if channel:
        query = query.where(Conversation.channel == channel)
    convos = session.scalars(query).all()
    ids = [c.id for c in convos]
    if not ids:
        return []
    # The newest non-system message and the message count per conversation, in two queries.
    visible = Message.role != MessageRole.SYSTEM.value
    stats = session.execute(
        select(Message.conversation_id, func.max(Message.id), func.count(Message.id))
        .where(Message.conversation_id.in_(ids), visible)
        .group_by(Message.conversation_id)
    ).all()
    last_ids = {cid: last for cid, last, _ in stats}
    counts = {cid: n for cid, _, n in stats}
    last = {
        m.conversation_id: m
        for m in session.scalars(select(Message).where(Message.id.in_(list(last_ids.values()))))
    }
    items = []
    for c in convos:
        state = ConversationState.model_validate(c.state) if c.state else None
        msg = last.get(c.id)
        items.append(
            InboxItem(
                id=c.id,
                channel=c.channel,
                status=c.status,
                contact=c.contact,
                subject=c.subject,
                customer_ref=c.customer_ref,
                aop_ref=state.aop_ref if state else None,
                assignee=state.assignee if state else None,
                escalation_reason=state.escalation_reason if state else None,
                last_role=msg.role if msg else None,
                last_message=msg.content[:PREVIEW_CHARS] if msg else None,
                message_count=counts.get(c.id, 0),
                updated_at=c.updated_at,
            )
        )
    return items


def _guard[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="conversation not found") from None
    except ConversationBusyError:
        raise HTTPException(
            status_code=409, detail="the agent is mid-reply on this conversation; try again"
        ) from None
    except NotHumanOwnedError:
        raise HTTPException(
            status_code=409, detail="take the conversation over before replying"
        ) from None


@router.post("/conversations/{conversation_id}/takeover")
def take_over(conversation_id: str, operator: Operator, service: ServiceDep) -> StateOut:
    state = _guard(lambda: service.take_over(conversation_id, operator))
    return StateOut(status=state.status, state=state)


@router.post("/conversations/{conversation_id}/reply")
def reply(conversation_id: str, body: ReplyIn, operator: Operator, service: ServiceDep) -> ReplyOut:
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="reply is empty")
    delivery = _guard(lambda: service.human_reply(conversation_id, operator, body.text))
    return ReplyOut(delivered=delivery.delivered, error=delivery.error)


@router.post("/conversations/{conversation_id}/handback")
def hand_back(
    conversation_id: str, body: HandBackIn, operator: Operator, service: ServiceDep
) -> StateOut:
    state = _guard(lambda: service.hand_back(conversation_id, operator, body.action))
    return StateOut(status=state.status, state=state)


@socket_router.websocket("/ws/ops")
async def inbox_socket(websocket: WebSocket) -> None:
    operator = await run_in_threadpool(websocket_operator, websocket)
    await websocket.accept()
    if operator is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="log in to the console first")
        return
    hub: EventHub = websocket.app.state.hub
    sub = hub.subscribe(INBOX)

    async def pump() -> None:
        async for event in sub:
            await websocket.send_json(event)

    task = asyncio.create_task(pump())
    try:
        while True:  # the console sends nothing; this notices the disconnect
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        hub.unsubscribe(sub)
