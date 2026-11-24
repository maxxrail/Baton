"""The channel adapter interface: what differs between web chat, email and SMS, and nothing else.

Every channel goes through the same agent (`Agent.handle_turn`) with the same guardrails; an
adapter only

- normalises an inbound message into an `InboundMessage` (`conversation_key`, `customer_hint`,
  `text`), and
- formats an outbound reply for its medium: markdown with citation links for the web widget,
  plain text with footnotes and a signature for email, and at most two SMS segments with a
  link to the full answer for SMS.

The reply text the agent stores keeps channel-neutral `[kb:id]` tags; formatting happens on the
way out (`format_reply`), so a transcript can be re-rendered for any channel.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from baton.channels.citations import render_citations
from baton.runtime.agent import Citation
from baton.runtime.state import Channel

Sender = Literal["agent", "human_agent"]

BRAND = "Northwind Goods Support"
EMAIL_FOOTER = "Reply to this email to continue the conversation."
SMS_MAX_SEGMENTS = 2


@dataclass(frozen=True)
class InboundMessage:
    """A customer message, whatever channel it came from.

    `conversation_key` identifies the thread on the channel: the conversation id for web, the
    Message-IDs the email replies to, the phone number for SMS. `customer_hint` is what the
    channel tells us about who's writing (an email address, a phone number); it helps the
    agent look the customer up but never verifies them."""

    channel: Channel
    conversation_key: str | None
    customer_hint: str | None
    text: str
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundContext:
    conversation_id: str
    sender: Sender = "agent"
    operator: str | None = None  # who wrote a human_agent reply
    web_url: str = "http://localhost:5173"  # where SMS links to the full answer
    link_token: str | None = None  # signed transcript token (baton.channels.links)

    @property
    def transcript_url(self) -> str | None:
        if not self.link_token:
            return None
        base = self.web_url.rstrip("/")
        return f"{base}/widget?c={self.conversation_id}&t={self.link_token}"


class ChannelAdapter(Protocol):
    channel: Channel

    def format_reply(
        self, text: str, citations: Sequence[Citation], ctx: OutboundContext
    ) -> str: ...


# ---------------------------------------------------------------------- plain-text helpers

_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_EMPHASIS_RE = re.compile(r"(\*\*|__)(.+?)\1|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)


def markdown_to_text(text: str) -> str:
    """Strip the little markdown models write (bold, italics, headings, links) for channels
    that show plain text. Links become `label (url)`; bullets stay as they are."""
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _EMPHASIS_RE.sub(lambda m: m.group(2) or m.group(3) or "", text)
    return _HEADING_RE.sub("", text)


# ---------------------------------------------------------------------- SMS segments

# GSM 03.38: the basic set costs one septet per character, the extension set two. Anything
# else forces UCS-2 for the whole message (70 characters per single SMS instead of 160).
_GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXTENDED = set("^{}\\[~]|€\f")
# Characters models like to write that have a GSM equivalent; replacing them keeps a reply
# in GSM-7 and roughly halves the segment count.
_TO_GSM = str.maketrans(
    {
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote / apostrophe
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2026": "...",
        "\u2022": "-",  # bullet
        "\u00a0": " ",  # no-break space
    }
)


def to_gsm(text: str) -> str:
    return text.translate(_TO_GSM)


def sms_segments(text: str) -> int:
    """How many SMS segments `text` takes (concatenated messages lose room to a header)."""
    if all(c in _GSM_BASIC or c in _GSM_EXTENDED for c in text):
        units = sum(2 if c in _GSM_EXTENDED else 1 for c in text)
        single, multi = 160, 153
    else:
        units = sum(2 if ord(c) > 0xFFFF else 1 for c in text)  # UTF-16 code units
        single, multi = 70, 67
    if units <= single:
        return 1
    return -(-units // multi)


def fit_sms(text: str, link: str | None, max_segments: int = SMS_MAX_SEGMENTS) -> str:
    """`text` if it fits in `max_segments`, else as many whole words as fit followed by a
    link to the full answer (or just "..." without one)."""
    text = to_gsm(text)
    if sms_segments(text) <= max_segments:
        return text
    suffix = f"... Full answer: {link}" if link else "..."
    words = text.split()
    lo, hi = 0, len(words)
    while lo < hi:  # the most words whose shortened form still fits
        mid = (lo + hi + 1) // 2
        if sms_segments(" ".join(words[:mid]) + suffix) <= max_segments:
            lo = mid
        else:
            hi = mid - 1
    body = " ".join(words[:lo]).rstrip(" .,;:")
    return f"{body}{suffix}" if body else suffix.removeprefix("... ")


# ---------------------------------------------------------------------- adapters


class WebAdapter:
    """The widget renders markdown: citations become numbered links plus a Sources list."""

    channel: Channel = "web"

    def format_reply(self, text: str, citations: Sequence[Citation], ctx: OutboundContext) -> str:
        return render_citations(text, citations, "markdown")


class EmailAdapter:
    """Plain text: citation footnotes, no markdown, and a signature."""

    channel: Channel = "email"

    def format_reply(self, text: str, citations: Sequence[Citation], ctx: OutboundContext) -> str:
        body = markdown_to_text(render_citations(text, citations, "footnotes"))
        who = f"{ctx.operator}, {BRAND}" if ctx.sender == "human_agent" and ctx.operator else BRAND
        return f"{body}\n\n-- \n{who}\n{EMAIL_FOOTER}"


class SmsAdapter:
    """At most two segments; anything longer is cut at a word with a link to the full answer.
    A cited answer links to its article instead (`render_citations` plain style)."""

    channel: Channel = "sms"

    def format_reply(self, text: str, citations: Sequence[Citation], ctx: OutboundContext) -> str:
        body = markdown_to_text(render_citations(text, citations, "plain"))
        body = re.sub(r"\s+", " ", body).strip()
        return fit_sms(body, ctx.transcript_url)


class CliAdapter:
    channel: Channel = "cli"

    def format_reply(self, text: str, citations: Sequence[Citation], ctx: OutboundContext) -> str:
        return render_citations(text, citations, "footnotes")


ADAPTERS: dict[Channel, ChannelAdapter] = {
    "web": WebAdapter(),
    "email": EmailAdapter(),
    "sms": SmsAdapter(),
    "cli": CliAdapter(),
}


def format_reply(
    channel: Channel, text: str, citations: Sequence[Citation], ctx: OutboundContext
) -> str:
    return ADAPTERS[channel].format_reply(text, citations, ctx)
