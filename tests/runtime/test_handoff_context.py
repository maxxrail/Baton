"""The handoff note is written from the whole conversation and the verified state.

Found in the Part 04 browser check: a customer verified in turn 1 asked for a human in turn 2.
The escalation turn calls no tools, so the note writer saw an empty tool log and no state, and
told the teammate the customer was "unverified" and nothing had been checked.
"""

from sqlalchemy.orm import Session, sessionmaker

from baton.runtime.agent import Agent
from tests.runtime.support import ScriptedLLM, advance, call, say, state_of

AVA = {"customer_id": "cus_0001", "order_number": "NW-10002", "zip": "98101"}


def test_handoff_writer_sees_verified_identity_and_earlier_tool_calls(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    cid = agent.start_conversation("web")
    llm.route_to = "where_is_my_order"
    llm.push(
        call(("lookup_customer", {"email": "ava.thompson@example.com"}), ("verify_customer", AVA)),
        call(advance(2), ("get_order", {"order_number": "NW-10002"})),
        say("NW-10002 was delivered 10 days ago."),
    )
    agent.handle_turn(cid, "where is NW-10002? ava.thompson@example.com, zip 98101")
    agent.handle_turn(cid, "I'd like to talk to a person, please.")

    assert state_of(sessions, cid).status == "escalated"
    (prompt,) = llm.handoff_prompts
    state = prompt.split("<verified_state>")[1].split("</verified_state>")[0]
    assert "verified as customer cus_0001 (Ava)" in state
    assert "NW-10002" in state
    tool_log = prompt.split("<tool_log>")[1].split("</tool_log>")[0]
    assert "turn 1: lookup_customer: ok" in tool_log
    assert "turn 1: verify_customer: ok" in tool_log
    assert "turn 1: get_order: ok" in tool_log
    assert "advance_step" not in tool_log
    assert "98101" not in prompt  # the zip never reaches the note writer


def test_model_is_told_nothing_was_sent_when_tool_responses_carry_no_text(
    agent: Agent, llm: ScriptedLLM
) -> None:
    """Found in the browser check: Sonnet 5.5 returned the answer written next to
    `advance_step(RESOLVE)` as a hidden progress block, then wrote only "Anything else about
    your points?". The runtime now tells the model the customer has seen nothing yet."""
    from baton.runtime.agent import DRAFT_KEPT_NOTE, NOTHING_SENT_NOTE

    cid = agent.start_conversation("web")
    llm.route_to = "where_is_my_order"
    llm.push(
        call(("lookup_customer", {"email": "ava.thompson@example.com"}), ("verify_customer", AVA)),
        say("Thanks! It was delivered 10 days ago."),
    )
    agent.handle_turn(cid, "where is NW-10002? ava.thompson@example.com 98101")
    _, messages, _ = llm.agent_calls[-1]
    notes = [b.text for b in messages[-1].content if getattr(b, "type", "") == "text"]  # type: ignore[union-attr]
    assert notes == [NOTHING_SENT_NOTE]
    assert DRAFT_KEPT_NOTE not in notes
