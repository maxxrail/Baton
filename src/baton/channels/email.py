"""Email: the inbound webhook payload, quoted-reply stripping, and outbound senders.

Inbound is a generic JSON shape (`EmailInbound`) that any provider's inbound-parse webhook can
be mapped to. Outbound goes through an `EmailSender`:

- `ConsoleEmailSender` (dev): prints a redacted copy and keeps what it sent in memory;
- `ResendEmailSender`: the Resend HTTP API (`EMAIL_PROVIDER=resend`, `RESEND_API_KEY`);
- `SmtpEmailSender`: any SMTP server with STARTTLS (`EMAIL_PROVIDER=smtp`, `SMTP_*`).

Customers reply on top of the whole thread, so `strip_quoted` cuts the quoted history before
the text reaches the agent; otherwise the agent would re-read (and re-answer) its own earlier
replies, and the transcript would grow with every message.
"""

import re
import smtplib
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Protocol

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from baton.config import Settings
from baton.runtime.redact import redact

log = structlog.get_logger("baton.channels.email")

MAX_EMAIL_CHARS = 20_000  # before stripping; a customer message is capped again after


class EmailInbound(BaseModel):
    """What the inbound webhook accepts. `from` may be `Name <addr>` or a bare address."""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", min_length=3, max_length=320)
    subject: str = Field(default="", max_length=998)
    text: str = Field(max_length=MAX_EMAIL_CHARS)
    message_id: str = Field(min_length=1, max_length=300)
    in_reply_to: str | None = Field(default=None, max_length=300)
    references: list[str] = Field(default_factory=list, max_length=50)
    # The provider's verdict that `from` is genuine (SPF/DKIM/DMARC passed). `From` is trivial
    # to forge, so an unauthenticated email is never threaded by subject (see the service).
    authenticated: bool = False

    @field_validator("from_")
    @classmethod
    def _address(cls, value: str) -> str:
        address = parseaddr(value)[1].strip().lower()
        if "@" not in address or " " in address:
            raise ValueError("not an email address")
        return address

    @property
    def thread_ids(self) -> list[str]:
        """Message-IDs this email answers, most specific first."""
        ids = [self.in_reply_to] if self.in_reply_to else []
        return ids + [r for r in reversed(self.references) if r not in ids]


# ---------------------------------------------------------------------- quoted history

# A line that starts the quoted part of a reply, as common clients write it.
_QUOTE_HEADERS = [
    re.compile(r"^\s*On .{1,300}wrote:\s*$", re.IGNORECASE),  # Gmail, Apple Mail
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),  # Outlook
    re.compile(r"^\s*_{10,}\s*$"),  # Outlook web separator
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),  # Outlook header block
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE),  # mobile signatures
]
_SIGNATURE = re.compile(r"^-- ?$")  # RFC 3676 signature delimiter


def strip_quoted(text: str) -> str:
    """The new part of a reply: everything before the quoted history or signature, minus
    `>`-quoted lines. If that leaves nothing (a reply typed *below* the quote), the
    unquoted lines are kept instead, so a message is never emptied by stripping."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    kept: list[str] = []
    for line in lines:
        if _SIGNATURE.match(line) or any(p.match(line) for p in _QUOTE_HEADERS):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    body = "\n".join(kept).strip()
    if body:
        return body
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith(">")).strip()


_SUBJECT_PREFIX = re.compile(r"^\s*((re|fwd?|aw|sv)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


def normalize_subject(subject: str) -> str:
    """`Re: Fwd: Where is my order?` -> `where is my order?` (for thread matching)."""
    return re.sub(r"\s+", " ", _SUBJECT_PREFIX.sub("", subject)).strip().lower()


def reply_subject(subject: str) -> str:
    base = _SUBJECT_PREFIX.sub("", subject).strip() or "Your Northwind Goods request"
    return f"Re: {base}"


# ---------------------------------------------------------------------- outbound


@dataclass(frozen=True)
class EmailOut:
    to: str
    subject: str
    body: str
    message_id: str
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)


class EmailSender(Protocol):
    def send(self, message: EmailOut) -> None: ...


def new_message_id(domain: str = "northwind.example") -> str:
    return make_msgid(domain=domain)


class EmailDeliveryError(RuntimeError):
    pass


@dataclass
class ConsoleEmailSender:
    """Dev outbox: prints each email (redacted) and keeps it in `sent`."""

    sent: list[EmailOut] = field(default_factory=list)
    echo: bool = True

    def send(self, message: EmailOut) -> None:
        self.sent.append(message)
        if self.echo:
            print(
                f"--- email to {redact(message.to)}: {message.subject}\n"
                f"{redact(message.body)}\n---",
                file=sys.stderr,
            )


class ResendEmailSender:
    URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str, sender: str, client: httpx.Client | None = None) -> None:
        self._key = api_key
        self._from = sender
        self._client = client or httpx.Client(timeout=10.0)

    def send(self, message: EmailOut) -> None:
        headers = {"Message-ID": message.message_id}
        if message.in_reply_to:
            headers["In-Reply-To"] = message.in_reply_to
            headers["References"] = " ".join(message.references or [message.in_reply_to])
        resp = self._client.post(
            self.URL,
            headers={"Authorization": f"Bearer {self._key}"},
            json={
                "from": self._from,
                "to": [message.to],
                "subject": message.subject,
                "text": message.body,
                "headers": headers,
            },
        )
        if resp.status_code >= 300:
            # The response body may echo the message; only the status is reported.
            raise EmailDeliveryError(f"resend returned {resp.status_code}")


SmtpFactory = Callable[[str, int], smtplib.SMTP]


class SmtpEmailSender:
    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        username: str = "",
        password: str = "",
        smtp_factory: SmtpFactory = smtplib.SMTP,
    ) -> None:
        self._host, self._port, self._from = host, port, sender
        self._username, self._password = username, password
        self._factory = smtp_factory

    def send(self, message: EmailOut) -> None:
        mail = EmailMessage()
        mail["From"] = self._from
        mail["To"] = message.to
        mail["Subject"] = message.subject
        mail["Message-ID"] = message.message_id
        if message.in_reply_to:
            mail["In-Reply-To"] = message.in_reply_to
            mail["References"] = " ".join(message.references or [message.in_reply_to])
        mail.set_content(message.body)
        with self._factory(self._host, self._port) as smtp:
            smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(mail)


def build_email_sender(settings: Settings) -> EmailSender:
    if settings.email_provider == "resend":
        key = settings.resend_api_key.get_secret_value() if settings.resend_api_key else ""
        if not key:
            raise ValueError("EMAIL_PROVIDER=resend requires RESEND_API_KEY")
        return ResendEmailSender(key, settings.email_from)
    if settings.email_provider == "smtp":
        if not settings.smtp_host:
            raise ValueError("EMAIL_PROVIDER=smtp requires SMTP_HOST")
        password = settings.smtp_password.get_secret_value() if settings.smtp_password else ""
        return SmtpEmailSender(
            settings.smtp_host,
            settings.smtp_port,
            settings.email_from,
            settings.smtp_username,
            password,
        )
    return ConsoleEmailSender()
