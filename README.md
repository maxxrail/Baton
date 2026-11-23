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
uv run python -m scripts.dev      # Baton API on :8000, Northwind mock store on :8100
```

Then open <http://localhost:8000/healthz> and <http://localhost:8100/docs>.
If a port is taken: `uv run python -m scripts.dev --baton-port 8001`.

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
| Database | `src/baton/db/`, `migrations/` | SQLAlchemy 2.0 + Alembic: `conversations`, `messages`, `traces`. SQLite locally, Postgres via `DATABASE_URL` |
| LLM layer | `src/baton/llm/` | `LLMClient` protocol, `AnthropicClient` (official SDK), `FakeLLM` for offline tests, `HashingEmbedder` |
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
| 01 | AOP language + compiler | |
| 02 | Agent runtime + guardrails | |
| 03 | Knowledge base (RAG) | |
| 04 | Channels + human handoff | |
| 05 | Simulations + eval harness | |
| 06 | Watchtower + insights | |
| 07 | Ship it | |
| 08 | *(stretch)* Voice + experiments | |

Details: [docs/ROADMAP.md](docs/ROADMAP.md). Build log, decisions and known gaps:
[docs/PROGRESS.md](docs/PROGRESS.md).

## License

MIT. See [LICENSE](LICENSE).
