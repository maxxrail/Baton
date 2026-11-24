"""Helpers for agent tests: a scripted LLM, response builders and DB readers.

`ScriptedLLM` plays three roles, picked by which tool the caller offers:
- the router (offers `choose_procedure`): answers with `route_to`;
- the handoff writer (offers `write_handoff`): answers with `handoff` (None -> no tool call,
  which exercises the deterministic fallback);
- the agent: pops the next scripted response, failing loudly if the script runs out.
"""

from collections import deque
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.router import ROUTE_TOOL
from baton.db import Message, Trace
from baton.llm import LLMResponse, Msg, ToolCall, ToolSpec, Usage
from baton.runtime.escalation import HANDOFF_TOOL
from baton.runtime.state import ConversationState, load_state

REPO = Path(__file__).resolve().parents[2]
USAGE = Usage(input_tokens=120, output_tokens=30)


def say(text: str) -> LLMResponse:
    return LLMResponse(text=text, usage=USAGE, stop_reason="end_turn", model="fake")


def call(*calls: tuple[str, dict[str, object]]) -> LLMResponse:
    """One model response containing one or more tool calls, in order."""
    return LLMResponse(
        tool_calls=[
            ToolCall(id=f"tc_{i}_{name}", name=name, input=args)
            for i, (name, args) in enumerate(calls)
        ],
        usage=USAGE,
        stop_reason="tool_use",
        model="fake",
    )


def advance(target: str | int, reason: str = "step done") -> tuple[str, dict[str, object]]:
    return ("advance_step", {"target": str(target), "reason": reason})


class ScriptedLLM:
    def __init__(self) -> None:
        self.route_to: str = "none"
        self.handoff: dict[str, object] | None = {
            "customer": "Ava Thompson (cus_0001)",
            "intent": "Wants a large refund",
            "tried": ["looked up the order"],
            "needed": "Approve or decline the refund",
        }
        self.script: deque[LLMResponse] = deque()
        self.agent_calls: list[tuple[str, list[Msg], list[ToolSpec] | None]] = []
        self.route_calls = 0
        self.handoff_calls = 0

    def push(self, *responses: LLMResponse) -> None:
        self.script.extend(responses)

    def complete(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 16000,
    ) -> LLMResponse:
        names = {t.name for t in tools or []}
        if ROUTE_TOOL in names:
            self.route_calls += 1
            args = {"aop_id": self.route_to, "confidence": 0.9, "rationale": "scripted"}
            return LLMResponse(
                tool_calls=[ToolCall(id="route", name=ROUTE_TOOL, input=args)], usage=USAGE
            )
        if HANDOFF_TOOL in names:
            self.handoff_calls += 1
            if self.handoff is None:
                return say("I'd rather not.")
            return LLMResponse(
                tool_calls=[ToolCall(id="handoff", name=HANDOFF_TOOL, input=self.handoff)],
                usage=USAGE,
            )
        self.agent_calls.append((system, list(messages), tools))
        if not self.script:
            raise AssertionError(f"agent script exhausted after {len(self.agent_calls)} calls")
        return self.script.popleft()

    @property
    def last_tools(self) -> set[str]:
        return {t.name for t in self.agent_calls[-1][2] or []}


def state_of(sessions: sessionmaker[Session], conversation_id: str) -> ConversationState:
    with sessions() as session:
        return load_state(session, conversation_id)


def traces_of(sessions: sessionmaker[Session], conversation_id: str) -> list[dict[str, object]]:
    with sessions() as session:
        rows = session.scalars(select(Trace).where(Trace.conversation_id == conversation_id))
        return sorted((r.payload for r in rows), key=lambda p: int(str(p["turn"])))


def messages_of(sessions: sessionmaker[Session], conversation_id: str) -> list[tuple[str, str]]:
    with sessions() as session:
        rows = session.execute(
            select(Message.role, Message.content)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id)
        ).all()
        return [(r, c) for r, c in rows]


def decisions(trace: dict[str, object]) -> list[tuple[str, str]]:
    """(guardrail, action) pairs from a stored trace."""
    items = trace["guardrails"]
    assert isinstance(items, list)
    return [(d["guardrail"], d["action"]) for d in items]


def tool_names(trace: dict[str, object]) -> list[str]:
    items = trace["tool_calls"]
    assert isinstance(items, list)
    return [t["name"] for t in items]
