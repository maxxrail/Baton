"""Turns on one conversation can't interleave, and the refund check fails closed.

Both came out of the code review: two concurrent turns used to load the same state and could
each pass the refund limit, and a failed order read used to skip the refund check entirely.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.models import AOPBundle
from baton.llm import LLMResponse, Msg, ToolSpec
from baton.runtime.agent import Agent
from baton.runtime.state import (
    TURN_LEASE,
    ConversationBusyError,
    acquire_turn,
    release_turn,
)
from baton.tools.errors import ToolError
from baton.tools.northwind import NorthwindTools, ToolContext, ToolOutcome
from tests.runtime.support import (
    REPO,
    ScriptedLLM,
    advance,
    call,
    decisions,
    messages_of,
    say,
    traces_of,
)

LIAM = {"customer_id": "cus_0002", "order_number": "NW-10004", "zip": "78701"}


class GatedLLM(ScriptedLLM):
    """Blocks the first agent call until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.gate = threading.Event()

    def complete(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 16000,
    ) -> LLMResponse:
        if not self.entered.is_set():
            self.entered.set()
            assert self.gate.wait(timeout=10)
        return super().complete(system, messages, tools, temperature, max_tokens)


def test_second_message_during_a_turn_is_refused(
    bundle: AOPBundle, tools: NorthwindTools, sessions: sessionmaker[Session]
) -> None:
    llm = GatedLLM()
    agent = Agent(bundle, llm, tools, sessions, prompts_dir=REPO / "prompts", raise_errors=True)
    cid = agent.start_conversation("web")
    llm.route_to = "none"
    llm.push(say("Hi! How can I help?"), say("Sure, what's the order number?"))

    first = threading.Thread(target=agent.handle_turn, args=(cid, "hello"))
    first.start()
    assert llm.entered.wait(timeout=10)  # turn 1 is inside the model call, holding the lease
    with pytest.raises(ConversationBusyError):
        agent.handle_turn(cid, "refund me $90")
    llm.gate.set()
    first.join(timeout=10)

    # The refused message wasn't stored, and the conversation is free again.
    assert [c for r, c in messages_of(sessions, cid) if r == "customer"] == ["hello"]
    assert agent.handle_turn(cid, "I need help with an order").text.startswith("Sure")


def test_lease_expires_and_is_released_on_errors(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    cid = agent.start_conversation("web")
    now = datetime.now(UTC)
    with sessions.begin() as session:
        acquire_turn(session, cid, now)
    with sessions.begin() as session, pytest.raises(ConversationBusyError):
        acquire_turn(session, cid, now + timedelta(seconds=1))
    with sessions.begin() as session:  # a crashed turn's lease runs out
        acquire_turn(session, cid, now + TURN_LEASE + timedelta(seconds=1))
        release_turn(session, cid)

    # An unexpected error mid-turn still frees the conversation.
    llm.route_to = "none"
    with pytest.raises(AssertionError, match="script exhausted"):
        agent.handle_turn(cid, "hello")
    llm.push(say("Hi again!"))
    assert agent.handle_turn(cid, "hello?").text == "Hi again!"


class FlakyOrderReads(NorthwindTools):
    """`get_order` fails with SERVICE_UNAVAILABLE while `down` is set."""

    down = False

    def execute(self, name: str, args: dict[str, object], ctx: ToolContext) -> ToolOutcome:
        if name == "get_order" and self.down:
            return ToolOutcome(name, error=ToolError("SERVICE_UNAVAILABLE"))
        return super().execute(name, args, ctx)


def test_refund_is_blocked_when_the_balance_cant_be_read(
    bundle: AOPBundle,
    llm: ScriptedLLM,
    tools: NorthwindTools,
    sessions: sessionmaker[Session],
    store_http: TestClient,
) -> None:
    flaky = FlakyOrderReads(tools.client)
    agent = Agent(bundle, llm, flaky, sessions, prompts_dir=REPO / "prompts", raise_errors=True)
    cid = agent.start_conversation("web")
    llm.route_to = "refund_request"
    llm.push(
        call(("verify_customer", LIAM)),
        call(advance(2), advance(3), advance(4), advance(5), advance(6)),
    )

    llm.push(
        call(("create_refund", {"order_number": "NW-10004", "amount_cents": 5000, "reason": "x"}))
    )
    flaky.down = True  # only the guardrail's balance read uses get_order in this turn
    llm.push(say("Our order system is slow right now. Could you try again in a few minutes?"))
    reply = agent.handle_turn(cid, "please refund $50 on NW-10004")

    assert reply.status == "open"
    assert store_http.get("/orders/NW-10004").json()["refunded_cents"] == 4000  # unchanged
    (trace,) = traces_of(sessions, cid)
    assert ("refund_unverifiable", "block") in decisions(trace)
    check_read = [t for t in trace["tool_calls"] if t["source"] == "guardrail"]
    assert check_read
    assert check_read[0]["name"] == "get_order"
    assert check_read[0]["error_code"] == "SERVICE_UNAVAILABLE"
