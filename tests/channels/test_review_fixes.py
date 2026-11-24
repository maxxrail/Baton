"""Regression tests for the Part 04 code review: lease ownership, capability leaks, Origin
checks, and sockets that must survive bad input."""

import pytest
from starlette.websockets import WebSocketDisconnect

from baton.api.auth import operator_from_token
from baton.channels.links import LINK_TTL_S, transcript_token, valid_transcript_token
from baton.db import Message
from baton.llm import LLMResponse, Msg, ToolSpec
from baton.runtime import handoff
from baton.runtime.state import LeaseLostError
from tests.channels.conftest import CONSOLE_SECRET, LINK_SECRET, OPS, Harness
from tests.runtime.support import messages_of, say, state_of

# ---------------------------------------------------------------- lease ownership


def test_a_turn_that_outlived_its_lease_cannot_overwrite_a_takeover(harness: Harness) -> None:
    """The agent's turn runs past its lease; meanwhile an operator takes over. The stale turn
    must not save its state (status back to "open") or send its reply."""
    h = harness
    h.llm.route_to = "none"
    cid = h.client.post("/conversations", json={}).json()["id"]
    original = h.llm.complete

    def slow_model(
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 16000,
    ) -> LLMResponse:
        # The lease expires mid-call and an operator claims the conversation.
        from sqlalchemy import update

        from baton.db import Conversation
        from baton.db.models import utcnow

        with h.sessions() as session:
            session.execute(update(Conversation).values(turn_lease_until=utcnow()))
            session.commit()
        handoff.take_over(h.sessions, cid, "sam")
        return original(system, messages, tools, temperature, max_tokens)

    h.llm.complete = slow_model  # type: ignore[method-assign]
    h.llm.push(say("All sorted, nothing more to do!"))
    service = h.client.app.state.get_service()  # type: ignore[attr-defined]
    with pytest.raises(LeaseLostError):
        service.customer_message(cid, "hello")

    state = state_of(h.sessions, cid)
    assert state.status == "escalated"
    assert state.assignee == "sam"
    assert "All sorted" not in [c for _, c in messages_of(h.sessions, cid)]
    # The operator's lease was not released by the stale turn either.
    resp = h.client.post(f"/ops/conversations/{cid}/reply", json={"text": "Hi"}, headers=OPS)
    assert resp.status_code == 200


# ---------------------------------------------------------------- capability leaks


def test_http_messages_only_reach_web_conversations(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say("Hi!"))
    h.send_sms("hello")
    cid = h.client.get("/ops/conversations", headers=OPS).json()[0]["id"]
    resp = h.client.post(f"/conversations/{cid}/messages", json={"text": "my order details?"})
    assert resp.status_code == 404
    with h.sessions() as session:
        assert session.query(Message).filter_by(conversation_id=cid).count() == 2


def test_sms_transcripts_need_a_signed_unexpired_link(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say("Hi!"))
    h.send_sms("hello")
    cid = h.client.get("/ops/conversations", headers=OPS).json()[0]["id"]
    url = f"/conversations/{cid}/messages"
    assert h.client.get(url).status_code == 404
    assert h.client.get(url, params={"t": "123.forged"}).status_code == 404
    other = transcript_token(LINK_SECRET, "someone-else")
    assert h.client.get(url, params={"t": other}).status_code == 404
    good = transcript_token(LINK_SECRET, cid)
    assert h.client.get(url, params={"t": good}).status_code == 200


def test_transcript_tokens_expire_and_bind_to_the_conversation() -> None:
    token = transcript_token("s", "c1", now=1_000)
    assert valid_transcript_token("s", "c1", token, now=1_000 + LINK_TTL_S)
    assert not valid_transcript_token("s", "c1", token, now=1_000 + LINK_TTL_S + 1)
    assert not valid_transcript_token("s", "c2", token, now=1_001)
    assert not valid_transcript_token("other", "c1", token, now=1_001)
    assert not valid_transcript_token("", "c1", token, now=1_001)
    assert not valid_transcript_token("s", "c1", "1000.éé", now=1_001)


def test_long_sms_replies_carry_a_signed_link(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say(" ".join(f"word{i}" for i in range(150))))
    h.send_sms("tell me everything")
    (_, body) = h.sms.sent[-1]
    link = body.rsplit("Full answer: ", 1)[1]
    assert link.startswith("https://chat.northwind.example/widget?c=")
    query = link.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in query.split("&"))
    assert h.client.get(f"/conversations/{params['c']}/messages", params={"t": params["t"]}).json()


# ---------------------------------------------------------------- Origin checks


def _login(h: Harness) -> None:
    h.client.app.state.settings.baton_web_url = "http://localhost:5173"  # type: ignore[attr-defined]
    assert (
        h.client.post("/ops/login", json={"name": "sam", "secret": CONSOLE_SECRET}).status_code
        == 204
    )


def test_cookie_writes_from_another_origin_are_refused(harness: Harness) -> None:
    h = harness
    _login(h)
    cid = h.client.post("/conversations", json={}).json()["id"]
    evil = {"Origin": "http://localhost:6666"}
    resp = h.client.post(f"/ops/conversations/{cid}/takeover", headers=evil)
    assert resp.status_code == 403
    # Reads are fine, and so are the web app's own origin and non-browser clients.
    assert h.client.get("/ops/conversations", headers=evil).status_code == 200
    ok = {"Origin": "http://localhost:5173"}
    assert h.client.post(f"/ops/conversations/{cid}/takeover", headers=ok).status_code == 200
    # Bearer callers aren't browsers riding a cookie: no Origin rule applies.
    resp = h.client.post(
        f"/ops/conversations/{cid}/handback", json={"action": "resolve"}, headers={**OPS, **evil}
    )
    assert resp.status_code == 200


def test_inbox_socket_refuses_other_origins_and_forged_cookies(harness: Harness) -> None:
    h = harness
    _login(h)
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        h.client.websocket_connect("/ws/ops", headers={"Origin": "http://evil.example"}) as ws,
    ):
        ws.receive_json()
    assert closed.value.code == 4401

    h.client.cookies.clear()
    h.client.cookies.set("baton_console", "sam|1|" + "0" * 64)
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        h.client.websocket_connect("/ws/ops") as ws,
    ):
        ws.receive_json()
    assert closed.value.code == 4401


def test_a_non_ascii_cookie_is_rejected_not_a_crash() -> None:
    # Browsers can send latin-1 cookie bytes; the str compare used to raise TypeError (a 500).
    assert operator_from_token("s3cret", "sam|1|" + chr(233) * 3) is None


def test_the_session_cookie_is_secure_when_the_console_is_served_over_https(
    harness: Harness,
) -> None:
    # The harness web app URL is https (as behind a TLS-terminating proxy).
    resp = harness.client.post("/ops/login", json={"name": "sam", "secret": CONSOLE_SECRET})
    assert "Secure" in resp.headers["set-cookie"]


# ---------------------------------------------------------------- sockets survive bad input


def test_chat_socket_survives_non_json_frames_and_turn_failures(harness: Harness) -> None:
    h = harness
    cid = h.client.post("/conversations", json={}).json()["id"]
    service = h.client.app.state.get_service()  # type: ignore[attr-defined]

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database is gone")

    with h.client.websocket_connect(f"/ws/chat/{cid}") as ws:
        ws.send_text("not json")
        assert ws.receive_json()["code"] == "invalid"
        service.customer_message = broken
        ws.send_json({"type": "message", "text": "hi"})
        assert ws.receive_json()["code"] == "failed"
        ws.send_json({"type": "dance"})  # still connected
        assert ws.receive_json()["code"] == "invalid"


def test_a_bad_channel_config_is_a_503_not_a_500(tmp_path: object) -> None:
    from fastapi.testclient import TestClient

    from baton.aop.compiler import compile_aops
    from baton.api.app import create_app
    from tests.channels.conftest import REPO, settings

    bundle = compile_aops(REPO / "aops", prompts_dir=REPO / "prompts")
    app = create_app(settings(email_provider="resend"), bundle)
    resp = TestClient(app).post(
        "/channels/email/inbound",
        json={"from": "a@b.example", "text": "hi", "message_id": "<x@y>"},
        headers={"x-baton-webhook-secret": "email-test-secret"},
    )
    assert resp.status_code == 503
    assert "RESEND_API_KEY" in resp.json()["detail"]
