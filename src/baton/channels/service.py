"""The conversation service: one path from any channel to the agent and back.

Web chat, email and SMS all end up in `customer_message`, which runs `Agent.handle_turn` (same
procedures, tools and guardrails whatever the channel) and then `deliver`s the reply the way
the conversation's channel needs: pushed to the widget's WebSocket, emailed in the same thread,
or texted. Human replies from the console are delivered the same way (`human_reply`).

The service also finds the conversation an inbound email or SMS belongs to:

- email: the Message-IDs it replies to (`In-Reply-To`, `References`) are looked up in
  `channel_threads`. Our Message-IDs are random, so only someone who received our email can
  quote one, and it only counts if they are also the conversation's customer: someone else
  replying to a forwarded email starts their own conversation instead of reading (or writing
  into) the original. Failing that, the same sender with the same subject within
  `EMAIL_THREAD_WINDOW` continues the conversation, but only when the provider vouches for
  the sender (`authenticated`: SPF/DKIM passed). A forged `From` could otherwise walk into a
  conversation whose customer is already verified. A Message-ID seen before is a webhook
  retry and is ignored.
- SMS: the phone number's latest conversation, unless it is open/resolved and older than
  `SMS_THREAD_WINDOW`. A human-owned conversation is always continued. A Twilio `MessageSid`
  seen before is a retry and is ignored.

Webhook work runs in the background and waits out a busy conversation for as long as a turn
lease can last, so a message isn't dropped because a slow turn was still running. If the turn
fails anyway, the external id is forgotten so the provider's retry is processed.

Every change is published to the event hub: the console's inbox listens on `inbox`, the widget
on its conversation's topic.
The service always passes a turn listener (to publish typing activity), so every channel's
turn calls the model through `LLMClient.stream`; reply text is still only sent once complete.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from baton.channels.base import OutboundContext, Sender, format_reply
from baton.channels.email import (
    EmailInbound,
    EmailOut,
    EmailSender,
    new_message_id,
    normalize_subject,
    reply_subject,
    strip_quoted,
)
from baton.channels.events import INBOX, Event, EventHub, conversation_topic
from baton.channels.links import transcript_token
from baton.channels.sms import HELP_REPLY, START_REPLY, STOP_REPLY, SmsSender, keyword
from baton.db.models import ChannelThread, Conversation, Message, MessageRole, SmsOptOut
from baton.db.session import session_scope
from baton.runtime import handoff
from baton.runtime.agent import Agent, AgentReply, Citation, TurnEvent, TurnListener
from baton.runtime.state import TURN_LEASE, Channel, ConversationBusyError, ConversationState

log = structlog.get_logger("baton.channels")

EMAIL_THREAD_WINDOW = timedelta(days=7)
SMS_THREAD_WINDOW = timedelta(hours=24)
MAX_CUSTOMER_CHARS = 4000
REQUEST_HUMAN_TEXT = "I'd like to talk to a person, please."
BUSY_WAIT_S = 1.0
# Long enough to outlast any lease: a turn that is still running (or crashed holding the
# lease) can't make a webhook message give up.
BUSY_RETRIES = int((TURN_LEASE.total_seconds() + 30) / BUSY_WAIT_S)


@dataclass(frozen=True)
class Delivery:
    """What happened to one outbound message."""

    delivered: bool
    body: str = ""
    error: str | None = None


@dataclass(frozen=True)
class SmsResult:
    """The result of an inbound SMS. `immediate` is a reply for the webhook response (TwiML);
    `conversation_id` is set when the text goes on to the agent."""

    immediate: str | None = None
    conversation_id: str | None = None


def message_event(
    message_id: int | None, role: str, body: str, citations: Sequence[Citation] = ()
) -> Event:
    return {
        "type": "message",
        "message": {
            "id": message_id,
            "role": role,
            "body": body,
            "citations": [c.model_dump() for c in citations],
            "created_at": datetime.now(UTC).isoformat(),
        },
    }


class ConversationService:
    def __init__(
        self,
        get_agent: Callable[[], Agent],
        sessions: sessionmaker[Session],
        hub: EventHub,
        email_sender: EmailSender,
        sms_sender: SmsSender,
        *,
        web_url: str = "http://localhost:5173",
        link_secret: str | None = None,
        busy_retries: int = BUSY_RETRIES,
        busy_wait_s: float = BUSY_WAIT_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """`get_agent` is called on first use, so the service exists without an LLM key."""
        self._get_agent = get_agent
        self.sessions = sessions
        self.hub = hub
        self.email_sender = email_sender
        self.sms_sender = sms_sender
        self.web_url = web_url
        self.link_secret = link_secret
        self.busy_retries = busy_retries
        self.busy_wait_s = busy_wait_s
        self._sleep = sleep
        self._clock = clock

    @property
    def agent(self) -> Agent:
        return self._get_agent()

    # ------------------------------------------------------------------ conversations

    def start(
        self,
        channel: Channel,
        customer_hint: str | None = None,
        *,
        contact: str | None = None,
        subject: str | None = None,
    ) -> str:
        cid = self.agent.start_conversation(channel, customer_hint)
        if contact or subject:
            with session_scope(self.sessions) as session:
                convo = session.get(Conversation, cid)
                assert convo is not None
                convo.contact, convo.subject = contact, subject
        self._inbox(cid, "created")
        return cid

    def customer_message(
        self,
        conversation_id: str,
        text: str,
        *,
        meta: dict[str, object] | None = None,
        listener: TurnListener | None = None,
        wait_if_busy: bool = False,
    ) -> tuple[AgentReply, Delivery | None]:
        """Run the agent on a customer message and deliver its reply (if it gave one).

        `wait_if_busy` (email/SMS, which run in the background) retries while another turn
        holds the conversation, instead of raising `ConversationBusyError`: a customer's
        second text while the first is being answered is queued, not dropped."""
        text = text.strip()[:MAX_CUSTOMER_CHARS]

        def relay(event: TurnEvent) -> None:
            if event.kind == "received":
                self._inbox(conversation_id, "message")
            else:
                self.hub.publish(
                    conversation_topic(conversation_id),
                    {"type": "typing", "activity": event.detail},
                )
                self.hub.publish(
                    INBOX,
                    {
                        "type": "activity",
                        "conversation_id": conversation_id,
                        "activity": event.detail,
                    },
                )
            if listener is not None:
                listener(event)

        attempts = self.busy_retries if wait_if_busy else 1
        for attempt in range(attempts):
            try:
                reply = self.agent.handle_turn(conversation_id, text, listener=relay, meta=meta)
                break
            except ConversationBusyError:
                if attempt == attempts - 1:
                    raise
                self._sleep(self.busy_wait_s)

        delivery = None
        if reply.replied:
            delivery = self.deliver(
                conversation_id, reply.text, reply.citations, message_id=reply.message_id
            )
        else:
            self.hub.publish(
                conversation_topic(conversation_id), {"type": "typing", "activity": ""}
            )
        self._status(conversation_id, reply.status)
        return reply, delivery

    def background_message(
        self, conversation_id: str, text: str, meta: dict[str, object] | None = None
    ) -> None:
        """`customer_message` for webhook background tasks: waits while the conversation is
        busy, and logs (never raises) a failure, since there's no caller left to tell."""
        try:
            self.customer_message(conversation_id, text, meta=meta, wait_if_busy=True)
        except Exception as exc:
            log.error(
                "background_turn_failed", conversation_id=conversation_id, error=type(exc).__name__
            )

    def background_sms(self, conversation_id: str, text: str, message_sid: str | None) -> None:
        try:
            self.customer_message(
                conversation_id, text, meta={"sms_sid": message_sid or ""}, wait_if_busy=True
            )
        except Exception as exc:
            if message_sid:
                self._forget_thread("sms", message_sid)  # let Twilio's retry through
            log.error(
                "background_turn_failed", conversation_id=conversation_id, error=type(exc).__name__
            )

    def background_email(self, inbound: EmailInbound) -> None:
        try:
            self.receive_email(inbound)
        except Exception as exc:
            log.error("background_email_failed", error=type(exc).__name__)

    # ------------------------------------------------------------------ delivery

    def deliver(
        self,
        conversation_id: str,
        text: str,
        citations: Sequence[Citation] = (),
        *,
        sender: Sender = "agent",
        operator: str | None = None,
        message_id: int | None = None,
    ) -> Delivery:
        """Send a stored reply to the customer through the conversation's own channel."""
        with self.sessions() as session:
            convo = session.get(Conversation, conversation_id)
            if convo is None:
                return Delivery(False, error="conversation not found")
            channel = cast(Channel, convo.channel)
            contact, subject = convo.contact, convo.subject
        token = transcript_token(self.link_secret, conversation_id) if self.link_secret else None
        ctx = OutboundContext(conversation_id, sender, operator, self.web_url, token)
        body = format_reply(channel, text, citations, ctx)
        role = MessageRole.AGENT.value if sender == "agent" else MessageRole.HUMAN_AGENT.value
        # The widget renders markdown; other channels' bodies are shown as-is in the console.
        self.hub.publish(
            conversation_topic(conversation_id),
            message_event(message_id, role, format_reply("web", text, citations, ctx), citations),
        )
        try:
            if channel == "email":
                if not contact:
                    return Delivery(False, body, "no email address on the conversation")
                self._send_email(conversation_id, contact, subject or "", body)
            elif channel == "sms":
                if not contact:
                    return Delivery(False, body, "no phone number on the conversation")
                if self._opted_out(contact):
                    log.info("sms_suppressed_opted_out", conversation_id=conversation_id)
                    return Delivery(False, body, "the customer has opted out of SMS (STOP)")
                self.sms_sender.send(contact, body)
        except Exception as exc:  # a provider outage must not lose the stored reply
            log.error(
                "delivery_failed",
                conversation_id=conversation_id,
                channel=channel,
                error=type(exc).__name__,
            )
            self._inbox(conversation_id, "delivery_failed")
            return Delivery(False, body, f"{channel} delivery failed ({type(exc).__name__})")
        return Delivery(True, body)

    def _send_email(self, conversation_id: str, to: str, subject: str, body: str) -> None:
        answering = self._last_customer_email_id(conversation_id)
        out = EmailOut(
            to=to,
            subject=reply_subject(subject),
            body=body,
            message_id=new_message_id(),
            in_reply_to=answering,
            references=[answering] if answering else [],
        )
        self.email_sender.send(out)
        # The customer's reply to this email will quote our Message-ID.
        self._remember_thread("email", out.message_id, conversation_id)

    # ------------------------------------------------------------------ email

    def receive_email(self, inbound: EmailInbound) -> str | None:
        """Thread an inbound email and run the agent on it. Returns the conversation id, or
        None if this Message-ID was already processed (a webhook retry) or nothing is left
        after stripping the quoted history."""
        text = strip_quoted(inbound.text)
        if not text or self.seen("email", inbound.message_id):
            return None
        cid = self._email_conversation(inbound)
        if not self._remember_thread("email", inbound.message_id, cid):
            log.info("email_duplicate_ignored", conversation_id=cid)  # a concurrent retry
            return None
        try:
            self.customer_message(
                cid, text, meta={"email_message_id": inbound.message_id}, wait_if_busy=True
            )
        except Exception:
            self._forget_thread("email", inbound.message_id)  # the provider's retry may run
            raise
        return cid

    def _email_conversation(self, inbound: EmailInbound) -> str:
        with self.sessions() as session:
            for thread_id in inbound.thread_ids:
                row = session.scalars(
                    select(ChannelThread).where(
                        ChannelThread.channel == "email", ChannelThread.thread_key == thread_id
                    )
                ).first()
                if row is None:
                    continue
                convo = session.get(Conversation, row.conversation_id)
                if convo is not None and convo.contact == inbound.from_:
                    return convo.id
                log.warning("email_thread_sender_mismatch", conversation_id=row.conversation_id)
                break
            # A forgeable From can't pick a conversation by subject.
            subject = normalize_subject(inbound.subject) if inbound.authenticated else ""
            since = self._clock() - EMAIL_THREAD_WINDOW
            candidates = session.scalars(
                select(Conversation)
                .where(
                    Conversation.channel == "email",
                    Conversation.contact == inbound.from_,
                    Conversation.updated_at >= since,
                )
                .order_by(Conversation.updated_at.desc())
            ).all()
            for convo in candidates:
                if subject and normalize_subject(convo.subject or "") == subject:
                    return convo.id
        return self.start(
            "email", inbound.from_, contact=inbound.from_, subject=inbound.subject[:300]
        )

    def _last_customer_email_id(self, conversation_id: str) -> str | None:
        with self.sessions() as session:
            rows = session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role == MessageRole.CUSTOMER.value,
                )
                .order_by(Message.id.desc())
                .limit(20)
            ).all()
        for row in rows:
            value = (row.meta or {}).get("email_message_id")
            if isinstance(value, str):
                return value
        return None

    def seen(self, channel: str, key: str) -> bool:
        """Whether an external id (email Message-ID, Twilio MessageSid) was processed."""
        with self.sessions() as session:
            found = session.scalars(
                select(ChannelThread.id).where(
                    ChannelThread.channel == channel, ChannelThread.thread_key == key
                )
            ).first()
        return found is not None

    def _forget_thread(self, channel: str, key: str) -> None:
        with session_scope(self.sessions) as session:
            row = session.scalars(
                select(ChannelThread).where(
                    ChannelThread.channel == channel, ChannelThread.thread_key == key
                )
            ).first()
            if row is not None:
                session.delete(row)

    def _remember_thread(self, channel: str, key: str, conversation_id: str) -> bool:
        """Record an external thread id. False if it was already known."""
        try:
            with session_scope(self.sessions) as session:
                session.add(
                    ChannelThread(channel=channel, thread_key=key, conversation_id=conversation_id)
                )
        except IntegrityError:
            return False
        return True

    # ------------------------------------------------------------------ SMS

    def receive_sms(self, phone: str, body: str, message_sid: str | None = None) -> SmsResult:
        """Handle the keyword part of an inbound SMS right away; anything else is for the
        agent (call `background_sms(result.conversation_id, ...)` afterwards). A MessageSid
        seen before (a Twilio retry) does nothing."""
        if message_sid and self.seen("sms", message_sid):
            log.info("sms_duplicate_ignored")
            return SmsResult()
        kind = keyword(body)
        if kind == "stop":
            self._set_opt_out(phone, True)
            return SmsResult(immediate=STOP_REPLY)
        if kind == "start":
            self._set_opt_out(phone, False)
            return SmsResult(immediate=START_REPLY)
        if kind == "help":
            return SmsResult(immediate=HELP_REPLY)
        if not body.strip():
            return SmsResult()
        cid = self._sms_conversation(phone)
        if message_sid and not self._remember_thread("sms", message_sid, cid):
            return SmsResult()  # a concurrent retry got here first
        return SmsResult(conversation_id=cid)

    def _sms_conversation(self, phone: str) -> str:
        with self.sessions() as session:
            convo = session.scalars(
                select(Conversation)
                .where(Conversation.channel == "sms", Conversation.contact == phone)
                .order_by(Conversation.updated_at.desc())
            ).first()
            if convo is not None and (
                convo.status == "escalated" or convo.updated_at >= self._clock() - SMS_THREAD_WINDOW
            ):
                return convo.id
        return self.start("sms", phone, contact=phone)

    def _opted_out(self, phone: str) -> bool:
        with self.sessions() as session:
            return session.get(SmsOptOut, phone) is not None

    def _set_opt_out(self, phone: str, opted_out: bool) -> None:
        with session_scope(self.sessions) as session:
            row = session.get(SmsOptOut, phone)
            if opted_out and row is None:
                session.add(SmsOptOut(phone=phone))
            elif not opted_out and row is not None:
                session.delete(row)

    # ------------------------------------------------------------------ the human side

    def take_over(self, conversation_id: str, operator: str) -> ConversationState:
        state = handoff.take_over(self.sessions, conversation_id, operator)
        self._status(conversation_id, state.status, note="A teammate has joined the chat.")
        return state

    def human_reply(self, conversation_id: str, operator: str, text: str) -> Delivery:
        stored = handoff.human_reply(self.sessions, conversation_id, operator, text)
        delivery = self.deliver(
            conversation_id,
            text.strip(),
            sender="human_agent",
            operator=operator,
            message_id=stored.message_id,
        )
        self._inbox(conversation_id, "message")
        return delivery

    def hand_back(
        self, conversation_id: str, operator: str, action: handoff.HandBack
    ) -> ConversationState:
        state = handoff.hand_back(self.sessions, conversation_id, operator, action)
        self._status(conversation_id, state.status)
        return state

    # ------------------------------------------------------------------ events

    def _inbox(self, conversation_id: str, reason: str) -> None:
        self.hub.publish(
            INBOX, {"type": "updated", "conversation_id": conversation_id, "reason": reason}
        )

    def _status(self, conversation_id: str, status: str, note: str | None = None) -> None:
        event: Event = {"type": "status", "status": status}
        if note:
            event["note"] = note
        self.hub.publish(conversation_topic(conversation_id), event)
        self._inbox(conversation_id, "status")
