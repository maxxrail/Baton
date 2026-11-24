"""Conversation state: everything the runtime must remember between turns.

The state is a Pydantic model stored as JSON in `conversations.state` and loaded at the start of
every turn. `customer_ref` and `status` are mirrored into their own columns so they can be
queried (the inbox in Part 04 filters on them).

Identity lives here, not in the prompt: `verified` flips to True only when the runtime sees
`verify_customer` return `verified: true`, and `customer_ref` is the id it was verified for.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from baton.db.models import Conversation, ConversationStatus

Channel = Literal["web", "email", "sms", "cli"]
Status = Literal["open", "escalated", "resolved"]


class HandoffSummary(BaseModel):
    """What the human teammate reads first when they pick up an escalated conversation."""

    customer: str
    intent: str
    tried: list[str]
    needed: str
    reason: str
    generated_by: Literal["llm", "fallback"]


class ConversationState(BaseModel):
    conversation_id: str
    channel: Channel
    status: Status = "open"

    # Identity. `customer_ref` is None until verification succeeds.
    customer_ref: str | None = None
    verified: bool = False
    customer_name: str | None = None
    # Accounts `lookup_customer` returned (id -> first name), so the name is known once one of
    # them is verified.
    lookup_candidates: dict[str, str] = Field(default_factory=dict)
    # Hashes of the (order number, zip) pairs that failed verification. A pair tried against
    # two candidate accounts (shared phone) counts once; the zip itself is never stored.
    failed_verifications: list[str] = Field(default_factory=list)

    # The active procedure.
    aop_id: str | None = None
    aop_version: int | None = None
    current_step: int | None = None
    slots: dict[str, str] = Field(default_factory=dict)

    # Facts the output guardrails check replies against.
    owned_orders: list[str] = Field(default_factory=list)
    orders_with_refunds: list[str] = Field(default_factory=list)
    auto_refunded_cents: int = 0
    # Side effects the store confirmed ("paused subscription sub_0001"), so a later turn
    # knows they happened. History sent to the model is text only; without this the agent
    # second-guessed its own success after a procedure ended.
    completed_actions: list[str] = Field(default_factory=list)
    # Help-center chunks returned by the most recent `search_knowledge`, so a follow-up answer
    # in the next turn is grounding-checked against the same passages (Part 03).
    kb_evidence: list[int] = Field(default_factory=list)

    # Loop detection.
    turn_count: int = 0
    turns_without_progress: int = 0

    escalation_reason: str | None = None
    handoff: HandoffSummary | None = None
    # The operator who took the conversation over in the console (Part 04), if any.
    assignee: str | None = None

    @property
    def aop_ref(self) -> str | None:
        if self.aop_id is None or self.aop_version is None:
            return None
        return f"{self.aop_id}@{self.aop_version}"

    def start_procedure(self, aop_id: str, version: int) -> None:
        self.aop_id, self.aop_version, self.current_step = aop_id, version, 1

    def record_completed(self, action: str) -> None:
        self.completed_actions = ([*self.completed_actions, action])[-10:]

    def end_procedure(self) -> None:
        self.aop_id = self.aop_version = self.current_step = None

    def remember_order(self, order_number: str, *, has_refunds: bool = False) -> None:
        if order_number not in self.owned_orders:
            self.owned_orders.append(order_number)
        if has_refunds and order_number not in self.orders_with_refunds:
            self.orders_with_refunds.append(order_number)

    def summary(self, max_failed_verifications: int | None = None) -> str:
        """The state as the model sees it in the system prompt. With the limit, the model is
        told how many verification attempts are left (it miscounted them otherwise)."""
        if self.verified:
            name = f" ({self.customer_name})" if self.customer_name else ""
            identity = f"verified as customer {self.customer_ref}{name}"
        else:
            identity = "not verified yet"
        lines = [
            f"- Channel: {self.channel}",
            f"- Customer identity: {identity}",
            "- Failed verification attempts: "
            + _attempts(len(self.failed_verifications), max_failed_verifications),
            f"- Turn: {self.turn_count}",
        ]
        if self.lookup_candidates and not self.verified:
            found = ", ".join(f"{cid} ({name})" for cid, name in self.lookup_candidates.items())
            lines.append(f"- Accounts found by lookup_customer: {found}")
        if self.slots:
            lines.append("- Known: " + ", ".join(f"{k}={v}" for k, v in sorted(self.slots.items())))
        if self.owned_orders:
            lines.append("- This customer's orders seen so far: " + ", ".join(self.owned_orders))
        if self.completed_actions:
            done = "; ".join(self.completed_actions)
            lines.append(f"- Done in this conversation (confirmed by the store): {done}")
        return "\n".join(lines)


def _attempts(failed: int, limit: int | None) -> str:
    if limit is None:
        return str(failed)
    left = max(limit - failed, 0)
    return f"{failed} of {limit} ({left} left before a teammate takes over)"


def credential_key(order_number: str, zip_code: str) -> str:
    raw = f"{order_number.strip().upper()}|{zip_code.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ConversationNotFoundError(LookupError):
    pass


class ConversationBusyError(RuntimeError):
    """Another turn is still running on this conversation."""


class LeaseLostError(RuntimeError):
    """The turn ran past its lease and someone else (an operator, or a newer turn) claimed the
    conversation. The turn's results are discarded rather than written over theirs."""


TURN_LEASE = timedelta(minutes=5)


def acquire_turn(session: Session, conversation_id: str, now: datetime | None = None) -> datetime:
    """Claim the conversation for one turn, or raise `ConversationBusyError`.

    A conditional UPDATE, so it's atomic on SQLite and Postgres alike and works across
    processes: only one caller can move the lease from "free or expired" to "held". Without
    it two concurrent turns would both load the same state (e.g. `auto_refunded_cents=0`),
    both pass the refund limit, and the last save would win.

    Returns the lease's expiry, which doubles as the holder's token: `holds_turn` and
    `release_turn` check it, so a turn whose lease expired can't save over (or release) a
    lease someone else has claimed since.
    """
    now = now or datetime.now(UTC)
    lease = now + TURN_LEASE
    claimed = session.execute(
        update(Conversation)
        .where(
            Conversation.id == conversation_id,
            or_(Conversation.turn_lease_until.is_(None), Conversation.turn_lease_until < now),
        )
        .values(turn_lease_until=lease)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 1:  # type: ignore[attr-defined]
        return lease
    if session.get(Conversation, conversation_id) is None:
        raise ConversationNotFoundError(conversation_id)
    raise ConversationBusyError(conversation_id)


def holds_turn(session: Session, conversation_id: str, lease: datetime) -> bool:
    current = session.execute(
        select(Conversation.turn_lease_until).where(Conversation.id == conversation_id)
    ).scalar_one_or_none()
    return current is not None and current == lease


def release_turn(session: Session, conversation_id: str, lease: datetime | None = None) -> None:
    """Free the lease; with `lease`, only if it is still that one."""
    query = update(Conversation).where(Conversation.id == conversation_id)
    if lease is not None:
        query = query.where(Conversation.turn_lease_until == lease)
    session.execute(
        query.values(turn_lease_until=None).execution_options(synchronize_session=False)
    )


def load_state(session: Session, conversation_id: str) -> ConversationState:
    """Load a conversation's state. Turns hold the lease from `acquire_turn` while they run."""
    convo = session.get(Conversation, conversation_id)
    if convo is None:
        raise ConversationNotFoundError(conversation_id)
    if convo.state:
        state = ConversationState.model_validate(convo.state)
    else:
        state = ConversationState.model_validate(
            {"conversation_id": convo.id, "channel": convo.channel}
        )
    return state


def save_state(session: Session, state: ConversationState) -> None:
    convo = session.get(Conversation, state.conversation_id)
    if convo is None:
        raise ConversationNotFoundError(state.conversation_id)
    convo.state = state.model_dump(mode="json")
    convo.customer_ref = state.customer_ref
    convo.status = ConversationStatus(state.status).value
