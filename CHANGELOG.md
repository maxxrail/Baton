# Changelog

All notable changes to Baton. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). One entry per roadmap part.

## [Unreleased]

## [Part 01] - 2026-11-23 — AOP language + compiler
### Added
- AOP format: YAML frontmatter + numbered plain-English steps with `@tool` references, step jumps, `ESCALATE`/`RESOLVE` terminals and sentence-scoped conditions. Eight Northwind AOPs in `aops/` (return, where-is-my-order, cancel, change address, refund, subscriptions, loyalty, general question).
- Deterministic parser (`baton.aop.parser`) with line-numbered errors; unknown frontmatter keys rejected; negated jumps and "in which case" rejected rather than guessed.
- Validator with 10 diagnostic codes (`PARSE_ERROR`, `UNKNOWN_TOOL`, `BAD_TARGET`, `UNREACHABLE_STEP`, `NO_TERMINAL`, `CYCLE_WITHOUT_EXIT`, `SIDE_EFFECT_WITHOUT_VERIFICATION`, `DUPLICATE_ID`, `MISSING_TRIGGERS`, `LONG_STEP`), each reported once at its root cause.
- Compiler producing an `AOPBundle` with a bundle hash and per-step prompts rendered from `prompts/aop_step.md`.
- Mermaid flowchart export; `baton-aop` CLI (`validate`, `graph`, `show`, `compile`).
- Router: embedding pre-filter over triggers + LLM classifier via a structured `choose_procedure` tool; output validated against the offered candidates. 40-case labeled fixture `evals/router_cases.yaml` and `scripts/eval_router.py`.
- Tool registry contracts for the 13 Northwind tools (`baton.tools.registry`), with `side_effect` and `requires_verification` flags.
- `aop_versions` table (migration `0002`), written only when an AOP's source hash or the step-prompt template changes.
- API: `GET /aops`, `GET /aops/{id}`, `POST /aops/validate`. The app compiles AOPs at startup and refuses to start if any has an error.
- Versioned prompt loader (`baton.prompts`): `name@sha256[:8]`, `$placeholder` templates.
- Settings `AOPS_DIR`, `PROMPTS_DIR`.

## [Part 00] - 2026-11-23 — Foundation + Northwind mock commerce
### Added
- uv project (Python 3.12, src layout) with ruff, strict mypy and pytest configuration.
- `baton.config.Settings` reading `.env`; `.env.example` lists every setting.
- Database layer: SQLAlchemy 2.0 models for conversations, messages and traces; Alembic with an initial migration.
- LLM layer: `LLMClient` protocol, `AnthropicClient` (official SDK, tool use, thinking-block replay, server-side refusal fallback), scripted `FakeLLM`, offline `HashingEmbedder`, `get_llm()` factory.
- Northwind Goods mock commerce API (port 8100): customer lookup/verification, orders, cancel, address change, returns (30-day window), refunds (capped at remaining balance), subscriptions, loyalty. Every rule is enforced server-side with machine-readable error codes.
- Deterministic seed data in `data/seed/` (50 customers, 200 orders, 20 subscriptions) with fixed tricky cases: expired return window, existing partial refund, two accounts sharing one phone.
- Baton API skeleton: `/healthz`, JSON logs with request IDs, PII redaction.
- `scripts/dev.py` to run both services together (Windows-safe).
- GitHub Actions CI on Ubuntu and Windows (ruff, mypy, pytest, alembic) plus gitleaks; PR template.
- Offline test guard: the suite fails on any non-loopback network connection.

### Changed
- Pre-commit runs ruff through `uv run` so its version matches `uv.lock`.

## [Planning]
### Added
- Project plan (`docs/ROADMAP.md`, `docs/parts/`), Claude Code setup (`.claude/`), repo config (pre-commit, gitleaks, gitattributes).
