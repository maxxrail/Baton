# CLAUDE.md — Baton

Instructions for Claude Code working in this repo. Read this file, then `docs/ROADMAP.md`, then the part file you've been asked to build.

## What this project is
*Name:* a relay baton. The AI agent and the human agent pass the conversation back and forth cleanly and nothing gets dropped.

Baton is an open-source, production-style **AI customer-support agent platform**, modeled on how Decagon builds agents:
- **AOPs (Agent Operating Procedures):** support workflows written in natural-language Markdown, compiled into a validated workflow graph that the agent runtime executes.
- **Agent runtime:** an LLM tool-calling loop that follows AOPs, with **guardrails enforced in code** (not in the prompt).
- **Knowledge (RAG):** grounded answers from a help center with citations.
- **Channels:** web chat widget, email, SMS. Human handoff inbox.
- **Simulations:** LLM-generated customer personas run full conversations against the agent; an LLM judge grades pass/fail with a rationale. Runs in CI as a regression gate.
- **Watchtower:** always-on QA that scores live conversations and flags policy violations; an insights dashboard.

The demo customer is a fictional DTC brand, **Northwind Goods** (orders, returns, refunds, subscriptions, loyalty points), backed by a mock commerce API that ships in this repo.

## How we work
- Build **one part at a time**. Each part lives in `docs/parts/part-NN-*.md` and has: Goal, Scope, Files, Acceptance criteria, Verify. A part is not done until every box under **Acceptance criteria** is true and every command under **Verify** passes.
- Start each part by making a short task list from the part file. End each part by:
  1. running the full test suite and linter,
  2. updating `docs/PROGRESS.md` (what was built, decisions made, known gaps),
  3. updating `README.md` if user-facing behavior changed,
  4. adding a `CHANGELOG.md` entry for the part,
  5. making a commit `feat(part-NN): <summary>` (or tell the user to commit with GitHub Desktop if git isn't set up).
- Don't start the next part unless asked. Don't silently cut scope — if something in a part can't be done, write it in `docs/PROGRESS.md` under "Known gaps" and tell the user.
- If the part file conflicts with code that already exists, the existing code's interfaces win unless the part explicitly says to change them. Note the deviation in PROGRESS.md.

## Stack (don't change without being asked)
- Python 3.12, **uv** for deps (`uv add`, `uv run`). FastAPI, Pydantic v2, SQLAlchemy 2.0 + Alembic.
- DB: **SQLite** for local dev and tests (zero setup on Windows); Postgres in production via `DATABASE_URL`. Never use SQLite-only or Postgres-only SQL.
- LLM: Anthropic Python SDK behind our own `LLMClient` interface (`src/baton/llm/`). Model name comes from `BATON_MODEL` env var. **Every component that calls an LLM must also work with `FakeLLM`** so tests run offline and deterministically.
- Embeddings: behind an `Embedder` interface; default is a local hashing/TF-IDF embedder so nothing needs an API key; optional real provider via env.
- Frontend (`web/`): React + Vite + TypeScript + Tailwind. One app with two routes: `/widget` (customer chat) and `/console` (ops: inbox, AOP viewer, simulations, Watchtower).
- Tests: pytest (+ pytest-asyncio), Vitest for web. Lint/format: ruff. Types: mypy on `src/` (strict-ish; no `Any` leaks in public interfaces).
- The dev machine is **Windows**. Use `pathlib`, no bash-only scripts as the only way to run things — provide `uv run` commands or a `Makefile` + equivalent `scripts/*.py`.

## Repo layout (target, same conventions as my frontdesk-agent repo)
```
.claude/          Claude Code commands, agents, hooks, settings
.github/          CI workflows, PR template
src/
  baton/
    api/          FastAPI app, routers, deps
    aop/          AOP parser, compiler, validator, router
    runtime/      agent loop, state, guardrails, escalation, traces, redact
    tools/        tool registry + Northwind tool implementations
    knowledge/    ingestion, chunking, retrieval, grounding
    channels/     web, email, sms adapters
    sim/          personas, simulator, judge, reports
    watchtower/   live scoring, flags, insights
    llm/          LLMClient, AnthropicClient, FakeLLM, Embedder
    db/           SQLAlchemy models + session
    config.py
  mock_commerce/  Northwind Goods mock store API (separate FastAPI app) + seed.py
aops/             *.md AOP files (the "customer's" procedures)
prompts/          versioned prompt files (brand.md, router.md, judge.md, ...); version = filename + hash
data/
  kb/             help-center articles (*.md)
  seed/           fixed seed data for mock commerce
evals/
  sims/           simulation suites (*.yaml), sims/generated/ for drafts
  reports/        simulation run output (gitignored)
  router_cases.yaml, retrieval_qa.yaml, judge_calibration.yaml, thresholds.yaml
migrations/       Alembic (alembic.ini at repo root points here)
ops/              deploy configs, dashboards, runbooks (Part 07)
scripts/          dev runner, traffic generator, eval scripts (all Python, Windows-safe)
web/              React app (widget + console)
tests/            mirrors src/baton/
docs/             ROADMAP, PROGRESS, ARCHITECTURE, EVAL_REPORT, parts/
CHANGELOG.md      one entry per part (Keep a Changelog format)
```
Package is installed in editable mode by uv (src layout), so imports are `from baton.runtime import ...`.

## Engineering rules
- Guardrails live in code. If a policy matters (refund limits, identity verification before account changes, no PII in logs), it is enforced by the runtime or the tool, and has a test proving the LLM can't talk its way past it.
- Every agent turn writes a **trace** (prompt version, AOP + step, tool calls with args/results, guardrail decisions, latency, tokens). Traces are how we debug, and how Simulations and Watchtower explain results.
- Tools are typed: Pydantic input/output models, a description, a `side_effect: bool` flag, and `requires_verification: bool`.
- Secrets only from env (`.env`, never committed). Keep `.env.example` current. pre-commit runs ruff + gitleaks; don't bypass it with `--no-verify`.
- Prompts live in `prompts/`, never as long strings inside Python. Code loads them by name and records the version in traces.
- No PII in logs: mask emails, phones, card-like numbers via `src/baton/runtime/redact.py`.
- Prefer small, boring, readable code over clever abstractions. This repo is a portfolio piece: a reviewer at an AI company should be able to read any file and understand it.

## Claude Code setup (`.claude/`)
- Slash commands: `/build-part NN`, `/continue-part NN`, `/verify`, `/review`, `/red-team [aop]`, `/sim [filter]`, `/status`, `/interview-prep NN`.
- Subagents: `code-reviewer` (reviews the diff against this file), `guardrail-red-teamer` (writes adversarial tests + sims), `eval-analyst` (reads simulation reports, groups failures, proposes fixes).
- A PostToolUse hook runs ruff format + ruff check --fix on every Python file you edit, and sends leftover lint errors back to you. Fix them, don't fight it.
- `.env` is denied for reads. Never print or commit secrets.

## Commands (keep this section current)
```
uv sync                         # install
uv run pytest -q                # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run python -m scripts.dev    # run API + mock commerce together
cd web && npm install && npm run dev
```
