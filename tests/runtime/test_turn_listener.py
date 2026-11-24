"""Turn listeners: progress events while a turn runs, and streaming only when listened to."""

from sqlalchemy.orm import Session, sessionmaker

from baton.runtime.agent import Agent, TurnEvent
from tests.runtime.support import ScriptedLLM, call, say


def test_listener_sees_the_turn_and_a_broken_listener_breaks_nothing(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    seen: list[TurnEvent] = []

    def listener(event: TurnEvent) -> None:
        seen.append(event)
        raise RuntimeError("socket closed")

    cid = agent.start_conversation("web")
    llm.route_to = "where_is_my_order"
    llm.push(call(("lookup_customer", {"email": "ava.thompson@example.com"})), say("Thanks!"))
    reply = agent.handle_turn(cid, "where is my order? ava.thompson@example.com", listener=listener)
    assert reply.text == "Thanks!"
    assert [(e.kind, e.detail) for e in seen][1:] == [
        ("activity", "routing"),
        ("activity", "thinking"),
        ("activity", "tool:lookup_customer"),
        ("activity", "thinking"),
    ]
    assert seen[0].kind == "received"
    assert seen[0].detail.isdigit()


def test_without_a_listener_the_agent_does_not_stream(agent: Agent, llm: ScriptedLLM) -> None:
    cid = agent.start_conversation("web")
    llm.push(say("Hi!"))
    agent.handle_turn(cid, "hello")
    assert llm.streamed == 0
