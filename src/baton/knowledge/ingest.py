"""Ingest help-center articles into the `kb_chunks` table.

    uv run baton-kb ingest

Incremental by content hash: every chunk's hash covers what goes into its embedding (title,
heading path, text) plus the embedder's name. On each run, per article:

- a chunk whose hash is already stored keeps its row and embedding; only bookkeeping fields
  (position, title, category, `updated_at`) are refreshed if they changed;
- a chunk with a new hash is embedded and inserted;
- stored chunks whose hash no longer appears are deleted, as are all chunks of articles whose
  file was removed.

So running it twice changes nothing, editing one article re-embeds only that article's changed
chunks, and switching embedders re-embeds everything (every hash changes).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from baton.db.models import KBChunk
from baton.knowledge.articles import load_articles
from baton.knowledge.chunking import ChunkDraft, chunk_article
from baton.llm.embedder import Embedder


@dataclass
class IngestReport:
    articles: int = 0
    chunks: int = 0
    embedded: int = 0  # chunks that were (re-)embedded this run
    unchanged: int = 0
    removed: int = 0
    changed_articles: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.articles} articles, {self.chunks} chunks: {self.embedded} embedded, "
            f"{self.unchanged} unchanged, {self.removed} removed"
        )


def ingest(session: Session, kb_dir: Path, embedder: Embedder) -> IngestReport:
    """Bring `kb_chunks` in line with the articles in `kb_dir`. The caller commits."""
    articles = load_articles(kb_dir)
    drafts = [chunk for article in articles for chunk in chunk_article(article)]
    report = IngestReport(articles=len(articles), chunks=len(drafts))

    stored: dict[str, dict[str, list[KBChunk]]] = defaultdict(lambda: defaultdict(list))
    for row in session.scalars(select(KBChunk)):
        stored[row.article_id][row.content_hash].append(row)

    to_embed: list[tuple[ChunkDraft, str]] = []
    changed: set[str] = set()
    for draft in drafts:
        digest = draft.content_hash(embedder.name)
        matches = stored[draft.article_id][digest]
        if matches:
            _refresh(matches.pop(), draft)
            report.unchanged += 1
        else:
            to_embed.append((draft, digest))
            changed.add(draft.article_id)

    for article_id, by_hash in stored.items():
        for rows in by_hash.values():
            for row in rows:  # left over: the chunk changed or its article is gone
                session.delete(row)
                report.removed += 1
                changed.add(article_id)

    if to_embed:
        vectors = embedder.embed([draft.embed_text() for draft, _ in to_embed])
        for (draft, digest), vector in zip(to_embed, vectors, strict=True):
            session.add(_row(draft, digest, vector, embedder.name))
        report.embedded = len(to_embed)
    session.flush()
    report.changed_articles = sorted(changed)
    return report


def _refresh(row: KBChunk, draft: ChunkDraft) -> None:
    """Update fields that don't affect the embedding (only if they differ, so nothing is
    written on a no-op run)."""
    updates = {
        "chunk_index": draft.index,
        "title": draft.title,
        "category": draft.category,
        "updated_at": draft.updated_at,
    }
    for name, value in updates.items():
        if getattr(row, name) != value:
            setattr(row, name, value)


def _row(draft: ChunkDraft, digest: str, vector: np.ndarray, embedder_name: str) -> KBChunk:
    return KBChunk(
        article_id=draft.article_id,
        chunk_index=draft.index,
        title=draft.title,
        category=draft.category,
        heading_path=list(draft.heading_path),
        text=draft.text,
        embedding=np.asarray(vector, dtype=np.float32).tobytes(),
        embedder=embedder_name,
        content_hash=digest,
        updated_at=draft.updated_at,
    )
