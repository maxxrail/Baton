"""The human side of a handoff: an operator takes a conversation over, replies, hands it back.

A conversation is human-owned while its status is `escalated`: the agent stores the customer's
messages but doesn't answer (see `Agent.handle_turn`). These functions move a conversation
in and out of that mode. Each one claims the same turn lease the agent uses, so an operator
action can't interleave with an agent turn that's still writing its reply (that raises
`ConversationBusyError`, and the console asks the operator to try again).

Every change also leaves a `system` message in the transcript, so the history shows who did what.
Delivering a human reply to the customer is the channel layer's job (`baton.channels.service`).
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from baton.db.models import Message, MessageRole
from baton.db.session import session_scope
from baton.runtime.state import (
    ConversationState,
    acquire_turn,
    load_state,
    release_turn,
    save_state,
)

HandBack = Literal["resolve", "return_to_agent"]
MAX_HUMAN_REPLY_CHARS = 4000


class NotHumanOwnedError(RuntimeError):
    """The operator tried to reply without taking the conversation over first."""


@dataclass(frozen=True)
class HumanMessage:
    message_id: int
    state: ConversationState


def _note(session: Session, conversation_id: str, text: str) -> None:
    session.add(
        Message(conversation_id=conversation_id, role=MessageRole.SYSTEM.value, content=text)
    )


def take_over(
    sessions: sessionmaker[Session], conversation_id: str, operator: str
) -> ConversationState:
    """The operator owns the conversation from now on; the agent goes quiet.

    Taking over an escalated conversation keeps the agent's handoff summary and reason.
    Taking over an open one (an operator stepping in on their own) records why."""
    with session_scope(sessions) as session:
        lease = acquire_turn(session, conversation_id)
        try:
            state = load_state(session, conversation_id)
            if state.status != "escalated":
                state.status = "escalated"
                state.escalation_reason = f"taken over by {operator}"
            state.assignee = operator
            save_state(session, state)
            _note(session, conversation_id, f"{operator} took over the conversation.")
        finally:
            release_turn(session, conversation_id, lease)
    return state


def human_reply(
    sessions: sessionmaker[Session], conversation_id: str, operator: str, text: str
) -> HumanMessage:
    """Store the operator's reply. Only allowed while a human owns the conversation."""
    text = text.strip()
    if not text:
        raise ValueError("reply is empty")
    if len(text) > MAX_HUMAN_REPLY_CHARS:
        raise ValueError(f"reply is longer than {MAX_HUMAN_REPLY_CHARS} characters")
    with session_scope(sessions) as session:
        lease = acquire_turn(session, conversation_id)
        try:
            state = load_state(session, conversation_id)
            if state.status != "escalated":
                raise NotHumanOwnedError(conversation_id)
            if state.assignee is None:
                state.assignee = operator
                save_state(session, state)
            message = Message(
                conversation_id=conversation_id,
                role=MessageRole.HUMAN_AGENT.value,
                content=text,
                meta={"operator": operator},
            )
            session.add(message)
            session.flush()
            message_id = message.id
        finally:
            release_turn(session, conversation_id, lease)
    return HumanMessage(message_id, state)


def hand_back(
    sessions: sessionmaker[Session], conversation_id: str, operator: str, action: HandBack
) -> ConversationState:
    """End the human's ownership.

    `resolve`: the issue is closed (a new customer message reopens it with the agent).
    `return_to_agent`: the agent answers the next customer message.

    Either way the procedure the agent was in is ended, since the human may have changed
    things the agent's step doesn't know about; the next message is routed afresh. Identity
    stays as it was (verified customers stay verified, failed attempts still count), and the
    no-progress counter restarts."""
    with session_scope(sessions) as session:
        lease = acquire_turn(session, conversation_id)
        try:
            state = load_state(session, conversation_id)
            state.status = "resolved" if action == "resolve" else "open"
            state.end_procedure()
            state.turns_without_progress = 0
            state.escalation_reason = None
            state.handoff = None
            state.assignee = None
            save_state(session, state)
            what = "resolved the conversation" if action == "resolve" else "handed back to the AI"
            _note(session, conversation_id, f"{operator} {what}.")
        finally:
            release_turn(session, conversation_id, lease)
    return state
