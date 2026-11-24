# Baton

**Baton is an open-source support-agent platform where procedures are written in plain English, compiled into validated workflows, executed by an LLM agent whose guardrails are enforced in code, tested with simulated customers before every deploy, and monitored by always-on QA after.**

The name comes from a relay baton: the AI agent and the human agent pass the conversation back
and forth cleanly, and nothing gets dropped. The demo customer is **Northwind Goods**, a
fictional DTC home-goods brand, backed by a mock commerce API that ships in this repo.

> Simulation pass rates will live here once Part 05 lands. Evals are the headline.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/). Works on Windows, macOS and Linux. No API key needed:
without one, Baton uses an offline scripted LLM.

```
uv sync                           # install (Python 3.12 is fetched if needed)
cp .env.example .env              # optional; add ANTHROPIC_API_KEY for real model calls
uv run alembic upgrade head       # create baton.db
uv run baton-kb ingest            # index the help center (data/kb) for grounded answers
uv run python -m scripts.dev      # Baton API on :8000, Northwind mock store on :8100
```

Then open <http://localhost:8000/healthz>, <http://localhost:8000/aops> and
<http://localhost:8100/docs>.
If a port is taken: `uv run python -m scripts.dev --baton-port 8001`.

AOPs (support procedures in plain-English Markdown, see [`aops/README.md`](aops/README.md)):

```
uv run baton-aop validate            # check every AOP; nonzero exit on any error
uv run baton-aop graph return_item   # Mermaid flowchart of one procedure
uv run baton-aop show return_item --prompts
uv run baton-aop compile             # record new AOP versions in the database
```

Help center (Markdown articles in [`data/kb/`](data/kb/README.md)):

```
uv run baton-kb ingest                          # incremental: only changed chunks are re-embedded
uv run baton-kb search "shipping to Canada"     # what the agent would retrieve, with scores
uv run python -m scripts.eval_retrieval         # recall@5, MRR, abstention on evals/retrieval_qa.yaml
```

Chat with the agent in a terminal (needs `ANTHROPIC_API_KEY`; the mock store must be running):

```
uv run python -m scripts.dev      # terminal 1
uv run baton-chat --trace         # terminal 2: route, steps, tools, KB searches, grounding, guardrails
```

Or over HTTP: `POST /conversations`, then `POST /conversations/{id}/messages {"text": "..."}`.
Every turn's trace is at `GET /conversations/{id}/traces`. Replies cite help-center
articles as `[kb:article_id]`, and the response's `citations` list gives each one's title and URL.
Recorded sessions: [`docs/samples/chat-return.md`](docs/samples/chat-return.md) (return flow) and
[`docs/samples/chat-knowledge.md`](docs/samples/chat-knowledge.md) (grounded answers and abstention).

Checks:

```
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pre-commit run --all-files
```

## What's in the box so far

| Piece | Where | Notes |
|---|---|---|
| Baton API | `src/baton/api/` | FastAPI, `/healthz`, JSON logs (structlog) with request IDs and PII redaction |
| AOPs | `aops/`, `src/baton/aop/` | 8 Northwind procedures. Deterministic parser, validator (10 diagnostic codes), compiler with per-step prompts, Mermaid graphs, `baton-aop` CLI, version history |
| Router | `src/baton/aop/router.py` | Embedding pre-filter over AOP triggers, then an LLM classifier with structured output; answers validated in code |
| Agent runtime | `src/baton/runtime/` | Routes to an AOP, follows it step by step (`advance_step` checked against the compiled graph), calls tools, persists state, writes a trace per turn, escalates with a handoff note |
| Guardrails | `src/baton/runtime/guardrails.py` | In code, not the prompt: identity gate, refunds ≤ $100 and ≤ balance (else escalate), per-step tool allow-list, reply checks (no unbacked refund claims, no other customers' orders, PII masking, length caps), escalation on 3 failed verifications / 12 stuck turns / "talk to a human" |
| Tools | `src/baton/tools/` | 13 Northwind tools over HTTP with timeouts, one retry on 5xx, customer-safe error codes, and scoping to the verified customer; plus `search_knowledge`, offered on every step |
| Knowledge (RAG) | `src/baton/knowledge/`, `data/kb/` | 28 help-center articles, heading-aware chunking, incremental ingest, hybrid BM25 + embedding retrieval with reciprocal-rank fusion, newer-policy-wins ranking, optional LLM reranker. **recall@5 0.969, MRR 0.768** on 32 answerable questions (offline embedder) |
| Grounding guardrail | `src/baton/knowledge/grounding.py` | An LLM judge labels each sentence of a help-center answer; code removes unsupported claims, or replaces the reply with "I'm not sure" plus an offer of a teammate. Fails closed. Citations must resolve to retrieved articles |
| Conversations API + CLI | `src/baton/api/conversations.py`, `src/baton/channels/cli.py` | `POST /conversations`, `POST /conversations/{id}/messages`, `GET .../traces`; `baton-chat --trace` |
| AOP API | `src/baton/api/aops.py` | `GET /aops`, `GET /aops/{id}` (graph + Mermaid), `POST /aops/validate` |
| Database | `src/baton/db/`, `migrations/` | SQLAlchemy 2.0 + Alembic: `conversations` (with JSON runtime state), `messages`, `traces`, `aop_versions`, `kb_chunks`. SQLite locally, Postgres via `DATABASE_URL` |
| LLM layer | `src/baton/llm/` | `LLMClient` protocol, `AnthropicClient` (official SDK), `FakeLLM` for offline tests, `HashingEmbedder` (default) and optional `VoyageEmbedder` |
| Northwind mock store | `src/mock_commerce/` | Customers, orders, returns, refunds, subscriptions, loyalty. Rules enforced server-side. See its [README](src/mock_commerce/README.md) |
| Seed data | `data/seed/` | Deterministic, with deliberate tricky cases (expired return window, partial refund, shared phone) |
| CI | `.github/workflows/ci.yml` | ruff, mypy, pytest on Ubuntu and Windows, plus gitleaks |

Configuration is all environment variables. See [`.env.example`](.env.example). `BATON_LLM`
picks `anthropic` or `fake`. It defaults to `anthropic` when `ANTHROPIC_API_KEY` is set and to
`fake` otherwise. `BATON_MODEL` defaults to `claude-sonnet-5-5`.

## Roadmap

| # | Part | Status |
|---|------|--------|
| 00 | Foundation + Northwind mock commerce | done |
| 01 | AOP language + compiler | done |
| 02 | Agent runtime + guardrails | done |
| 03 | Knowledge base (RAG) | done |
| 04 | Channels + human handoff | |
| 05 | Simulations + eval harness | |
| 06 | Watchtower + insights | |
| 07 | Ship it | |
| 08 | *(stretch)* Voice + experiments | |

Details: [docs/ROADMAP.md](docs/ROADMAP.md). Build log, decisions and known gaps:
[docs/PROGRESS.md](docs/PROGRESS.md).

## License

MIT. See [LICENSE](LICENSE).
