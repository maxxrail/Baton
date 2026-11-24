"""Human handoff: once escalated, the agent is silent; a human replies through the customer's own
channel; handing back (resolve / return to agent) brings the agent back."""

import pytest
from sqlalchemy import select

from baton.db import Message
from tests.channels.conftest import AVA_EMAIL, AVA_PHONE, OPS, Harness
from tests.runtime.support import say, state_of, traces_of

ASK_HUMAN = "let me speak to a person"


def _roles(h: Harness, cid: str) -> list[str]:
    with h.sessions() as session:
        return list(
            session.scalars(
                select(Message.role).where(Message.conversation_id == cid).order_by(Message.id)
            )
        )


def _escalated_web(h: Harness) -> str:
    h.llm.route_to = "where_is_my_order"
    cid = h.client.post("/conversations", json={"channel": "web"}).json()["id"]
    reply = h.client.post(f"/conversations/{cid}/messages", json={"text": ASK_HUMAN}).json()
    assert reply["status"] == "escalated"
    return cid


def test_escalated_conversation_gets_no_agent_reply(harness: Harness) -> None:
    h = harness
    cid = _escalated_web(h)
    calls = len(h.llm.agent_calls) + h.llm.route_calls

    reply = h.client.post(f"/conversations/{cid}/messages", json={"text": "hello? anyone?"}).json()
    assert reply["replied"] is False
    assert reply["text"] == reply["body"] == ""
    assert len(h.llm.agent_calls) + h.llm.route_calls == calls  # no model call at all
    # The message is stored (and traced) for the human, but nothing was said back.
    assert _roles(h, cid)[-1] == "customer"
    assert traces_of(h.sessions, cid)[-1]["reply_source"] == "human_owned"


def test_widget_sees_no_reply_while_human_owned_then_the_humans_reply(harness: Harness) -> None:
    h = harness
    cid = _escalated_web(h)
    with h.client.websocket_connect(f"/ws/chat/{cid}") as ws:
        ws.send_json({"type": "message", "text": "still there?"})
        assert ws.receive_json() == {"type": "typing", "activity": ""}  # no reply is coming
        assert ws.receive_json()["status"] == "escalated"

        resp = h.client.post(
            f"/ops/conversations/{cid}/reply",
            json={"text": "Hi, I'm Sam. Looking now."},
            headers=OPS,
        )
        assert resp.json() == {"delivered": True, "error": None}
        event = ws.receive_json()
        assert event["type"] == "message"
        assert event["message"]["role"] == "human_agent"
        assert event["message"]["body"] == "Hi, I'm Sam. Looking now."


def test_human_reply_is_delivered_by_email_in_the_same_thread(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "where_is_my_order"
    h.send_email(text=ASK_HUMAN, message_id="<help@mail.example>", authenticated=True)
    h.send_email(
        text="any update?",
        subject="Re: Where is my order?",
        message_id="<p2@mail.example>",
        authenticated=True,
    )
    assert len(h.email.sent) == 1  # the escalation notice; the follow-up got no agent reply
    cid = h.client.get("/ops/conversations", headers=OPS).json()[0]["id"]

    resp = h.client.post(f"/ops/conversations/{cid}/reply", json={"text": "Sorted!"}, headers=OPS)
    assert resp.json()["delivered"] is True
    mail = h.email.sent[-1]
    assert mail.to == AVA_EMAIL
    assert mail.in_reply_to == "<p2@mail.example>"  # answers the latest customer email
    assert mail.body.startswith("Sorted!")
    assert "api, Northwind Goods Support" in mail.body  # signed by the operator
    assert _roles(h, cid)[-1] == "human_agent"


def test_human_reply_is_delivered_by_sms(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "where_is_my_order"
    h.send_sms(ASK_HUMAN)
    cid = h.client.get("/ops/conversations", headers=OPS).json()[0]["id"]
    h.client.post(f"/ops/conversations/{cid}/reply", json={"text": "On it."}, headers=OPS)
    assert h.sms.sent[-1] == (AVA_PHONE, "On it.")


def test_return_to_agent_brings_the_agent_back(harness: Harness) -> None:
    h = harness
    cid = _escalated_web(h)
    resp = h.client.post(
        f"/ops/conversations/{cid}/handback", json={"action": "return_to_agent"}, headers=OPS
    )
    assert resp.json()["status"] == "open"
    state = state_of(h.sessions, cid)
    assert state.handoff is None
    assert state.aop_id is None
    assert state.assignee is None

    h.llm.route_to = "none"
    h.llm.push(say("I'm back. What else can I do?"))
    reply = h.client.post(f"/conversations/{cid}/messages", json={"text": "thanks"}).json()
    assert reply["replied"] is True
    assert reply["text"] == "I'm back. What else can I do?"
    assert _roles(h, cid).count("system") >= 2  # handoff note + "handed back" note


def test_resolve_closes_and_a_new_message_reopens_with_the_agent(harness: Harness) -> None:
    h = harness
    cid = _escalated_web(h)
    resp = h.client.post(
        f"/ops/conversations/{cid}/handback", json={"action": "resolve"}, headers=OPS
    )
    assert resp.json()["status"] == "resolved"
    h.llm.route_to = "none"
    h.llm.push(say("Welcome back!"))
    reply = h.client.post(f"/conversations/{cid}/messages", json={"text": "one more q"}).json()
    assert (reply["status"], reply["replied"]) == ("open", True)


def test_handback_keeps_identity_and_failed_attempts(harness: Harness) -> None:
    """Handing back must not reset the verification cap: three wrong zips, a human, then
    the agent again still has three strikes on record."""
    h = harness
    cid = _escalated_web(h)
    with h.sessions() as session:
        from baton.runtime.state import load_state, save_state

        state = load_state(session, cid)
        state.failed_verifications = ["a", "b", "c"]
        save_state(session, state)
        session.commit()
    h.client.post(
        f"/ops/conversations/{cid}/handback", json={"action": "return_to_agent"}, headers=OPS
    )
    assert len(state_of(h.sessions, cid).failed_verifications) == 3


def test_operator_can_take_over_an_open_conversation(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say("Hi!"))
    cid = h.client.post("/conversations", json={}).json()["id"]
    h.client.post(f"/conversations/{cid}/messages", json={"text": "hi"})

    # Replying before taking over is refused: the agent still owns it.
    resp = h.client.post(f"/ops/conversations/{cid}/reply", json={"text": "hey"}, headers=OPS)
    assert resp.status_code == 409

    resp = h.client.post(f"/ops/conversations/{cid}/takeover", headers=OPS)
    assert resp.json()["status"] == "escalated"
    state = state_of(h.sessions, cid)
    assert (state.assignee, state.escalation_reason) == ("api", "taken over by api")
    reply = h.client.post(f"/conversations/{cid}/messages", json={"text": "hello?"}).json()
    assert reply["replied"] is False


def test_operator_actions_wait_for_a_running_turn(harness: Harness) -> None:
    from baton.runtime.state import acquire_turn

    cid = _escalated_web(harness)
    with harness.sessions() as session:
        acquire_turn(session, cid)  # simulate an agent turn in flight
        session.commit()
    for path, body in [
        ("takeover", None),
        ("reply", {"text": "hi"}),
        ("handback", {"action": "resolve"}),
    ]:
        resp = harness.client.post(f"/ops/conversations/{cid}/{path}", json=body, headers=OPS)
        assert resp.status_code == 409, path


@pytest.mark.parametrize("path", ["takeover", "reply", "handback"])
def test_operator_actions_need_auth(harness: Harness, path: str) -> None:
    cid = _escalated_web(harness)
    assert harness.client.post(f"/ops/conversations/{cid}/{path}", json={}).status_code == 401


def test_unknown_conversation_is_404(harness: Harness) -> None:
    resp = harness.client.post("/ops/conversations/nope/takeover", headers=OPS)
    assert resp.status_code == 404


def test_reply_to_an_opted_out_sms_customer_reports_not_delivered(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "where_is_my_order"
    h.send_sms(ASK_HUMAN)
    h.send_sms("STOP")
    cid = h.client.get("/ops/conversations", headers=OPS).json()[0]["id"]
    resp = h.client.post(f"/ops/conversations/{cid}/reply", json={"text": "Hi"}, headers=OPS)
    assert resp.json()["delivered"] is False
    assert "opted out" in resp.json()["error"]
