"""The console API (login, inbox, live socket) and the widget's WebSocket protocol."""

import pytest
from starlette.websockets import WebSocketDisconnect

from baton.api.auth import COOKIE, SESSION_TTL_S, operator_from_token, session_token
from baton.knowledge.base import KnowledgeBase
from tests.channels.conftest import CONSOLE_SECRET, OPS, Harness
from tests.knowledge.judge import judge, supported_if
from tests.runtime.support import call, say

# ---------------------------------------------------------------- auth


def test_session_tokens_are_signed_and_expire() -> None:
    token = session_token("s3cret", "sam", now=1_000)
    assert operator_from_token("s3cret", token, now=1_001) == "sam"
    assert operator_from_token("other", token, now=1_001) is None  # secret rotated
    assert operator_from_token("s3cret", token.replace("sam", "eve"), now=1_001) is None
    assert operator_from_token("s3cret", token, now=1_000 + SESSION_TTL_S + 1) is None
    assert operator_from_token("s3cret", "garbage", now=1_001) is None


def test_login_sets_an_httponly_strict_cookie(harness: Harness) -> None:
    c = harness.client
    c.app.state.settings.baton_web_url = "http://localhost:5173"  # type: ignore[attr-defined]
    assert c.get("/ops/me").status_code == 401
    assert c.post("/ops/login", json={"name": "sam", "secret": "wrong"}).status_code == 401
    assert c.post("/ops/login", json={"name": "<b>", "secret": CONSOLE_SECRET}).status_code == 422
    resp = c.post("/ops/login", json={"name": "sam", "secret": CONSOLE_SECRET})
    assert resp.status_code == 204
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert CONSOLE_SECRET not in cookie
    assert c.get("/ops/me").json() == {"name": "sam"}
    c.post("/ops/logout")
    c.cookies.delete(COOKIE)
    assert c.get("/ops/me").status_code == 401


def test_console_is_closed_without_a_secret(harness: Harness) -> None:
    harness.client.app.state.settings.baton_console_secret = None  # type: ignore[attr-defined]
    assert harness.client.get("/ops/conversations", headers=OPS).status_code == 503
    resp = harness.client.post("/ops/login", json={"name": "sam", "secret": "anything"})
    assert resp.status_code == 503


# ---------------------------------------------------------------- inbox


def test_inbox_lists_and_filters(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say("Hi web!"), say("Hi sms!"))
    web = h.client.post("/conversations", json={}).json()["id"]
    h.client.post(f"/conversations/{web}/messages", json={"text": "hello"})
    h.send_sms("hi there")

    items = h.client.get("/ops/conversations", headers=OPS).json()
    assert [i["channel"] for i in items] == ["sms", "web"]  # most recent first
    sms = items[0]
    assert sms["contact"] == "+12065550101"
    assert sms["last_role"] == "agent"
    assert sms["last_message"] == "Hi sms!"
    assert sms["message_count"] == 2
    only_web = h.client.get("/ops/conversations?channel=web", headers=OPS).json()
    assert [i["id"] for i in only_web] == [web]
    assert h.client.get("/ops/conversations?status=escalated", headers=OPS).json() == []
    assert h.client.get("/ops/conversations?status=bogus", headers=OPS).status_code == 422


def test_inbox_socket_needs_a_session(harness: Harness) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        harness.client.websocket_connect("/ws/ops") as ws,
    ):
        ws.receive_json()
    assert closed.value.code == 4401


def test_inbox_socket_pushes_new_conversations_and_messages(harness: Harness) -> None:
    h = harness
    h.client.app.state.settings.baton_web_url = "http://localhost:5173"  # type: ignore[attr-defined]
    h.client.post("/ops/login", json={"name": "sam", "secret": CONSOLE_SECRET})
    h.llm.route_to = "none"
    h.llm.push(say("Hello!"))
    with h.client.websocket_connect("/ws/ops") as ws:
        cid = h.client.post("/conversations", json={}).json()["id"]
        assert ws.receive_json() == {"type": "updated", "conversation_id": cid, "reason": "created"}
        h.client.post(f"/conversations/{cid}/messages", json={"text": "hi"})
        # The customer's message is announced before the agent has answered it.
        assert ws.receive_json() == {"type": "updated", "conversation_id": cid, "reason": "message"}
        rest = [ws.receive_json() for _ in range(2)]
        assert {"type": "activity", "conversation_id": cid, "activity": "routing"} in rest


# ---------------------------------------------------------------- widget socket


def test_unknown_conversation_closes_the_socket(harness: Harness) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        harness.client.websocket_connect("/ws/chat/nope") as ws,
    ):
        ws.receive_json()
    assert closed.value.code == 4404


def test_invalid_frames_get_an_error_not_a_disconnect(harness: Harness) -> None:
    cid = harness.client.post("/conversations", json={}).json()["id"]
    with harness.client.websocket_connect(f"/ws/chat/{cid}") as ws:
        ws.send_json({"type": "dance"})
        assert ws.receive_json()["code"] == "invalid"
        ws.send_json({"type": "message", "text": "   "})
        assert ws.receive_json()["code"] == "invalid"
        ws.send_json({"type": "message", "text": "x" * 4001})
        assert ws.receive_json()["code"] == "invalid"


def test_talk_to_a_human_button_escalates_with_a_handoff(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "none"
    cid = h.client.post("/conversations", json={}).json()["id"]
    with h.client.websocket_connect(f"/ws/chat/{cid}") as ws:
        ws.send_json({"type": "request_human"})
        events = []
        while not events or events[-1]["type"] != "status":
            events.append(ws.receive_json())
    assert [e["type"] for e in events if e["type"] != "typing"] == ["message", "status"]
    assert events[-1]["status"] == "escalated"
    detail = h.client.get(f"/conversations/{cid}", headers=OPS).json()
    assert detail["state"]["handoff"]["generated_by"] == "llm"
    assert detail["state"]["escalation_reason"] == "customer asked for a human"


def test_typing_shows_which_tool_is_running(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "where_is_my_order"
    h.llm.push(call(("lookup_customer", {"email": "ava.thompson@example.com"})), say("Got it."))
    cid = h.client.post("/conversations", json={}).json()["id"]
    with h.client.websocket_connect(f"/ws/chat/{cid}") as ws:
        ws.send_json({"type": "message", "text": "where's my order? ava.thompson@example.com"})
        typing = []
        while (event := ws.receive_json())["type"] == "typing":
            typing.append(event["activity"])
    assert typing == ["routing", "thinking", "tool:lookup_customer", "thinking"]
    assert h.llm.streamed == 2  # with a listener the agent streams its model calls


def test_widget_message_carries_citation_links(harness: Harness, knowledge: KnowledgeBase) -> None:
    h = harness
    agent = h.client.app.state.get_agent()  # type: ignore[attr-defined]
    agent.tools.knowledge = agent.knowledge = knowledge
    h.llm.route_to = "general_question"
    h.llm.grounding = judge(supported_if(**{"6-10 business days": "kb:shipping-international"}))
    h.llm.push(
        call(("search_knowledge", {"query": "Canada shipping time"})),
        say("Shipping to Canada takes 6-10 business days [kb:shipping-international]."),
    )
    cid = h.client.post("/conversations", json={}).json()["id"]
    with h.client.websocket_connect(f"/ws/chat/{cid}") as ws:
        ws.send_json({"type": "message", "text": "How long does shipping to Canada take?"})
        while (event := ws.receive_json())["type"] != "message":
            pass
    url = "https://help.northwind.example/articles/shipping-international"
    body = event["message"]["body"]
    assert f"6-10 business days [1]({url})." in body
    assert f"1. [International orders and shipping to Canada]({url})" in body
    assert event["message"]["citations"][0]["url"] == url
    # The stored transcript re-renders the same links later (citations are kept on the message).
    transcript = h.client.get(f"/conversations/{cid}/messages").json()
    assert transcript["messages"][-1]["body"] == body
