"""Heading-aware chunking of help-center articles.

An article is split into sections at its Markdown headings; each section knows its heading path
(`("Return policy", "Return window")`). Sections are then packed, in order, into chunks:

- a chunk grows section by section and closes once it reaches `target_tokens` (300) or when the
  next section would push it over `max_tokens` (500), so chunks land in the ~300-500 token band
  whenever the article is long enough;
- a single section bigger than `max_tokens` is split at paragraph breaks, and each piece repeats
  the section heading so it still reads on its own;
- a short trailing chunk (under 100 tokens) is folded into the previous one when that stays
  under `max_tokens`, so an article doesn't end in a two-line fragment;
- a chunk never starts or ends mid-paragraph, and its `heading_path` is the deepest path shared
  by all of its sections (a whole-article chunk is just the title).

Token counts are estimated (~1.33 tokens per word): exact counts don't matter for chunking, and
this keeps ingestion tokenizer-free and offline.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date

from baton.knowledge.articles import Article

TARGET_TOKENS = 300
MAX_TOKENS = 500
MIN_TAIL_TOKENS = 100  # a smaller last chunk is folded into the one before it, if it fits

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text.split()) * 4 / 3)


@dataclass(frozen=True)
class Section:
    path: tuple[str, ...]
    text: str  # includes its own heading line

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class ChunkDraft:
    """A chunk ready to embed and store."""

    article_id: str
    title: str
    category: str
    updated_at: date
    index: int
    heading_path: tuple[str, ...]
    text: str

    def embed_text(self) -> str:
        """What gets embedded and keyword-indexed: the title, the heading path and the text."""
        return f"{self.title}\n{' > '.join(self.heading_path)}\n{self.text}"

    def content_hash(self, embedder_name: str) -> str:
        """Changes whenever the stored embedding would change."""
        raw = "\x1f".join([embedder_name, self.embed_text()])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_sections(article: Article) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (level, heading) of the current section's ancestors
    lines: list[str] = []

    def flush() -> None:
        text = "\n".join(lines).strip()
        # A heading with no body of its own (an H1 right before an H2) adds nothing: its
        # text lives on in the heading paths of the sections under it.
        if any(line.strip() and not _HEADING_RE.match(line) for line in lines):
            path = tuple(h for _, h in stack) or (article.meta.title,)
            sections.append(Section(path=path, text=text))
        lines.clear()

    for line in article.body.split("\n"):
        match = _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
        lines.append(line)
    flush()
    return sections


def _split_large(section: Section, max_tokens: int) -> list[Section]:
    """Split an oversized section at blank lines, repeating its heading on each piece."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section.text) if p.strip()]
    heading = paragraphs[0] if _HEADING_RE.match(paragraphs[0]) else ""
    body = paragraphs[1:] if heading else paragraphs
    pieces: list[Section] = []
    current: list[str] = []
    for paragraph in body:
        candidate = "\n\n".join([heading, *current, paragraph]).strip()
        if current and estimate_tokens(candidate) > max_tokens:
            pieces.append(Section(section.path, "\n\n".join([heading, *current]).strip()))
            current = []
        current.append(paragraph)
    if current:
        pieces.append(Section(section.path, "\n\n".join([heading, *current]).strip()))
    return pieces


def _common_path(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    common = paths[0]
    for path in paths[1:]:
        n = 0
        while n < min(len(common), len(path)) and common[n] == path[n]:
            n += 1
        common = common[:n]
    return common


def chunk_article(
    article: Article, target_tokens: int = TARGET_TOKENS, max_tokens: int = MAX_TOKENS
) -> list[ChunkDraft]:
    sections: list[Section] = []
    for section in split_sections(article):
        if section.tokens > max_tokens:
            sections.extend(_split_large(section, max_tokens))
        else:
            sections.append(section)

    groups: list[list[Section]] = []
    current: list[Section] = []
    for section in sections:
        size = sum(s.tokens for s in current)
        if current and (size >= target_tokens or size + section.tokens > max_tokens):
            groups.append(current)
            current = []
        current.append(section)
    if current:
        groups.append(current)
    if len(groups) > 1:
        tail, previous = groups[-1], groups[-2]
        tail_size = sum(s.tokens for s in tail)
        if (
            tail_size < MIN_TAIL_TOKENS
            and sum(s.tokens for s in previous) + tail_size <= max_tokens
        ):
            groups[-2:] = [previous + tail]

    meta = article.meta
    return [
        ChunkDraft(
            article_id=meta.id,
            title=meta.title,
            category=meta.category,
            updated_at=meta.updated_at,
            index=i,
            heading_path=_common_path([s.path for s in group]) or (meta.title,),
            text="\n\n".join(s.text for s in group),
        )
        for i, group in enumerate(groups)
    ]
