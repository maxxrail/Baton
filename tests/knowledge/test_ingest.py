"""Ingestion is idempotent and incremental: editing one article re-embeds only its chunks."""

import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from baton.db import KBChunk
from baton.knowledge.ingest import ingest
from baton.knowledge.retrieve import KnowledgeIndexError, Retriever
from baton.llm import HashingEmbedder
from baton.llm.embedder import Vector
from tests.knowledge.conftest import KB_DIR, make_kb_sessions


class CountingEmbedder:
    """HashingEmbedder that records every text it embeds."""

    def __init__(self, dim: int = 512) -> None:
        self.inner = HashingEmbedder(dim)
        self.dim = dim
        self.name = self.inner.name
        self.embedded: list[str] = []

    def embed(self, texts: list[str]) -> Vector:
        self.embedded.extend(texts)
        return self.inner.embed(texts)


@pytest.fixture
def kb_copy(tmp_path: Path) -> Path:
    target = tmp_path / "kb"
    shutil.copytree(KB_DIR, target)
    return target


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    return make_kb_sessions(tmp_path / "baton.db")


def _run(sessions: sessionmaker[Session], kb: Path, embedder: CountingEmbedder):  # type: ignore[no-untyped-def]
    with sessions() as session:
        report = ingest(session, kb, embedder)
        session.commit()
    return report


def _rows(sessions: sessionmaker[Session]) -> dict[int, tuple[str, str, bytes]]:
    with sessions() as session:
        return {
            r.id: (r.article_id, r.content_hash, r.embedding)
            for r in session.scalars(select(KBChunk))
        }


def test_first_ingest_embeds_every_chunk(sessions: sessionmaker[Session], kb_copy: Path) -> None:
    embedder = CountingEmbedder()
    report = _run(sessions, kb_copy, embedder)
    assert report.articles == 28
    assert report.chunks == report.embedded == len(embedder.embedded) == len(_rows(sessions))
    assert report.chunks > report.articles  # some long articles have several chunks
    with sessions() as session:
        row = session.scalars(select(KBChunk).where(KBChunk.article_id == "return-policy")).first()
        assert row is not None
        assert row.heading_path[0] == "Return policy"
        assert row.updated_at == date(2026, 2, 1)
        assert np.frombuffer(row.embedding, dtype=np.float32).shape == (512,)


def test_second_ingest_changes_nothing(sessions: sessionmaker[Session], kb_copy: Path) -> None:
    _run(sessions, kb_copy, CountingEmbedder())
    before = _rows(sessions)
    embedder = CountingEmbedder()
    report = _run(sessions, kb_copy, embedder)
    assert embedder.embedded == []
    assert report.embedded == report.removed == 0
    assert report.unchanged == report.chunks
    assert report.changed_articles == []
    assert _rows(sessions) == before  # same ids, hashes and vectors


def test_editing_one_article_reembeds_only_its_chunks(
    sessions: sessionmaker[Session], kb_copy: Path
) -> None:
    _run(sessions, kb_copy, CountingEmbedder())
    before = _rows(sessions)
    path = kb_copy / "gift-cards.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("$25 to $500", "$10 to $1,000"), encoding="utf-8"
    )

    embedder = CountingEmbedder()
    report = _run(sessions, kb_copy, embedder)

    assert report.changed_articles == ["gift-cards"]
    assert report.embedded == report.removed == 1
    assert len(embedder.embedded) == 1
    assert "$10 to $1,000" in embedder.embedded[0]
    after = _rows(sessions)
    untouched = {k: v for k, v in before.items() if v[0] != "gift-cards"}
    assert {k: v for k, v in after.items() if v[0] != "gift-cards"} == untouched


def test_only_the_changed_chunk_of_a_multi_chunk_article_is_reembedded(
    sessions: sessionmaker[Session], kb_copy: Path
) -> None:
    _run(sessions, kb_copy, CountingEmbedder())
    path = kb_copy / "return-policy.md"
    text = path.read_text(encoding="utf-8")
    assert "are donated." in text
    path.write_text(text.replace("are donated.", "are recycled."), encoding="utf-8")
    embedder = CountingEmbedder()
    report = _run(sessions, kb_copy, embedder)
    assert report.embedded == 1  # return-policy has 2 chunks; only the edited one changes
    assert "recycled" in embedder.embedded[0]


def test_a_date_only_change_updates_the_row_without_reembedding(
    sessions: sessionmaker[Session], kb_copy: Path
) -> None:
    _run(sessions, kb_copy, CountingEmbedder())
    path = kb_copy / "gift-cards.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("2025-10-15", "2026-07-01"), encoding="utf-8"
    )
    embedder = CountingEmbedder()
    report = _run(sessions, kb_copy, embedder)
    assert embedder.embedded == []
    assert report.embedded == 0
    with sessions() as session:
        row = session.scalars(select(KBChunk).where(KBChunk.article_id == "gift-cards")).one()
        assert row.updated_at == date(2026, 7, 1)


def test_deleted_articles_are_removed(sessions: sessionmaker[Session], kb_copy: Path) -> None:
    _run(sessions, kb_copy, CountingEmbedder())
    (kb_copy / "gift-cards.md").unlink()
    report = _run(sessions, kb_copy, CountingEmbedder())
    assert report.removed == 1
    assert report.changed_articles == ["gift-cards"]
    assert "gift-cards" not in {v[0] for v in _rows(sessions).values()}


def test_switching_embedders_reembeds_everything_and_old_index_is_refused(
    sessions: sessionmaker[Session], kb_copy: Path
) -> None:
    _run(sessions, kb_copy, CountingEmbedder(512))
    with sessions() as session, pytest.raises(KnowledgeIndexError, match="baton-kb ingest"):
        Retriever.load(session, HashingEmbedder(256))
    report = _run(sessions, kb_copy, CountingEmbedder(256))
    assert report.embedded == report.chunks
    assert report.removed == report.chunks
    with sessions() as session:
        assert len(Retriever.load(session, HashingEmbedder(256))) == report.chunks
