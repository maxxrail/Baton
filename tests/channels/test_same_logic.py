"""Acceptance: the conversation logic doesn't depend on the channel.

The same scripted model run, sent once over the web widget's WebSocket, once as an inbound
email and once as an inbound SMS, makes exactly the same tool calls (names and arguments) and
the same guardrail decisions. Only the delivery differs.
"""

from sqlalchemy import select

from baton.db import Conversation
from tests.channels.conftest import AVA, AVA_EMAIL, AVA_PHONE, Harness
from tests.runtime.support import advance, call, say, traces_of

QUESTION = f"Where is NW-10002? I'm {AVA_EMAIL}, zip 98101"
ANSWER = "Your order NW-10002 was delivered 10 days ago."


def _script(h: Harness) -> None:
    h.llm.route_to = "where_is_my_order"
    h.llm.push(
        call(("lookup_customer", {"email": AVA_EMAIL}), ("verify_customer", AVA)),
        call(advance(2), ("get_order", {"order_number": "NW-10002"})),
        say(ANSWER),
    )


def _fingerprint(h: Harness, cid: str) -> list[object]:
    (trace,) = traces_of(h.sessions, cid)
    tools = trace["tool_calls"]
    guardrails = trace["guardrails"]
    assert isinstance(tools, list)
    assert isinstance(guardrails, list)
    return [
        [(t["name"], t["args"], t["ok"]) for t in tools],
        [(g["guardrail"], g["action"]) for g in guardrails],
        trace["aop_ref"],
        trace["step_after"],
    ]


def _only(h: Harness, channel: str) -> str:
    with h.sessions() as session:
        ids = session.scalars(select(Conversation.id).where(Conversation.channel == channel)).all()
    assert len(ids) == 1
    return ids[0]


def test_web_email_and_sms_make_identical_tool_calls(harness: Harness) -> None:
    h = harness

    # Web: the widget's WebSocket.
    _script(h)
    web = h.client.post("/conversations", json={"channel": "web"}).json()["id"]
    with h.client.websocket_connect(f"/ws/chat/{web}") as ws:
        ws.send_json({"type": "message", "text": QUESTION})
        while (event := ws.receive_json())["type"] != "message":
            assert event["type"] == "typing"
    assert event["message"]["role"] == "agent"
    assert event["message"]["body"] == ANSWER

    # Email: the inbound webhook, with a quoted thread under the new text.
    _script(h)
    quoted = f"{QUESTION}\n\nOn Mon, Ava wrote:\n> an older message\n"
    assert h.send_email(text=quoted).status_code == 202
    email = _only(h, "email")

    # SMS: a signed Twilio webhook.
    _script(h)
    assert h.send_sms(QUESTION).status_code == 200
    sms = _only(h, "sms")

    web_fp, email_fp, sms_fp = (_fingerprint(h, c) for c in (web, email, sms))
    assert web_fp == email_fp == sms_fp
    assert [name for name, _, _ in web_fp[0]] == [  # type: ignore[union-attr]
        "lookup_customer",
        "verify_customer",
        "advance_step",
        "get_order",
    ]

    # Each reply went out through its own channel, formatted for it.
    (mail,) = h.email.sent
    assert mail.to == AVA_EMAIL
    assert mail.body.startswith(ANSWER)
    assert "Northwind Goods Support" in mail.body
    assert h.sms.sent == [(AVA_PHONE, ANSWER)]
