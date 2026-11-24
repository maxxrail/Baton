"""Hybrid retrieval over the help center: BM25 + embedding cosine, fused by reciprocal rank.

`Retriever.search(query)`:

1. **BM25** over each chunk's title, heading path and text (`baton.knowledge.text.analyze`
   terms; the title and heading path are counted twice, because they say what the chunk is
   about).
2. **Cosine** between the query embedding and each chunk embedding (the `Embedder` interface).
3. **Reciprocal-rank fusion**: `score = w_bm25 / (k + rank_bm25) + w_vec / (k + rank_vec)`.
   RRF only uses ranks, so the two very different score scales never need calibrating.
4. **Optional LLM rerank** of the top candidates (`BATON_KB_RERANK=true`, see `rerank.py`).
5. **Newer wins**: if two candidates are on the same topic (same category, and the articles'
   closest chunks have cosine >= `same_topic_cosine`), the older one is moved to just below
   the newer one. Demoting the old article (rather than promoting the new one) keeps the
   relevance order for everything else, and outdated text tends to fall out of the top k.
   This runs last, so neither fusion nor the reranker can put an outdated policy first.
6. **Coverage** for abstention: the share of the query's terms (IDF-weighted, terms the KB
   has never seen count at full weight) found in the best of the top 3 chunks. Below
   `coverage_threshold` the result is marked `covered=False`: the help center probably doesn't
   answer this, and the agent should say so instead of stretching a near miss.

The whole index lives in memory: a help center is hundreds of chunks, not millions.
"""

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from baton.db.models import KBChunk
from baton.knowledge.text import analyze
from baton.llm.embedder import Embedder, Vector


class KnowledgeIndexError(RuntimeError):
    """The stored index can't be used (e.g. it was built with a different embedder)."""


@dataclass(frozen=True)
class IndexedChunk:
    id: int
    article_id: str
    title: str
    category: str
    heading_path: tuple[str, ...]
    text: str
    updated_at: date

    @property
    def citation(self) -> str:
        return f"[kb:{self.article_id}]"

    @property
    def section(self) -> str:
        return " > ".join(self.heading_path)

    def index_text(self) -> str:
        heading = " ".join(self.heading_path)
        return f"{self.title} {heading} {self.title} {heading} {self.text}"


@dataclass(frozen=True)
class Hit:
    chunk: IndexedChunk
    score: float
    bm25_rank: int | None  # None: no query term occurs in the chunk
    vector_rank: int
    cosine: float


@dataclass(frozen=True)
class SearchResult:
    query: str
    hits: list[Hit]
    covered: bool
    coverage: float
    reranked: bool = False
    # (newer article, older article) pairs the recency rule reordered.
    recency_swaps: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalConfig:
    top_k: int = 5
    rrf_k: int = 60
    # BM25 counts double: with the offline hashing embedder, keyword match is the stronger
    # signal (alone: recall@5 0.969 vs 0.750 on evals/retrieval_qa.yaml). See PROGRESS.md.
    bm25_weight: float = 2.0
    vector_weight: float = 1.0
    k1: float = 1.2
    b: float = 0.75
    # Two articles in the same category whose closest chunks are at least this similar are
    # "on the same topic" for the newer-wins rule. The deliberate conflict pairs measure
    # 0.41-0.44 with the hashing embedder; the closest non-conflicting pair is 0.37.
    same_topic_cosine: float = 0.40
    # Below this query-term coverage the result is marked covered=False (see `_coverage`).
    coverage_threshold: float = 0.33
    candidates: int = 10  # considered for rerank and recency before cutting to top_k


class Reranker(Protocol):
    def rerank(self, query: str, hits: list[Hit]) -> list[Hit] | None:
        """A new order for `hits`, or None to keep the current one."""
        ...


class BM25:
    """Okapi BM25 over pre-analyzed documents."""

    def __init__(self, docs: Sequence[list[str]], k1: float, b: float) -> None:
        self.k1, self.b = k1, b
        self.tfs = [Counter(doc) for doc in docs]
        self.lengths = [len(doc) for doc in docs]
        self.avg_len = (sum(self.lengths) / len(docs)) if docs else 0.0
        n = len(docs)
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        # BM25+ style idf, always positive.
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.max_idf = math.log(1 + (n + 0.5) / 0.5) if n else 1.0

    def scores(self, query_terms: list[str]) -> list[float]:
        out = []
        for tf, length in zip(self.tfs, self.lengths, strict=True):
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * length / (self.avg_len or 1.0))
            for term in set(query_terms):
                f = tf.get(term, 0)
                if f:
                    score += self.idf[term] * f * (self.k1 + 1) / (f + norm)
            out.append(score)
        return out

    def term_weight(self, term: str) -> float:
        return self.idf.get(term, self.max_idf)


def _ranks(scores: Sequence[float], *, require_positive: bool) -> list[int | None]:
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    ranks: list[int | None] = [None] * len(scores)
    for rank, i in enumerate(order, start=1):
        if require_positive and scores[i] <= 0:
            continue
        ranks[i] = rank
    return ranks


class Retriever:
    def __init__(
        self,
        chunks: Sequence[IndexedChunk],
        vectors: Vector,
        embedder: Embedder,
        *,
        config: RetrievalConfig | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must line up")
        self.chunks = list(chunks)
        self.vectors = vectors
        self.embedder = embedder
        self.config = config or RetrievalConfig()
        self.reranker = reranker
        self.terms = [set(analyze(c.index_text())) for c in self.chunks]
        self.bm25 = BM25(
            [analyze(c.index_text()) for c in self.chunks], self.config.k1, self.config.b
        )
        self._by_id = {c.id: i for i, c in enumerate(self.chunks)}
        self._topic_cache: dict[tuple[str, str], bool] = {}

    @classmethod
    def load(
        cls,
        session: Session,
        embedder: Embedder,
        *,
        config: RetrievalConfig | None = None,
        reranker: Reranker | None = None,
    ) -> "Retriever":
        rows = list(
            session.scalars(select(KBChunk).order_by(KBChunk.article_id, KBChunk.chunk_index))
        )
        stale = sorted({r.embedder for r in rows} - {embedder.name})
        if stale:
            raise KnowledgeIndexError(
                f"the knowledge base was embedded with {', '.join(stale)} but the configured "
                f"embedder is {embedder.name}: run `uv run baton-kb ingest`"
            )
        chunks = [
            IndexedChunk(
                id=r.id,
                article_id=r.article_id,
                title=r.title,
                category=r.category,
                heading_path=tuple(r.heading_path),
                text=r.text,
                updated_at=r.updated_at,
            )
            for r in rows
        ]
        if rows:
            vectors = np.stack([np.frombuffer(r.embedding, dtype=np.float32) for r in rows])
        else:
            vectors = np.zeros((0, embedder.dim), dtype=np.float32)
        return cls(chunks, vectors, embedder, config=config, reranker=reranker)

    def __len__(self) -> int:
        return len(self.chunks)

    def get(self, chunk_id: int) -> IndexedChunk | None:
        i = self._by_id.get(chunk_id)
        return self.chunks[i] if i is not None else None

    @property
    def article_ids(self) -> set[str]:
        return {c.article_id for c in self.chunks}

    def search(self, query: str, top_k: int | None = None) -> SearchResult:
        cfg = self.config
        top_k = top_k or cfg.top_k
        terms = analyze(query)
        if not self.chunks or not query.strip():
            return SearchResult(query=query, hits=[], covered=False, coverage=0.0)

        bm25_ranks = _ranks(self.bm25.scores(terms), require_positive=True)
        cosines = self.vectors @ self.embedder.embed([query])[0]
        vector_ranks = _ranks([float(c) for c in cosines], require_positive=False)

        hits = []
        for i, chunk in enumerate(self.chunks):
            b_rank, v_rank = bm25_ranks[i], vector_ranks[i]
            assert v_rank is not None
            score = cfg.vector_weight / (cfg.rrf_k + v_rank)
            if b_rank is not None:
                score += cfg.bm25_weight / (cfg.rrf_k + b_rank)
            hits.append(Hit(chunk, score, b_rank, v_rank, float(cosines[i])))
        hits.sort(key=lambda h: (-h.score, h.chunk.id))
        candidates = hits[: max(cfg.candidates, top_k)]

        reranked = False
        if self.reranker is not None:
            new_order = self.reranker.rerank(query, candidates)
            if new_order is not None:
                candidates, reranked = new_order, True

        candidates, swaps = self._prefer_newer(candidates)
        top = candidates[:top_k]
        coverage = self._coverage(terms, top[:3])
        return SearchResult(
            query=query,
            hits=top,
            covered=coverage >= cfg.coverage_threshold,
            coverage=round(coverage, 3),
            reranked=reranked,
            recency_swaps=swaps,
        )

    # ------------------------------------------------------------------ helpers

    def _same_topic(self, a: IndexedChunk, b: IndexedChunk) -> bool:
        """Same category, and the two articles' closest chunks are similar enough. Compared
        per article, so every chunk of a newer policy outranks every chunk of the old one."""
        if a.article_id == b.article_id or a.category != b.category:
            return False
        key = (min(a.article_id, b.article_id), max(a.article_id, b.article_id))
        if key not in self._topic_cache:
            rows_a = [i for i, c in enumerate(self.chunks) if c.article_id == a.article_id]
            rows_b = [i for i, c in enumerate(self.chunks) if c.article_id == b.article_id]
            closest = float((self.vectors[rows_a] @ self.vectors[rows_b].T).max())
            self._topic_cache[key] = closest >= self.config.same_topic_cosine
        return self._topic_cache[key]

    def _prefer_newer(self, hits: list[Hit]) -> tuple[list[Hit], list[tuple[str, str]]]:
        """Move each hit below every newer hit on the same topic."""
        out = list(hits)
        swaps: list[tuple[str, str]] = []
        for older in hits:
            newer = [
                h
                for h in out
                if h.chunk.updated_at > older.chunk.updated_at
                and self._same_topic(h.chunk, older.chunk)
            ]
            if not newer:
                continue
            lowest_newer = max(out.index(h) for h in newer)
            if out.index(older) < lowest_newer:
                out.remove(older)
                out.insert(lowest_newer, older)  # the list shifted up by one: lands just below
                swaps.append((out[lowest_newer - 1].chunk.article_id, older.chunk.article_id))
        return out, swaps

    def _coverage(self, terms: list[str], hits: list[Hit]) -> float:
        unique = set(terms)
        if not unique or not hits:
            return 0.0
        total = sum(self.bm25.term_weight(t) for t in unique)
        best = 0.0
        for hit in hits:
            have = self.terms[self._by_id[hit.chunk.id]]
            found = sum(self.bm25.term_weight(t) for t in unique if t in have)
            best = max(best, found / total)
        return best
