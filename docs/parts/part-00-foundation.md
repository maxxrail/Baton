# Part 00 — Foundation + Northwind mock commerce

## Goal
A clean, runnable repo skeleton with CI, a database layer, LLM interfaces (with a fake for tests), and a mock commerce API for the fictional brand **Northwind Goods** that later parts will call as "tools".

## Scope
1. **Project setup**
   - `pyproject.toml` managed by uv; Python 3.12. Deps: fastapi, uvicorn[standard], pydantic>=2, pydantic-settings, sqlalchemy>=2, alembic, httpx, anthropic, numpy, pyyaml, structlog. Dev: pytest, pytest-asyncio, ruff, mypy, respx, pre-commit.
   - `src/baton/config.py`: `Settings` (pydantic-settings) reading `.env`: `DATABASE_URL` (default `sqlite:///./baton.db`), `ANTHROPIC_API_KEY`, `BATON_MODEL` (default a current Claude Sonnet model), `BATON_LLM` (`anthropic` | `fake`, default `fake` if no key), `MOCK_COMMERCE_URL`.
   - src layout: package code in `src/baton/` and `src/mock_commerce/`, configured in `pyproject.toml` so `uv sync` installs it editable. Create the empty top-level folders from CLAUDE.md's layout (`aops/`, `prompts/`, `data/kb/`, `data/seed/`, `evals/sims/`, `ops/`, `scripts/`) with a short README.md in each saying what goes there.
   - The repo already contains `.gitignore`, `.gitattributes`, `.pre-commit-config.yaml`, `.gitleaksignore`, `.dockerignore`, `CHANGELOG.md`, `LICENSE` and `.env.example`. Keep them, and update `.env.example` with every setting you add.
   - Run `uv run pre-commit install` and make sure `uv run pre-commit run --all-files` passes.
   - Ruff + mypy config in `pyproject.toml`.
   - GitHub Actions `.github/workflows/ci.yml`: uv install, ruff, mypy, pytest on ubuntu-latest **and** windows-latest, plus a gitleaks job. Add `.github/pull_request_template.md` (what/why/how tested).
2. **DB layer** (`src/baton/db/`)
   - SQLAlchemy 2.0 declarative base, session factory, Alembic configured with `alembic.ini` at the repo root and scripts in `migrations/`, with an initial migration.
   - Tables for now: `conversations` (id, channel, customer_ref, status: open|escalated|resolved, created_at, updated_at), `messages` (id, conversation_id, role: customer|agent|human_agent|system, content, created_at), `traces` (id, conversation_id, message_id nullable, payload JSON, created_at).
3. **LLM layer** (`src/baton/llm/`)
   - `LLMClient` protocol: `complete(system: str, messages: list[Msg], tools: list[ToolSpec] | None, temperature: float, max_tokens: int) -> LLMResponse` where `LLMResponse` has `text`, `tool_calls: list[ToolCall]`, `usage` (input/output tokens), `stop_reason`.
   - `AnthropicClient` implementing it with the official SDK (tool-use format mapped to our types).
   - `FakeLLM`: scripted responses (a queue of `LLMResponse`s or a callable `(system, messages) -> LLMResponse`) for tests. Raises a clear error if it runs out of script.
   - `Embedder` protocol + `HashingEmbedder` (deterministic, numpy, no network).
   - `get_llm()` factory driven by settings.
4. **Northwind mock commerce** (`src/mock_commerce/`)
   - Separate FastAPI app on its own port (default 8100), SQLite-backed (`mock_commerce.db`), seeded by `src/mock_commerce/seed.py` with deterministic data (fixed random seed): ~50 customers, ~200 orders across statuses (processing, shipped, delivered, returned, cancelled), line items, ~20 subscriptions, loyalty point balances, and a few tricky cases (order delivered 45 days ago — outside 30-day return window; order with a partial refund already; customer with two accounts on the same phone).
   - Endpoints (JSON, typed with Pydantic): `GET /customers/lookup?email=|phone=`, `POST /customers/{id}/verify` (body: order_number + zip → bool), `GET /customers/{id}/orders`, `GET /orders/{order_number}`, `POST /orders/{order_number}/cancel` (only if processing), `POST /orders/{order_number}/return` (creates RMA, enforces 30-day window), `POST /orders/{order_number}/refund` (amount; never more than order total minus prior refunds), `POST /orders/{order_number}/address` (only if processing), `GET /subscriptions/{customer_id}`, `POST /subscriptions/{id}/pause|resume|cancel`, `GET /customers/{id}/loyalty`.
   - Business rules enforced server-side with 4xx + machine-readable error codes (e.g. `RETURN_WINDOW_EXPIRED`, `REFUND_EXCEEDS_BALANCE`).
   - `src/mock_commerce/README.md` documenting the data and the tricky cases.
5. **API skeleton** (`src/baton/api/`)
   - FastAPI app with `GET /healthz`, structured logging (structlog, JSON), request IDs.
6. **Dev runner**: `scripts/dev.py` starts both apps (baton API on 8000, mock commerce on 8100) with uvicorn; works on Windows.
7. **README.md** first version: one-paragraph pitch (from ROADMAP), quickstart, status table linking PROGRESS.md.

## Acceptance criteria
- [ ] `uv sync` then `uv run pytest -q` passes with no network and no API key.
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` are clean.
- [ ] `uv run python -m scripts.dev` serves `/healthz` on :8000 and mock commerce on :8100.
- [ ] Seed is deterministic (same IDs/order numbers every run) and the tricky cases exist; tests assert them.
- [ ] Mock commerce rules tested: cancel non-processing order → 409; return outside window → `RETURN_WINDOW_EXPIRED`; over-refund → `REFUND_EXCEEDS_BALANCE`; verify with wrong zip → false.
- [ ] `FakeLLM` and `HashingEmbedder` have unit tests; `AnthropicClient` has a test using a mocked HTTP layer (respx) or SDK stub, no real calls.
- [ ] Alembic `upgrade head` works on a fresh SQLite file.
- [ ] CI workflow file exists and would run lint, types, tests on Linux + Windows, and gitleaks.
- [ ] `uv run pre-commit run --all-files` passes.
- [ ] Seed data lives in `data/seed/` (JSON/CSV), loaded by `src/mock_commerce/seed.py`.
- [ ] CHANGELOG.md has a Part 00 entry.

## Verify
```
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check . && uv run mypy src
uv run alembic upgrade head
uv run pre-commit run --all-files
uv run python -m scripts.dev   # then open http://localhost:8000/healthz and http://localhost:8100/docs
```
