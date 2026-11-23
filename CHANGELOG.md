# Changelog

All notable changes to Baton. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). One entry per roadmap part.

## [Unreleased]

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
