"""Full conversations through the real runtime, real tools and the in-process mock store.

The LLM is scripted, so these prove the runtime's plumbing: routing, step tracking, tool
execution, state updates, traces. Seed fixtures are documented in src/mock_commerce/README.md.
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from baton.runtime.agent import Agent
from tests.runtime.support import (
    ScriptedLLM,
    advance,
    call,
    decisions,
    messages_of,
    say,
    state_of,
    tool_names,
    traces_of,
)

AVA_EMAIL = "ava.thompson@example.com"


def _verify_ava(order: str) -> tuple[str, dict[str, object]]:
    return ("verify_customer", {"customer_id": "cus_0001", "order_number": order, "zip": "98101"})


def test_where_is_my_order(agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]) -> None:
    cid = agent.start_conversation("web")
    llm.route_to = "where_is_my_order"
    llm.push(
        call(("lookup_customer", {"email": AVA_EMAIL})),
        say("Thanks, Ava. What's the shipping ZIP code on order NW-10003?"),
    )
    first = agent.handle_turn(cid, f"Where's my order NW-10003? I'm {AVA_EMAIL}")
    assert first.status == "open"
    assert "ZIP" in first.text

    llm.push(
        call(_verify_ava("NW-10003")),
        call(advance(2), ("get_order", {"order_number": "NW-10003"})),
        call(advance(3)),
        call(advance("RESOLVE")),
        say("NW-10003 hasn't shipped yet. We'll email tracking as soon as it does."),
    )
    second = agent.handle_turn(cid, "98101")
    assert second.status == "resolved"
    assert second.text.startswith("NW-10003 hasn't shipped")

    state = state_of(sessions, cid)
    assert state.verified
    assert state.customer_ref == "cus_0001"
    assert state.customer_name == "Ava"
    assert state.aop_id is None  # resolved
    assert state.slots["order_number"] == "NW-10003"
    assert llm.route_calls == 1  # routed once; turn 2 continued the active procedure

    traces = traces_of(sessions, cid)
    assert len(traces) == 2
    assert tool_names(traces[1]) == [
        "verify_customer",
        "advance_step",
        "get_order",
        "advance_step",
        "advance_step",
    ]
    assert (traces[1]["step_before"], traces[1]["step_after"]) == (1, None)
    assert ("step_transition", "allow") in decisions(traces[1])
    roles = [r for r, _ in messages_of(sessions, cid)]
    assert roles == ["customer", "agent", "customer", "agent"]


def test_cancel_order(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session], store_http: TestClient
) -> None:
    cid = agent.start_conversation("web")
    llm.route_to = "cancel_order"
    llm.push(
        call(("lookup_customer", {"email": AVA_EMAIL}), _verify_ava("NW-10003")),
        call(advance(2), ("get_order", {"order_number": "NW-10003"})),
        call(advance(3)),
        say("NW-10003 is the Brass Reading Lamp. Shall I cancel it?"),
    )
    agent.handle_turn(cid, f"Please cancel NW-10003, I'm {AVA_EMAIL}, zip 98101")
    # The confirmation step: cancel_order isn't even offered to the model yet.
    assert "cancel_order" not in llm.last_tools

    llm.push(
        call(advance(4)),
        call(("cancel_order", {"order_number": "NW-10003"})),
        call(advance("RESOLVE")),
        say("Done: NW-10003 is cancelled and $189.00 has been refunded to your original card."),
    )
    reply = agent.handle_turn(cid, "yes, cancel it")
    assert reply.status == "resolved"
    assert "refunded" in reply.text  # allowed: cancel_order succeeded this turn
    order = store_http.get("/orders/NW-10003").json()
    assert order["status"] == "cancelled"
    assert order["refundable_cents"] == 0


def test_return_within_window(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session], store_http: TestClient
) -> None:
    cid = agent.start_conversation("web")
    llm.route_to = "return_item"
    llm.push(
        call(("lookup_customer", {"email": AVA_EMAIL})),
        say("Happy to help. What's the ZIP code on order NW-10002?"),
    )
    agent.handle_turn(cid, f"I want to return the mugs from NW-10002. {AVA_EMAIL}")
    llm.push(
        call(_verify_ava("NW-10002")),
        call(advance(2), ("get_order", {"order_number": "NW-10002"})),
        call(advance(3), advance(4)),
        say("You're inside the 30-day window. Would you like a refund or an exchange?"),
    )
    agent.handle_turn(cid, "98101")
    llm.push(
        call(advance(5), ("create_return", {"order_number": "NW-10002", "reason": "refund"})),
        call(advance(6), advance("RESOLVE")),
        say(
            "Your return RMA-10002-1 is open and the label is on its way by email. "
            "Your refund is issued once we receive the mugs."
        ),
    )
    reply = agent.handle_turn(cid, "a refund please")
    assert reply.status == "resolved"
    assert "RMA-10002-1" in reply.text
    returns = store_http.get("/orders/NW-10002").json()["returns"]
    assert [r["rma_number"] for r in returns] == ["RMA-10002-1"]
    assert state_of(sessions, cid).slots["rma_number"] == "RMA-10002-1"


def test_subscription_pause(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session], store_http: TestClient
) -> None:
    cid = agent.start_conversation("sms")
    llm.route_to = "manage_subscription"
    ethan = {"customer_id": "cus_0019", "order_number": "NW-10006", "zip": "11201"}
    llm.push(
        call(
            ("lookup_customer", {"email": "ethan.okafor19@example.com"}),
            ("verify_customer", ethan),
            ("get_subscriptions", {"customer_id": "cus_0019"}),
        ),
        call(advance(2), advance(3)),
        call(("pause_subscription", {"subscription_id": "sub_0001"})),
        call(advance("RESOLVE")),
        say("Your soap subscription is paused. Text us whenever you want it back."),
    )
    reply = agent.handle_turn(
        cid, "pause my soap subscription. ethan.okafor19@example.com, order NW-10006, zip 11201"
    )
    assert reply.status == "resolved"
    subs = store_http.get("/subscriptions/cus_0019").json()["subscriptions"]
    assert subs[0]["status"] == "paused"
    assert state_of(sessions, cid).slots["subscription_id"] == "sub_0001"


def test_small_refund_is_issued(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session], store_http: TestClient
) -> None:
    """Under the limit and the balance: the refund runs and the agent may say so."""
    cid = agent.start_conversation("web")
    llm.route_to = "refund_request"
    liam = {"customer_id": "cus_0002", "order_number": "NW-10004", "zip": "78701"}
    llm.push(
        call(("lookup_customer", {"email": "liam.okafor@example.com"}), ("verify_customer", liam)),
        call(advance(2), ("get_order", {"order_number": "NW-10004"})),
        call(advance(3), advance(4), advance(5), advance(6)),
        call(
            (
                "create_refund",
                {"order_number": "NW-10004", "amount_cents": 2500, "reason": "late delivery"},
            )
        ),
        call(advance("RESOLVE")),
        say("I've refunded $25.00 to your card; it takes 5-10 business days to appear."),
    )
    reply = agent.handle_turn(
        cid, "NW-10004 was late, can I get $25 back? liam.okafor@example.com 78701"
    )
    assert reply.status == "resolved"
    assert reply.text.startswith("I've refunded $25.00")
    assert store_http.get("/orders/NW-10004").json()["refunded_cents"] == 6500
    assert state_of(sessions, cid).auto_refunded_cents == 2500
    (trace,) = traces_of(sessions, cid)
    assert ("refund_limit", "allow") in decisions(trace)


def test_topic_switch_reroutes_and_keeps_verification(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    cid = agent.start_conversation("web")
    llm.route_to = "where_is_my_order"
    llm.push(
        call(("lookup_customer", {"email": AVA_EMAIL}), _verify_ava("NW-10002")),
        say("You're verified, Ava. Which order are you asking about?"),
    )
    agent.handle_turn(cid, f"where's my stuff? {AVA_EMAIL}, NW-10002, 98101")

    llm.route_to = "loyalty_points"
    llm.push(
        call(("switch_procedure", {"reason": "customer asks about points"})),
        call(("get_loyalty", {"customer_id": "cus_0001"})),
        say("You have points to spare! Anything else?"),
    )
    agent.handle_turn(cid, "actually, how many loyalty points do I have?")
    state = state_of(sessions, cid)
    assert state.aop_id == "loyalty_points"
    assert state.verified
    trace = traces_of(sessions, cid)[1]
    assert trace["aop_ref"] == "loyalty_points@1"
    assert "get_loyalty" in tool_names(trace)


def test_no_procedure_greets_without_tools(agent: Agent, llm: ScriptedLLM) -> None:
    cid = agent.start_conversation("web")
    llm.route_to = "none"
    llm.push(say("Hi! What can I help you with today?"))
    reply = agent.handle_turn(cid, "hello")
    assert reply.status == "open"
    assert llm.agent_calls[-1][2] is None  # nothing to call without a procedure
