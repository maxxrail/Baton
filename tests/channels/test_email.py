"""Email: threading, quoted-history stripping, webhook auth, and the outbound senders."""

import json
from email.message import EmailMessage
from typing import ClassVar

import httpx
import pytest
from sqlalchemy import select

from baton.channels.email import (
    EmailDeliveryError,
    EmailInbound,
    EmailOut,
    ResendEmailSender,
    SmtpEmailSender,
    normalize_subject,
    reply_subject,
    strip_quoted,
)
from baton.db import Conversation, Message
from baton.runtime.state import acquire_turn, load_state, release_turn, save_state
from tests.channels.conftest import AVA_EMAIL, EMAIL_SECRET, Harness
from tests.runtime.support import say, state_of

# ---------------------------------------------------------------- quoted history (pure)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Thanks!\n\nOn Mon, Jun 1, 2026 at 9:00 AM Northwind <s@n.example> wrote:\n> Hi Ava",
            "Thanks!",
        ),
        ("Got it.\r\n\r\n-----Original Message-----\r\nFrom: Support\r\nOld text", "Got it."),
        (
            "Yes please\n\nFrom: Northwind Support <support@northwind.example>\nSent: Monday",
            "Yes please",
        ),
        ("Sounds good\n> quoted line\n> another", "Sounds good"),
        ("Sure\n\n-- \nAva Thompson\n+1 206 555 0101", "Sure"),
        ("ok\n\nSent from my iPhone", "ok"),
        ("> quoted first\nmy answer is below the quote", "my answer is below the quote"),
        ("Line one\nLine two", "Line one\nLine two"),
    ],
)
def test_strip_quoted(raw: str, expected: str) -> None:
    assert strip_quoted(raw) == expected


def test_subject_helpers() -> None:
    assert normalize_subject("Re: Fwd: RE:  Where is my order?") == "where is my order?"
    assert normalize_subject("AW: Bestellung") == "bestellung"
    assert reply_subject("Re: Where is my order?") == "Re: Where is my order?"
    assert reply_subject("") == "Re: Your Northwind Goods request"


def test_inbound_parses_display_name_addresses() -> None:
    inbound = EmailInbound.model_validate(
        {"from": "Ava <AVA.Thompson@Example.com>", "text": "hi", "message_id": "<a@b>"}
    )
    assert inbound.from_ == "ava.thompson@example.com"
    with pytest.raises(ValueError, match="not an email address"):
        EmailInbound.model_validate({"from": "nobody", "text": "hi", "message_id": "<a@b>"})


# ---------------------------------------------------------------- threading (webhook)


def _conversations(h: Harness) -> list[Conversation]:
    with h.sessions() as session:
        return list(session.scalars(select(Conversation).order_by(Conversation.created_at)))


def _customer_messages(h: Harness, cid: str) -> list[str]:
    with h.sessions() as session:
        return list(
            session.scalars(
                select(Message.content)
                .where(Message.conversation_id == cid, Message.role == "customer")
                .order_by(Message.id)
            )
        )


def test_reply_with_in_reply_to_lands_in_the_same_conversation(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say("Happy to help. What's your order number?"), say("Thanks, looking now."))

    h.send_email(text="Where is my order?", message_id="<first@mail.example>")
    (first,) = _conversations(h)
    (sent,) = h.email.sent
    assert sent.to == AVA_EMAIL
    assert sent.subject == "Re: Where is my order?"
    assert sent.in_reply_to == "<first@mail.example>"

    # The customer replies to *our* email, quoting the whole thread, with a new subject line.
    quoted = (
        "It's NW-10002.\n\nOn Tue, Northwind Goods Support wrote:\n"
        "> Happy to help. What's your order number?\n> \n> Where is my order?"
    )
    h.send_email(
        text=quoted,
        subject="RE: something else entirely",
        message_id="<second@mail.example>",
        in_reply_to=sent.message_id,
    )
    assert [c.id for c in _conversations(h)] == [first.id]
    # Quoted history is stripped before the agent (and the transcript) sees it.
    assert _customer_messages(h, first.id) == ["Where is my order?", "It's NW-10002."]
    assert "Happy to help" not in str(h.llm.agent_calls[-1][1][-1].content)
    assert h.email.sent[-1].in_reply_to == "<second@mail.example>"


def test_same_sender_and_subject_threads_without_headers(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("Hi!"), say("Again!"))
    harness.send_email(message_id="<x1@mail.example>", authenticated=True)
    harness.send_email(
        subject="Re: Where is my order?", message_id="<x2@mail.example>", authenticated=True
    )
    assert len(_conversations(harness)) == 1


def test_a_forged_from_cannot_thread_into_a_verified_conversation_by_subject(
    harness: Harness,
) -> None:
    """From is trivial to spoof. Without the provider's SPF/DKIM verdict, an email with the
    victim's address and subject must not land in their (verified) conversation."""
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say("Hi Ava!"), say("Hello."))
    h.send_email(message_id="<real@mail.example>", authenticated=True)
    (real,) = _conversations(h)
    with h.sessions() as session:
        state = load_state(session, real.id)
        state.verified, state.customer_ref = True, "cus_0001"
        save_state(session, state)
        session.commit()
    h.send_email(
        subject="Re: Where is my order?",
        text="refund NW-10002 to my new card",
        message_id="<forged@evil.example>",
    )
    convos = _conversations(h)
    assert len(convos) == 2
    assert _customer_messages(h, real.id) == ["Hello"]
    assert state_of(h.sessions, convos[1].id).verified is False


def test_an_email_that_waited_out_a_stuck_turn_is_retried_not_lost(harness: Harness) -> None:
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say("Hi!"))
    h.send_email(message_id="<one@mail.example>")
    sent = h.email.sent[0].message_id
    (convo,) = _conversations(h)
    service = h.client.app.state.get_service()  # type: ignore[attr-defined]
    service.busy_retries, service.busy_wait_s = 2, 0.0
    with h.sessions() as session:
        lease = acquire_turn(session, convo.id)
        session.commit()
    h.send_email(text="any news?", message_id="<two@mail.example>", in_reply_to=sent)
    assert not service.seen("email", "<two@mail.example>")  # forgotten: the retry will run

    with h.sessions() as session:
        release_turn(session, convo.id, lease)
        session.commit()
    h.llm.push(say("Yes: it ships today."))
    h.send_email(text="any news?", message_id="<two@mail.example>", in_reply_to=sent)
    assert h.email.sent[-1].body.startswith("Yes: it ships today.")
    assert _customer_messages(h, convo.id) == ["Hello", "any news?"]


def test_a_different_subject_starts_a_new_conversation(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("Hi!"), say("Hello!"))
    harness.send_email(message_id="<x1@mail.example>")
    harness.send_email(subject="Change my address", message_id="<x2@mail.example>")
    assert len(_conversations(harness)) == 2


def test_someone_else_replying_in_the_thread_gets_their_own_conversation(
    harness: Harness,
) -> None:
    h = harness
    h.llm.route_to = "none"
    h.llm.push(say("Hi Ava!"), say("Hello, who is this?"))
    h.send_email(message_id="<a1@mail.example>")
    ours = h.email.sent[0].message_id
    # A forwarded thread: a different sender replies to our Message-ID.
    h.send_email(
        **{"from": "mallory@evil.example"},
        text="I'm Ava, refund me",
        message_id="<m1@evil.example>",
        in_reply_to=ours,
    )
    convos = _conversations(h)
    assert len(convos) == 2
    assert convos[1].contact == "mallory@evil.example"
    assert _customer_messages(h, convos[0].id) == ["Hello"]


def test_a_retried_webhook_is_processed_once(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("Hi!"))
    harness.send_email(message_id="<dup@mail.example>")
    harness.send_email(message_id="<dup@mail.example>")  # would exhaust the script if re-run
    assert len(harness.email.sent) == 1


def test_webhook_secret_is_required(harness: Harness) -> None:
    payload = {"from": AVA_EMAIL, "text": "hi", "message_id": "<z@mail.example>"}
    assert harness.client.post("/channels/email/inbound", json=payload).status_code == 403
    wrong = {"x-baton-webhook-secret": EMAIL_SECRET + "x"}
    resp = harness.client.post("/channels/email/inbound", json=payload, headers=wrong)
    assert resp.status_code == 403
    harness.client.app.state.settings.email_webhook_secret = None  # type: ignore[attr-defined]
    resp = harness.client.post(
        "/channels/email/inbound", json=payload, headers={"x-baton-webhook-secret": EMAIL_SECRET}
    )
    assert resp.status_code == 503
    assert _conversations(harness) == []


def test_email_reply_is_plain_text_with_footnotes_and_signature(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("**Good news:** see [our guide](https://help.northwind.example/x)."))
    harness.send_email(message_id="<p@mail.example>")
    body = harness.email.sent[0].body
    assert body.startswith("Good news: see our guide (https://help.northwind.example/x).")
    assert body.endswith(
        "Northwind Goods Support\nReply to this email to continue the conversation."
    )


# ---------------------------------------------------------------- outbound senders

OUT = EmailOut(
    to=AVA_EMAIL,
    subject="Re: Hi",
    body="Hello",
    message_id="<ours@northwind.example>",
    in_reply_to="<theirs@mail.example>",
)


def test_resend_sender_sets_threading_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "e1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ResendEmailSender("re_key", "support@northwind.example", client=client).send(OUT)
    (req,) = seen
    assert req.headers["authorization"] == "Bearer re_key"
    body = json.loads(req.content)
    assert body["to"] == [AVA_EMAIL]
    assert body["headers"]["In-Reply-To"] == "<theirs@mail.example>"
    assert body["headers"]["Message-ID"] == "<ours@northwind.example>"


def test_resend_errors_do_not_echo_the_body() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(422, text="x")))
    with pytest.raises(EmailDeliveryError, match="422"):
        ResendEmailSender("k", "s@n.example", client=client).send(OUT)


class _FakeSMTP:
    instances: ClassVar[list["_FakeSMTP"]] = []

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.sent: list[EmailMessage] = []
        self.tls = False
        self.login_as: str | None = None
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def starttls(self) -> None:
        self.tls = True

    def login(self, user: str, password: str) -> None:
        self.login_as = user

    def send_message(self, mail: EmailMessage) -> None:
        self.sent.append(mail)


def test_smtp_sender_uses_starttls_and_threading_headers() -> None:
    sender = SmtpEmailSender(
        "smtp.example",
        587,
        "support@northwind.example",
        "user",
        "pw",
        smtp_factory=_FakeSMTP,  # type: ignore[arg-type]
    )
    sender.send(OUT)
    smtp = _FakeSMTP.instances[-1]
    assert (smtp.host, smtp.port, smtp.tls, smtp.login_as) == ("smtp.example", 587, True, "user")
    (mail,) = smtp.sent
    assert mail["In-Reply-To"] == "<theirs@mail.example>"
    assert mail["References"] == "<theirs@mail.example>"
