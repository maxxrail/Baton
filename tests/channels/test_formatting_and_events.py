"""Per-channel formatting (SMS segments, plain text), the event hub, and turn listeners."""

import asyncio
import threading

import pytest

from baton.channels.base import (
    OutboundContext,
    fit_sms,
    format_reply,
    markdown_to_text,
    sms_segments,
    to_gsm,
)
from baton.channels.events import EventHub
from baton.runtime.agent import Citation

CTX = OutboundContext("c123", web_url="https://chat.northwind.example", link_token="9.ab")
CITE = Citation(article_id="return-policy", title="Return policy", url="https://h.example/rp")


# ---------------------------------------------------------------- SMS


def test_segment_counting_gsm_and_unicode() -> None:
    assert sms_segments("a" * 160) == 1
    assert sms_segments("a" * 161) == 2  # concatenated: 153 per segment
    assert sms_segments("a" * 306) == 2
    assert sms_segments("a" * 307) == 3
    assert sms_segments("€" * 80) == 1  # extension characters cost two septets
    assert sms_segments("€" * 81) == 2
    assert sms_segments("😀" * 35) == 1  # UCS-2: 70 units, emoji are two units
    assert sms_segments("é" * 160) == 1  # é is in the GSM basic set


def test_smart_punctuation_is_replaced_to_stay_in_gsm() -> None:
    text = "It\u2019s here \u2014 \u201cdelivered\u201d\u2026"
    assert to_gsm(text) == 'It\'s here - "delivered"...'
    assert sms_segments(to_gsm(text * 5)) < sms_segments(text * 5)


def test_long_sms_is_cut_at_a_word_with_a_link_to_the_full_answer() -> None:
    long = " ".join(f"word{i}" for i in range(200))
    out = format_reply("sms", long, [], CTX)
    assert sms_segments(out) <= 2
    assert out.endswith("... Full answer: https://chat.northwind.example/widget?c=c123&t=9.ab")
    assert out.startswith("word0 word1")
    assert "word19" in out  # a real amount of the answer survives, not just the link
    assert fit_sms("short", "x") == "short"
    # Without a link secret there is no link: the reply is cut with "...".
    unsigned = format_reply("sms", long, [], OutboundContext("c123"))
    assert unsigned.endswith("...")
    assert "widget" not in unsigned
    assert sms_segments(unsigned) <= 2


def test_sms_with_a_citation_links_the_article_and_strips_markdown() -> None:
    out = format_reply("sms", "**30 days** to return [kb:return-policy].", [CITE], CTX)
    assert out == "30 days to return. More: https://h.example/rp"


def test_web_keeps_markdown_with_numbered_citation_links() -> None:
    out = format_reply("web", "You have 30 days [kb:return-policy].", [CITE], CTX)
    assert out == (
        "You have 30 days [1](https://h.example/rp).\n\nSources:\n"
        "1. [Return policy](https://h.example/rp)"
    )


def test_email_signature_names_the_human_who_wrote_it() -> None:
    human = OutboundContext("c1", sender="human_agent", operator="Sam")
    assert format_reply("email", "Done.", [], human).endswith(
        "-- \nSam, Northwind Goods Support\nReply to this email to continue the conversation."
    )
    assert "\nNorthwind Goods Support\n" in format_reply("email", "Done.", [], CTX)


@pytest.mark.parametrize(
    ("md", "plain"),
    [
        ("**bold** and *italic*", "bold and italic"),
        ("## Heading\ntext", "Heading\ntext"),
        ("see [guide](https://x.example/g)", "see guide (https://x.example/g)"),
        ("2 * 3 * 4", "2 * 3 * 4"),  # arithmetic isn't emphasis
        ("- item one\n- item two", "- item one\n- item two"),
    ],
)
def test_markdown_to_text(md: str, plain: str) -> None:
    assert markdown_to_text(md) == plain


# ---------------------------------------------------------------- event hub


async def test_publish_from_another_thread_reaches_the_subscriber() -> None:
    hub = EventHub()
    sub = hub.subscribe("t")
    thread = threading.Thread(target=hub.publish, args=("t", {"n": 1}))
    thread.start()
    thread.join()
    assert await asyncio.wait_for(sub.queue.get(), timeout=1) == {"n": 1}
    hub.publish("other", {"n": 2})
    await asyncio.sleep(0)
    assert sub.queue.empty()
    hub.unsubscribe(sub)
    assert hub.subscribers("t") == 0


async def test_a_slow_subscriber_drops_its_oldest_events() -> None:
    hub = EventHub()
    sub = hub.subscribe("t")
    for i in range(sub.queue.maxsize + 5):
        hub.publish("t", {"n": i})
    await asyncio.sleep(0.01)
    assert sub.queue.qsize() == sub.queue.maxsize
    assert (await sub.queue.get())["n"] == 5
