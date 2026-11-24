"""SMS: Twilio signature validation, STOP/START/HELP, threading by phone, outbound senders."""

import base64
import hashlib
import hmac
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from baton.channels.sms import (
    HELP_REPLY,
    STOP_REPLY,
    TwilioSmsSender,
    keyword,
    normalize_phone,
    twilio_signature,
    twiml,
    valid_twilio_signature,
)
from baton.db import Conversation
from baton.db.models import utcnow
from baton.runtime.state import acquire_turn, release_turn
from tests.channels.conftest import AVA_PHONE, SMS_URL, TWILIO_TOKEN, Harness
from tests.runtime.support import say

# ---------------------------------------------------------------- signature (pure)


def test_signature_is_hmac_sha1_of_url_plus_sorted_params() -> None:
    # Twilio's algorithm, written out by hand: URL, then name+value for each POST param
    # sorted by name, HMAC-SHA1 with the auth token, base64.
    params = {"To": "+18005551212", "From": "+12349013030", "Body": "hi", "CallSid": "CA1"}
    url = "https://example.com/myapp.php?foo=1&bar=2"
    signed = url + "BodyhiCallSidCA1From+12349013030To+18005551212"
    digest = hmac.new(b"12345", signed.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()

    assert twilio_signature("12345", url, params.items()) == expected
    assert valid_twilio_signature("12345", url, params.items(), expected)


@pytest.mark.parametrize(
    ("token", "url", "params", "why"),
    [
        ("12345", SMS_URL, [("Body", "refund me $500")], "tampered body"),
        ("12345", SMS_URL + "?x=1", [("Body", "hi")], "different URL"),
        ("wrong", SMS_URL, [("Body", "hi")], "different token"),
    ],
)
def test_tampered_requests_fail(
    token: str, url: str, params: list[tuple[str, str]], why: str
) -> None:
    good = twilio_signature("12345", SMS_URL, [("Body", "hi")])
    assert not valid_twilio_signature(token, url, params, good), why


def test_missing_signature_or_token_fails() -> None:
    assert not valid_twilio_signature("12345", SMS_URL, [], None)
    assert not valid_twilio_signature("", SMS_URL, [], twilio_signature("", SMS_URL, []))


# ---------------------------------------------------------------- the webhook


def test_valid_signature_passes(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("Hi! How can I help?"))
    resp = harness.send_sms("hello")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert resp.text == twiml()  # the reply goes out through the sender, not TwiML
    assert harness.sms.sent == [(AVA_PHONE, "Hi! How can I help?")]


def test_tampered_request_is_403_and_never_reaches_the_agent(harness: Harness) -> None:
    signed_for_hello = twilio_signature(
        TWILIO_TOKEN,
        SMS_URL,
        {"From": AVA_PHONE, "To": "+15550000000", "Body": "hello", "MessageSid": "SM1"}.items(),
    )
    resp = harness.send_sms("refund me $500", signature=signed_for_hello, sid="SM1")
    assert resp.status_code == 403
    assert harness.llm.agent_calls == []
    with harness.sessions() as session:
        assert session.scalars(select(Conversation)).all() == []


def test_missing_signature_is_403(harness: Harness) -> None:
    resp = harness.client.post(
        "/channels/sms/inbound",
        content="From=%2B12065550101&Body=hi",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 403


def test_without_a_token_the_webhook_is_closed(harness: Harness) -> None:
    harness.client.app.state.settings.twilio_auth_token = None  # type: ignore[attr-defined]
    assert harness.send_sms("hello").status_code == 503


def test_public_url_is_what_gets_signed_behind_a_proxy(harness: Harness) -> None:
    settings = harness.client.app.state.settings  # type: ignore[attr-defined]
    settings.baton_public_url = "https://baton.example.com"
    params = {"From": AVA_PHONE, "To": "+15550000000", "Body": "HELP", "MessageSid": "SM1"}
    public = "https://baton.example.com/channels/sms/inbound"
    assert harness.send_sms("HELP", sid="SM1").status_code == 403  # signed for the internal URL
    signed = twilio_signature(TWILIO_TOKEN, public, params.items())
    resp = harness.send_sms("HELP", signature=signed, sid="SM1")
    assert resp.status_code == 200


# ---------------------------------------------------------------- keywords


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("STOP", "stop"),
        ("stop", "stop"),
        (" Stop. ", "stop"),
        ("unsubscribe", "stop"),
        ("CANCEL", "stop"),
        ("start", "start"),
        ("HELP", "help"),
        ("stop my subscription", None),
        ("cancel my order NW-10004", None),
        ("help me with a return", None),
    ],
)
def test_keywords_only_match_the_whole_message(body: str, expected: str | None) -> None:
    assert keyword(body) == expected


def test_stop_opts_out_and_suppresses_later_replies(harness: Harness) -> None:
    resp = harness.send_sms("STOP")
    assert STOP_REPLY in resp.text
    assert harness.llm.agent_calls == []  # keywords never reach the agent

    harness.llm.route_to = "none"
    harness.llm.push(say("Hi there"))
    assert harness.send_sms("hello?").status_code == 200
    assert len(harness.llm.agent_calls) == 1  # the agent still answers in the transcript...
    assert harness.sms.sent == []  # ...but nothing is texted to an opted-out number

    assert "resubscribed" in harness.send_sms("START").text
    harness.llm.push(say("Welcome back"))
    harness.send_sms("hi again")
    assert harness.sms.sent == [(AVA_PHONE, "Welcome back")]


def test_help_is_answered_without_the_agent(harness: Harness) -> None:
    assert HELP_REPLY.replace("&", "&amp;") in harness.send_sms("HELP").text
    assert harness.llm.agent_calls == []


# ---------------------------------------------------------------- threading by phone


def _sms_conversations(h: Harness) -> list[str]:
    with h.sessions() as session:
        return list(session.scalars(select(Conversation.id).order_by(Conversation.created_at)))


def test_texts_from_one_phone_continue_one_conversation(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("Hi!"), say("Sure."))
    harness.send_sms("hi")
    harness.send_sms("one more thing")
    assert len(_sms_conversations(harness)) == 1


def test_a_text_after_a_day_starts_a_new_conversation(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("Hi!"), say("Hello again."))
    harness.send_sms("hi")
    with harness.sessions() as session:
        session.execute(update(Conversation).values(updated_at=utcnow() - timedelta(days=2)))
        session.commit()
    harness.send_sms("new question")
    assert len(_sms_conversations(harness)) == 2


def test_normalize_phone() -> None:
    assert normalize_phone("+1 (206) 555-0101") == "+12065550101"
    assert normalize_phone("2065550101") == "+12065550101"
    assert normalize_phone("+442071234567") == "+442071234567"


# ---------------------------------------------------------------- outbound


def test_twilio_sender_posts_form_with_basic_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"sid": "SM9"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    TwilioSmsSender("AC1", "tok", "+15550000000", client=client).send(AVA_PHONE, "hi")
    (req,) = seen
    assert req.url.path == "/2010-04-01/Accounts/AC1/Messages.json"
    assert req.headers["authorization"].startswith("Basic ")
    assert b"To=%2B12065550101" in req.content


def test_twilio_sender_raises_without_echoing_the_response() -> None:
    from baton.channels.sms import SmsDeliveryError

    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, text="secret details"))
    )
    with pytest.raises(SmsDeliveryError) as err:
        TwilioSmsSender("AC1", "tok", "+1", client=client).send(AVA_PHONE, "hi")
    assert "secret" not in str(err.value)


def test_a_twilio_retry_runs_one_turn(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("Hi!"))  # a second turn would exhaust the script and fail loudly
    assert harness.send_sms("hello", sid="SMdup").status_code == 200
    assert harness.send_sms("hello", sid="SMdup").status_code == 200
    assert harness.sms.sent == [(AVA_PHONE, "Hi!")]


def test_a_text_that_waited_out_a_stuck_turn_is_retried_not_lost(harness: Harness) -> None:
    """A turn holds the conversation longer than the webhook will wait: the text isn't
    stored, so its MessageSid is forgotten and Twilio's retry is processed."""
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say("Hi!"))
    h.send_sms("hello")
    (cid,) = _sms_conversations(h)
    service = h.client.app.state.get_service()  # type: ignore[attr-defined]
    service.busy_retries, service.busy_wait_s = 2, 0.0
    with h.sessions() as session:
        lease = acquire_turn(session, cid)  # a stuck turn
        session.commit()
    h.send_sms("are you there?", sid="SMstuck")
    assert h.sms.sent == [(AVA_PHONE, "Hi!")]
    assert not service.seen("sms", "SMstuck")

    with h.sessions() as session:
        release_turn(session, cid, lease)
        session.commit()
    h.llm.push(say("Yes, still here."))
    h.send_sms("are you there?", sid="SMstuck")  # Twilio's retry
    assert h.sms.sent[-1] == (AVA_PHONE, "Yes, still here.")
