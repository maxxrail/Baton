# Part 03 — Knowledge base (RAG) with citations

## Goal
Answer policy and product questions from Northwind's help center with **grounded, cited** answers, and refuse to guess when the KB doesn't cover it.

## Scope
1. **KB content**: write ~25 help-center articles in `data/kb/*.md` (frontmatter: id, title, category, updated_at): shipping times by region, return policy, warranty, sizing guides, care instructions, loyalty program tiers, subscription FAQ, payment methods, international orders, gift cards, etc. Make them realistic and slightly overlapping, with at least two articles that **conflict on purpose** (old vs. new policy, distinguished by `updated_at`).
2. **Ingestion** (`src/baton/knowledge/ingest.py`): heading-aware chunking (~300–500 tokens, keep the heading path), store chunks in a `kb_chunks` table (article_id, heading_path, text, embedding blob, updated_at, content_hash). Re-ingest is incremental by hash. CLI `uv run baton-kb ingest`.
3. **Retrieval** (`src/baton/knowledge/retrieve.py`): hybrid = BM25 (implement or use `rank-bm25`) + embedding cosine (Embedder interface), reciprocal-rank fusion, top-k=5. Prefer newer `updated_at` when chunks conflict. Optional LLM reranker behind a flag.
4. **Answering**: a `search_knowledge` tool available to every AOP (and the core of `general_question`). The reply must cite sources as `[kb:article_id]`; the channel layer renders these as links.
5. **Grounding check** (`src/baton/knowledge/grounding.py`): after drafting a KB answer, a judge step (LLM, structured output) checks each claim is supported by the retrieved chunks. Unsupported → remove the claim or say "I'm not sure" and offer escalation. Recorded in the trace. Add as an output guardrail.
6. **Retrieval eval**: `evals/retrieval_qa.yaml` with 40 question → expected article_id(s), including 8 questions the KB does **not** answer. `scripts/eval_retrieval.py` reports recall@5, MRR, and abstention accuracy. Runs with HashingEmbedder in CI; report real-embedder numbers in PROGRESS.md if a key is available.

## Acceptance criteria
- [ ] Ingest is idempotent; editing one article re-embeds only its chunks.
- [ ] Retrieval recall@5 ≥ 0.85 on answerable questions with the default embedder (tune chunking/BM25 weights until it is; document what you changed).
- [ ] Conflict test: the newer policy article ranks above the older one.
- [ ] Grounding guardrail test: FakeLLM drafts an answer with an invented claim → claim removed or answer replaced with abstention + escalation offer.
- [ ] Unanswerable questions → agent abstains (no invented policy) in tests.
- [ ] Citations appear in replies and resolve to real article ids.
- [ ] Lint, types, tests clean.

## Verify
```
uv run baton-kb ingest
uv run python -m scripts.eval_retrieval
uv run pytest -q
uv run baton-chat --trace   # ask: "how long does shipping to Canada take?" and "do you price match?" (unanswerable)
```
