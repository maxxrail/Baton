"""SMS via Twilio: webhook signature validation, STOP/HELP keywords, outbound senders.

Twilio signs every webhook: HMAC-SHA1, keyed with the account's auth token, over the full URL
it called followed by each POST parameter's name and value, sorted by name; the base64 digest
arrives in `X-Twilio-Signature`. Anything that doesn't match is rejected before it's parsed
further, so nobody can post fake customer messages (or fake STOPs) to the endpoint.

Keywords follow Twilio's defaults and only match when they are the whole message: "stop"
opts out, but "stop my subscription" goes to the agent.
"""

import base64
import hashlib
import hmac
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, Protocol
from xml.sax.saxutils import escape

import httpx
import structlog

from baton.config import Settings
from baton.runtime.redact import redact

log = structlog.get_logger("baton.channels.sms")

STOP_WORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT"})
START_WORDS = frozenset({"START", "UNSTOP", "YES"})
HELP_WORDS = frozenset({"HELP", "INFO"})

STOP_REPLY = (
    "You're unsubscribed from Northwind Goods texts and won't get any more. Reply START to "
    "resubscribe."
)
START_REPLY = "You're resubscribed to Northwind Goods support texts. Reply STOP to opt out."
HELP_REPLY = (
    "Northwind Goods support: text your question and we'll reply here. Help center: "
    "https://help.northwind.example. Reply STOP to opt out. Msg & data rates may apply."
)

Keyword = Literal["stop", "start", "help"]


def keyword(body: str) -> Keyword | None:
    word = body.strip().strip(".!").upper()
    if word in STOP_WORDS:
        return "stop"
    if word in START_WORDS:
        return "start"
    if word in HELP_WORDS:
        return "help"
    return None


def twilio_signature(auth_token: str, url: str, params: Iterable[tuple[str, str]]) -> str:
    """The `X-Twilio-Signature` Twilio would send for this URL and form body."""
    payload = url + "".join(k + v for k, v in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_twilio_signature(
    auth_token: str, url: str, params: Iterable[tuple[str, str]], signature: str | None
) -> bool:
    if not signature or not auth_token:
        return False
    expected = twilio_signature(auth_token, url, params)
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii", "replace"))


def twiml(message: str | None = None) -> str:
    """A TwiML response; with `message`, Twilio sends it back as an SMS."""
    body = f"<Message>{escape(message)}</Message>" if message else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def normalize_phone(raw: str) -> str:
    """E.164-ish: keep a leading + and digits. Twilio already sends E.164."""
    raw = raw.strip()
    digits = "".join(c for c in raw if c.isdigit())
    return f"+{digits}" if raw.startswith("+") or len(digits) > 10 else f"+1{digits}"


# ---------------------------------------------------------------------- outbound


class SmsSender(Protocol):
    def send(self, to: str, body: str) -> None: ...


class SmsDeliveryError(RuntimeError):
    pass


@dataclass
class ConsoleSmsSender:
    """Dev outbox: prints each SMS (redacted) and keeps it in `sent`."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    echo: bool = True

    def send(self, to: str, body: str) -> None:
        self.sent.append((to, body))
        if self.echo:
            print(f"--- sms to {redact(to)}:\n{redact(body)}\n---", file=sys.stderr)


class TwilioSmsSender:
    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._sid, self._token, self._from = account_sid, auth_token, from_number
        self._client = client or httpx.Client(timeout=10.0)

    def send(self, to: str, body: str) -> None:
        resp = self._client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json",
            auth=(self._sid, self._token),
            data={"From": self._from, "To": to, "Body": body},
        )
        if resp.status_code >= 300:
            raise SmsDeliveryError(f"twilio returned {resp.status_code}")


def build_sms_sender(settings: Settings) -> SmsSender:
    if settings.sms_provider == "twilio":
        token = settings.twilio_auth_token.get_secret_value() if settings.twilio_auth_token else ""
        if not (settings.twilio_account_sid and token and settings.twilio_from_number):
            raise ValueError(
                "SMS_PROVIDER=twilio requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and "
                "TWILIO_FROM_NUMBER"
            )
        return TwilioSmsSender(settings.twilio_account_sid, token, settings.twilio_from_number)
    return ConsoleSmsSender()
