# Changelog

All notable changes to Baton. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). One entry per roadmap part.

## [Unreleased]

## [Part 02] - 2026-11-23 — Agent runtime + guardrails
### Added
- Northwind tool implementations (`baton.tools.northwind`) over an httpx `CommerceClient`: 5 s timeout, GETs retried once on 5xx/timeouts (POSTs never, so a refund can't be paid twice), store errors mapped to `ToolError(code, customer_safe_message)`. Calls are scoped to the verified customer: another customer's order or subscription reads as not found, `customer_id` arguments must match, and lookup results mask contact details.
- `ConversationState` (`baton.runtime.state`), persisted as JSON in `conversations.state` (migration `0003`), and a per-conversation turn lease (`conversations.turn_lease_until`) so concurrent turns can't interleave (API 409).
- Agent loop (`baton.runtime.agent`): routes with the Part 01 router, re-routes on `switch_procedure`, offers only the current step's tools, validates `advance_step(target)` against the compiled graph, caps each turn at 6 store tool calls, and never holds a DB transaction during model or store calls.
- Deterministic guardrails (`baton.runtime.guardrails`): identity gate; refunds ≤ `BATON_REFUND_AUTO_LIMIT_CENTS` (default $100, counting earlier refunds in the conversation and on the order) and ≤ the order's live balance, else escalate; fails closed if the balance can't be read; per-step tool allow-list; one identity per conversation; reply checks for unbacked refund claims (including amounts that don't match a real refund), other customers' order numbers in any spelling, unprovided emails/phones, card numbers, and per-channel length caps; escalation after 3 failed verifications, 12 turns without progress, or a request for a human.
- Escalation with an LLM-written handoff note (`write_handoff` tool, validated) and a deterministic fallback (`baton.runtime.escalation`).
- Per-turn traces (`baton.runtime.traces`): AOP ref, source hash and step-prompt version, step before/after, prompt versions, route decision, every LLM call (purpose, tokens, latency), every tool call (redacted args/results, blocker), every guardrail decision, rejected reply drafts. Secrets (`zip`, `line1`) are masked and PII redacted.
- API: `POST /conversations`, `POST /conversations/{id}/messages`, `GET /conversations/{id}`, `GET /conversations/{id}/traces`. The agent is built lazily; without an LLM the API returns 503 with instructions.
- `baton-chat [--trace]` terminal chat, with preflight checks for the migrated DB and the reachable store.
- Prompts `brand.md`, `agent.md`, `handoff.md`. Sample live transcript in `docs/samples/chat-return.md`.

### Changed
- `Router.route` accepts a per-call `llm`, so the agent can meter router calls in the trace.
- `OrderStatus` includes `returned` (seeded orders have it; listing them failed validation before).
- Every compiled AOP version is stored on the agent's first turn, so each trace's `aop_id@version` has a row in `aop_versions`.

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
