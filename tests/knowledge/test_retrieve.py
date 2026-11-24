"""Hybrid retrieval: fusion, the newer-policy rule, coverage/abstention and the reranker."""

from datetime import date

import numpy as np
import pytest

from baton.knowledge.base import KnowledgeBase
from baton.knowledge.rerank import RERANK_TOOL, LLMReranker
from baton.knowledge.retrieve import BM25, Hit, IndexedChunk, RetrievalConfig, Retriever
from baton.llm import FakeLLM, HashingEmbedder, LLMResponse, ToolCall
from tests.knowledge.conftest import REPO


def _articles(retriever: Retriever, query: str, k: int = 5) -> list[str]:
    return [h.chunk.article_id for h in retriever.search(query, top_k=k).hits]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("how long does shipping to Canada take?", "shipping-international"),
        ("can I use fabric softener on the towels?", "care-linens-towels"),
        ("does the gift card expire?", "gift-cards"),
        ("what size flat sheet fits a queen bed?", "sizing-guide-bedding"),
    ],
)
def test_finds_the_answering_article_first(retriever: Retriever, query: str, expected: str) -> None:
    assert _articles(retriever, query)[0] == expected


# ---------------------------------------------------------------- conflicting policies


@pytest.mark.parametrize(
    ("query", "newer", "older"),
    [
        ("how many days do I have to return something?", "return-policy", "return-policy-2024"),
        (
            "is return shipping free? do you include a prepaid label",
            "return-policy",
            "return-policy-2024",
        ),
        ("is shipping free over 50 dollars?", "shipping-costs", "free-shipping-offer-2025"),
        ("what does expedited shipping cost?", "shipping-costs", "free-shipping-offer-2025"),
    ],
)
def test_newer_policy_ranks_above_the_older_one(
    retriever: Retriever, query: str, newer: str, older: str
) -> None:
    found = _articles(retriever, query, k=10)
    assert newer in found
    if older in found:
        assert found.index(newer) < found.index(older)


def test_the_old_article_wins_on_fusion_alone_so_the_rule_is_doing_the_work(
    retriever: Retriever,
) -> None:
    # Without the recency rule the 2025 "$50" article is the best keyword match; the rule must
    # be what puts the current policy first, and the swap is reported.
    query = "is shipping free over 50 dollars?"
    no_rule = Retriever(
        retriever.chunks,
        retriever.vectors,
        retriever.embedder,
        config=RetrievalConfig(same_topic_cosine=1.1),  # nothing is "same topic"
    )
    assert _articles(no_rule, query)[0] == "free-shipping-offer-2025"
    result = retriever.search(query)
    assert result.hits[0].chunk.article_id == "shipping-costs"
    assert ("shipping-costs", "free-shipping-offer-2025") in result.recency_swaps


def test_recency_only_reorders_articles_on_the_same_topic() -> None:
    emb = HashingEmbedder()

    def chunk(i: int, article: str, category: str, text: str, day: int) -> IndexedChunk:
        return IndexedChunk(i, article, article, category, (article,), text, date(2026, 1, day))

    chunks = [
        chunk(1, "old-returns", "returns", "returns accepted within 60 days of delivery", 1),
        chunk(2, "new-returns", "returns", "returns accepted within 30 days of delivery", 20),
        chunk(3, "new-gifts", "payments", "returns of gift cards are not accepted", 25),
    ]
    vectors = emb.embed([c.text for c in chunks])
    retriever = Retriever(chunks, vectors, emb)
    order = _articles(retriever, "returns accepted within 60 days of delivery")
    assert order.index("new-returns") < order.index("old-returns")
    # new-gifts is newer but a different category: it may rank anywhere, but it isn't
    # treated as replacing the returns policy.
    assert all(pair[1] == "old-returns" for pair in retriever.search("returns").recency_swaps)


# ---------------------------------------------------------------- coverage / abstention


@pytest.mark.parametrize(
    "query",
    [
        "do you price match if I find it cheaper somewhere else?",
        "are you hiring? I'd like to apply for a job",
        "do you have wholesale pricing for restaurants?",
    ],
)
def test_questions_the_help_center_doesnt_answer_are_not_covered(
    retriever: Retriever, query: str
) -> None:
    result = retriever.search(query)
    assert not result.covered
    assert result.coverage < retriever.config.coverage_threshold


def test_answerable_questions_are_covered(retriever: Retriever) -> None:
    result = retriever.search("how long does shipping to Canada take?")
    assert result.covered
    assert result.coverage == 1.0


def test_empty_query_or_index() -> None:
    emb = HashingEmbedder()
    empty = Retriever([], np.zeros((0, emb.dim), dtype=np.float32), emb)
    assert empty.search("anything").hits == []
    assert not empty.search("anything").covered


def test_bm25_prefers_rare_matching_terms() -> None:
    bm25 = BM25([["ship", "canada"], ["ship", "ship", "us"], ["gift", "card"]], k1=1.2, b=0.75)
    scores = bm25.scores(["canada", "ship"])
    assert scores[0] > scores[1] > scores[2] == 0.0
    assert bm25.term_weight("never-seen") > bm25.term_weight("ship")


def test_knowledge_base_loads_lazily_and_resolves_titles(knowledge: KnowledgeBase) -> None:
    assert len(knowledge) >= 28
    assert knowledge.title("gift-cards") == "Gift cards"
    assert knowledge.title("nope") is None
    assert knowledge.url("gift-cards").endswith("/articles/gift-cards")
    hit = knowledge.search("gift card expiry").hits[0]
    assert knowledge.chunk(hit.chunk.id) == hit.chunk


# ---------------------------------------------------------------- LLM reranker


def _rerank_llm(ranking: object) -> FakeLLM:
    return FakeLLM(
        [LLMResponse(tool_calls=[ToolCall(id="r", name=RERANK_TOOL, input={"ranking": ranking})])]
    )


def _with_reranker(retriever: Retriever, llm: FakeLLM) -> Retriever:
    return Retriever(
        retriever.chunks,
        retriever.vectors,
        retriever.embedder,
        reranker=LLMReranker(llm, REPO / "prompts"),
    )


def test_reranker_reorders_candidates(retriever: Retriever) -> None:
    query = "how long does shipping to Canada take?"
    before = retriever.search(query, top_k=10).hits
    llm = _rerank_llm([3, 1])
    result = _with_reranker(retriever, llm).search(query, top_k=10)
    assert result.reranked
    assert result.hits[0].chunk.id == before[2].chunk.id
    assert result.hits[1].chunk.id == before[0].chunk.id
    system, messages, tools = llm.calls[0]
    assert "rank" in system.lower()
    assert tools is not None
    assert tools[0].name == RERANK_TOOL
    assert 'updated="' in str(messages[0].content)  # dates are shown so newer can win


@pytest.mark.parametrize("ranking", ["not a list", [], [99, -1, "x"]])
def test_unusable_rerank_output_keeps_the_fused_order(
    retriever: Retriever, ranking: object
) -> None:
    query = "how long does shipping to Canada take?"
    before = [h.chunk.id for h in retriever.search(query).hits]
    result = _with_reranker(retriever, _rerank_llm(ranking)).search(query)
    assert not result.reranked
    assert [h.chunk.id for h in result.hits] == before


def test_reranker_failure_keeps_the_fused_order(retriever: Retriever) -> None:
    def boom(*args: object) -> LLMResponse:
        raise RuntimeError("model down")

    result = _with_reranker(retriever, FakeLLM(boom)).search("gift card expiry")
    assert not result.reranked
    assert result.hits


def test_recency_rule_runs_after_the_reranker(retriever: Retriever) -> None:
    query = "is shipping free over 50 dollars?"
    candidates: list[Hit] = retriever.search(query, top_k=10).hits
    old = next(
        i for i, h in enumerate(candidates, 1) if h.chunk.article_id == "free-shipping-offer-2025"
    )
    result = _with_reranker(retriever, _rerank_llm([old])).search(query)
    found = [h.chunk.article_id for h in result.hits]
    assert found.index("shipping-costs") < found.index("free-shipping-offer-2025")
