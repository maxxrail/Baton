"""The web chat WebSocket: `/ws/chat/{conversation_id}`.

Client -> server:
- `{"type": "message", "text": "..."}` a customer message
- `{"type": "request_human"}` the widget's "talk to a human" button (sent to the agent as an
  explicit request, so it takes the same escalation path and handoff summary as typing it)

Server -> client:
- `{"type": "typing", "activity": "thinking" | "tool:get_order" | ... | ""}` while the agent
  works ("" = stopped)
- `{"type": "message", "message": {id, role, body, citations, created_at}}` an agent or human
  reply, `body` as markdown with citation links
- `{"type": "status", "status": "open" | "escalated" | "resolved", "note"?}`
- `{"type": "error", "code": "busy" | "unavailable" | "invalid" | "failed", "message": ...}`

Turns run one at a time per socket in a worker thread; events (from this socket's turns, other
tabs, or a human in the console) arrive through the event hub. The agent's reply text is sent
once it has passed every reply guardrail, never token by token (see `baton.runtime.agent`).
"""

import asyncio
import contextlib

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from baton.api.conversations import MAX_MESSAGE_CHARS
from baton.channels.events import EventHub, Subscription, conversation_topic
from baton.channels.service import REQUEST_HUMAN_TEXT, ConversationService
from baton.db.models import Conversation
from baton.runtime.factory import AgentUnavailableError
from baton.runtime.state import ConversationBusyError, LeaseLostError

router = APIRouter(tags=["chat"])
log = structlog.get_logger("baton.api.chat")

CLOSE_NOT_FOUND = 4404


async def _pump(websocket: WebSocket, sub: Subscription) -> None:
    async for event in sub:
        await websocket.send_json(event)


@router.websocket("/ws/chat/{conversation_id}")
async def chat_socket(websocket: WebSocket, conversation_id: str) -> None:
    service: ConversationService = await run_in_threadpool(websocket.app.state.get_service)
    hub: EventHub = websocket.app.state.hub
    found = await run_in_threadpool(_is_web_conversation, service, conversation_id)
    await websocket.accept()
    if not found:
        await websocket.close(code=CLOSE_NOT_FOUND, reason="conversation not found")
        return

    sub = hub.subscribe(conversation_topic(conversation_id))
    pump = asyncio.create_task(_pump(websocket, sub))
    try:
        while True:
            try:
                data = await websocket.receive_json()
            except ValueError:  # not JSON
                await _error(websocket, "invalid", "frames must be JSON")
                continue
            kind = data.get("type") if isinstance(data, dict) else None
            if kind == "request_human":
                text = REQUEST_HUMAN_TEXT
            elif kind == "message":
                text = str(data.get("text", "")).strip()
                if not text or len(text) > MAX_MESSAGE_CHARS:
                    await _error(websocket, "invalid", "message is empty or too long")
                    continue
            else:
                await _error(websocket, "invalid", "unknown message type")
                continue
            try:
                await run_in_threadpool(service.customer_message, conversation_id, text)
            except ConversationBusyError:
                await _error(websocket, "busy", "still replying to your previous message")
            except AgentUnavailableError as exc:
                await _error(websocket, "unavailable", str(exc))
            except LeaseLostError:
                await _error(websocket, "busy", "a teammate took over; your message was kept")
            except Exception as exc:  # keep the socket alive; the error trace has details
                error = type(exc).__name__
                log.error("chat_turn_failed", conversation_id=conversation_id, error=error)
                await _error(websocket, "failed", "something went wrong; please try again")
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump
        hub.unsubscribe(sub)


def _is_web_conversation(service: ConversationService, conversation_id: str) -> bool:
    with service.sessions() as session:
        convo = session.get(Conversation, conversation_id)
        return convo is not None and convo.channel == "web"


async def _error(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_json({"type": "error", "code": code, "message": message})
