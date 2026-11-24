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
from sqlalchemy import or_, update
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

    # Loop detection.
    turn_count: int = 0
    turns_without_progress: int = 0

    escalation_reason: str | None = None
    handoff: HandoffSummary | None = None

    @property
    def aop_ref(self) -> str | None:
        if self.aop_id is None or self.aop_version is None:
            return None
        return f"{self.aop_id}@{self.aop_version}"

    def start_procedure(self, aop_id: str, version: int) -> None:
        self.aop_id, self.aop_version, self.current_step = aop_id, version, 1

    def end_procedure(self) -> None:
        self.aop_id = self.aop_version = self.current_step = None

    def remember_order(self, order_number: str, *, has_refunds: bool = False) -> None:
        if order_number not in self.owned_orders:
            self.owned_orders.append(order_number)
        if has_refunds and order_number not in self.orders_with_refunds:
            self.orders_with_refunds.append(order_number)

    def summary(self) -> str:
        """The state as the model sees it in the system prompt."""
        if self.verified:
            name = f" ({self.customer_name})" if self.customer_name else ""
            identity = f"verified as customer {self.customer_ref}{name}"
        else:
            identity = "not verified yet"
        lines = [
            f"- Channel: {self.channel}",
            f"- Customer identity: {identity}",
            f"- Failed verification attempts: {len(self.failed_verifications)}",
            f"- Turn: {self.turn_count}",
        ]
        if self.lookup_candidates and not self.verified:
            found = ", ".join(f"{cid} ({name})" for cid, name in self.lookup_candidates.items())
            lines.append(f"- Accounts found by lookup_customer: {found}")
        if self.slots:
            lines.append("- Known: " + ", ".join(f"{k}={v}" for k, v in sorted(self.slots.items())))
        if self.owned_orders:
            lines.append("- This customer's orders seen so far: " + ", ".join(self.owned_orders))
        return "\n".join(lines)


def credential_key(order_number: str, zip_code: str) -> str:
    raw = f"{order_number.strip().upper()}|{zip_code.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ConversationNotFoundError(LookupError):
    pass


class ConversationBusyError(RuntimeError):
    """Another turn is still running on this conversation."""


TURN_LEASE = timedelta(minutes=5)


def acquire_turn(session: Session, conversation_id: str, now: datetime | None = None) -> None:
    """Claim the conversation for one turn, or raise `ConversationBusyError`.

    A conditional UPDATE, so it's atomic on SQLite and Postgres alike and works across
    processes: only one caller can move the lease from "free or expired" to "held". Without
    it two concurrent turns would both load the same state (e.g. `auto_refunded_cents=0`),
    both pass the refund limit, and the last save would win.
    """
    now = now or datetime.now(UTC)
    claimed = session.execute(
        update(Conversation)
        .where(
            Conversation.id == conversation_id,
            or_(Conversation.turn_lease_until.is_(None), Conversation.turn_lease_until < now),
        )
        .values(turn_lease_until=now + TURN_LEASE)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 1:  # type: ignore[attr-defined]
        return
    if session.get(Conversation, conversation_id) is None:
        raise ConversationNotFoundError(conversation_id)
    raise ConversationBusyError(conversation_id)


def release_turn(session: Session, conversation_id: str) -> None:
    session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(turn_lease_until=None)
        .execution_options(synchronize_session=False)
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
