"""Render `[kb:article_id]` citations for a channel.

The agent's reply (and the stored message) keeps the channel-neutral `[kb:article_id]` tags;
each channel turns them into something its customers can click or read:

- `markdown` (web widget): numbered links `[1](url)` inline, plus a "Sources" list of titles;
- `footnotes` (terminal, email): `[1]` inline and a "Sources" list with titles and URLs;
- `plain` (SMS): tags removed, and one "More:" link to the first cited article, since every
  character counts and SMS can't render links.

A tag for an article that isn't in `citations` (it was never retrieved) is dropped, never shown
as a dead link. The agent's grounding check already strips those; this is the last line.
"""

import re
from collections.abc import Sequence
from typing import Literal

from baton.knowledge.grounding import CITATION_RE
from baton.runtime.agent import Citation
from baton.runtime.state import Channel

Style = Literal["markdown", "footnotes", "plain"]

# The same tag twice in a row, with at most punctuation and spaces between them.
_REPEATED_TAG_RE = re.compile(r"\[kb:([a-z0-9-]+)\]([.!?,;:]?)\s*\[kb:\1\]")

STYLE_BY_CHANNEL: dict[Channel, Style] = {
    "web": "markdown",
    "email": "footnotes",
    "cli": "footnotes",
    "sms": "plain",
}


def render_citations(text: str, citations: Sequence[Citation], style: Style) -> str:
    known = {c.article_id: c for c in citations}
    order: list[str] = []

    def number(article_id: str) -> int:
        if article_id not in order:
            order.append(article_id)
        return order.index(article_id) + 1

    def replace(match: re.Match[str]) -> str:
        article_id = match.group(1)
        if article_id not in known or style == "plain":
            return ""
        n = number(article_id)
        return f"[{n}]({known[article_id].url})" if style == "markdown" else f"[{n}]"

    # Models sometimes cite a sentence and then the paragraph: "... days [kb:x]. [kb:x]".
    # Show one marker, not "[1]. [1]".
    collapsed = None
    while collapsed != text:
        collapsed, text = text, _REPEATED_TAG_RE.sub(r"[kb:\1]\2", text)
    body = CITATION_RE.sub(replace, text)
    body = re.sub(r"[ \t]+([.,!?;:])", r"\1", body)  # "days [kb:x]." -> "days." for plain
    body = re.sub(r"[ \t]{2,}", " ", body).strip()

    if style == "plain":
        cited = [a for a in dict.fromkeys(CITATION_RE.findall(text)) if a in known]
        return f"{body} More: {known[cited[0]].url}" if cited else body
    if not order:
        return body
    if style == "markdown":
        sources = "\n".join(
            f"{n}. [{known[a].title}]({known[a].url})" for n, a in enumerate(order, 1)
        )
    else:
        sources = "\n".join(
            f"[{n}] {known[a].title} - {known[a].url}" for n, a in enumerate(order, 1)
        )
    return f"{body}\n\nSources:\n{sources}"


def render_for_channel(text: str, citations: Sequence[Citation], channel: Channel) -> str:
    return render_citations(text, citations, STYLE_BY_CHANNEL[channel])
