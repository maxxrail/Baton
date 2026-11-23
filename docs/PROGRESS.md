# Progress

Claude Code updates this file at the end of every part.

## Status
| Part | Status | Date | Notes |
|------|--------|------|-------|
| 00 | done | 2026-11-23 | Repo skeleton, CI, DB + Alembic, LLM layer (Anthropic/Fake/Hashing), Northwind mock commerce + deterministic seed, API skeleton, dev runner |
| 01 | not started | | |
| 02 | not started | | |
| 03 | not started | | |
| 04 | not started | | |
| 05 | not started | | |
| 06 | not started | | |
| 07 | not started | | |
| 08 | not started | | |

## Part 00 — what was built
- `pyproject.toml` (uv, hatchling, src layout installing `baton` and `mock_commerce` editable), ruff/mypy (strict)/pytest config, `.python-version` = 3.12.
- `baton.config.Settings` (pydantic-settings): `DATABASE_URL`, `ANTHROPIC_API_KEY` (SecretStr), `BATON_MODEL`, `BATON_LLM`, `BATON_LLM_FALLBACKS`, `MOCK_COMMERCE_URL`, `LOG_LEVEL`.
- `baton.db`: models `conversations`, `messages`, `traces`; engine/session helpers (SQLite FKs on); Alembic at repo root with migration `0001`. A test checks the migrations match the models.
- `baton.llm`: `LLMClient` protocol, provider-neutral types (`Msg`, `ToolSpec`, `LLMResponse`, content blocks), `AnthropicClient`, `FakeLLM`, `Embedder` + `HashingEmbedder`, `get_llm()` / `get_embedder()`.
- `mock_commerce`: separate FastAPI app (port 8100) with every endpoint in the spec, rules enforced server-side with machine-readable error codes; seed generator + loader; README documenting data and tricky cases.
- `baton.api`: FastAPI app with `/healthz`, JSON structlog logs, `X-Request-ID` propagation, PII redaction on every log event (`baton.runtime.redact`).
- `scripts/dev.py`: runs both apps with uvicorn subprocesses, reseeds the mock store, checks ports first, Windows-safe.
- CI (`.github/workflows/ci.yml`): ruff, format, mypy, pytest and `alembic upgrade head` on ubuntu + windows, plus a gitleaks job. PR template.
- Folder READMEs for `aops/`, `prompts/`, `data/`, `data/kb/`, `data/seed/`, `evals/`, `evals/sims/generated/`, `ops/`, `scripts/`, `migrations/`.
- Tests are offline by construction: `tests/conftest.py` blocks every non-loopback socket connection, forces `BATON_LLM=fake` and blanks `ANTHROPIC_API_KEY` (so a real key in `.env` never loads).
- Code review (`code-reviewer` subagent) fixes applied: messages ordered by an integer autoincrement id (timestamps tie within a Windows clock tick); `UTCDateTime` column type so datetimes are aware UTC on SQLite and Postgres alike; row locks on mock-commerce writes; cancel refunds the remaining balance (consistent with seeded cancelled orders); RMAs ordered by creation time; uvicorn access log disabled (it printed raw `?email=` query strings); `redact_value` now covers dict keys, Pydantic models and arbitrary objects; `FakeLLM` responders receive `tools`; `.env.example` no longer hard-codes `BATON_LLM=fake`.

## Decisions
- 2026-11-23 — Default model `claude-sonnet-5-5` — the part asks for "a current Claude Sonnet model"; override with `BATON_MODEL`.
- 2026-11-23 — `AnthropicClient` ignores `temperature` — the current Claude API rejects sampling parameters (SDK 2.x doesn't accept them). The `LLMClient.complete` signature keeps `temperature` as the part specifies, documented as a hint.
- 2026-11-23 — `AnthropicClient` calls `client.beta.messages.create` with the server-side fallback beta (`fallbacks="default"`), so a safety-classifier refusal is retried on a fallback model by the API. Disable with `BATON_LLM_FALLBACKS=false`. `stop_reason="refusal"` is surfaced to callers either way.
- 2026-11-23 — `LLMResponse.content` keeps every block in order, including thinking blocks, so the runtime can append the assistant turn to history unchanged (the API requires thinking blocks be replayed as-is).
- 2026-11-23 — AnthropicClient test uses `httpx2.MockTransport` instead of respx. The anthropic SDK 2.x is built on `httpx2`; respx only patches `httpx`. This still exercises real request serialisation and response parsing with no network. respx stays a dev dependency for testing our own httpx clients (e.g. the mock-commerce tool client in Part 02).
- 2026-11-23 — Seed JSON stores times as day offsets (`delivered_days_ago: 45`) resolved at load time, so "delivered 45 days ago" stays true no matter when the store is seeded. `scripts/dev.py` reseeds on every start (`--keep-data` to skip).
- 2026-11-23 — Mock commerce money is integer cents everywhere; business-rule violations are 409 with `{"error": {"code", "message"}}`, input validation is 422.
- 2026-11-23 — Phone lookup returns a list (two seeded accounts share a phone); `verify` returns `false` for wrong zip, unknown order and someone else's order alike, so it leaks nothing.
- 2026-11-23 — Mock commerce has its own tiny settings (`MOCK_COMMERCE_DATABASE_URL`) and SQLAlchemy `Base`; it doesn't import `baton`, because it stands in for an external system. It uses `create_all` rather than Alembic (it's a disposable mock).
- 2026-11-23 — Pre-commit ruff hooks switched from the pinned `ruff-pre-commit` v0.8.4 repo to `repo: local` running `uv run ruff`, so pre-commit, CI, and the Claude Code hook all use the version in `uv.lock`. Migrations are linted/formatted like everything else.
- 2026-11-23 — `messages.id` is an integer autoincrement (it defines message order); conversations and traces keep uuid-hex string ids.
- 2026-11-23 — `baton.db.UTCDateTime` rejects naive datetimes on write and always returns aware UTC on read.
- 2026-11-23 — A minimal `baton/runtime/redact.py` (emails, phones, card numbers) was added in Part 00 because this part introduces logging and CLAUDE.md forbids PII in logs. Part 02 can extend it for traces.

## Known gaps
- Port 8000 is used by Docker Desktop/WSL on the dev machine, so the live dev-runner check ran with `--baton-port 8001`. On a machine with 8000 free, the default command works unchanged; `scripts/dev.py` now fails fast with a clear message when a port is taken.
- `.claude/commands/verify.md` step 7 runs `uv run baton-sim validate` when `evals/sims/` exists. That directory exists now (with a README) but `baton-sim` is built in Part 05, so skip step 7 until then.
- The CI workflow has not run on GitHub yet; it will on the first push. The gitleaks job uses `gitleaks/gitleaks-action@v2`, which needs a `GITLEAKS_LICENSE` secret only for organisation-owned repos (personal repos are fine).
- `redact.py` limits (documented in its docstring, pinned by a test): non-North-American phone formats aren't masked; any 13-19 digit run is masked as a card.
- Mock `verify` has no attempt limit. **Part 02 tools must cap identity-verification attempts per conversation** (guardrail in code).
- `get_llm()` without a key returns an empty `FakeLLM`, so a real agent turn would raise `FakeLLMExhaustedError`. Fine until Part 02 adds the runtime; it should then fail with a clear "set ANTHROPIC_API_KEY or script the fake" message.
- Mock commerce has no reset endpoint; tests and the dev runner reseed directly. Part 05 may want `POST /admin/reset` for simulations.
- Only `HashingEmbedder` exists; the optional real embedding provider from CLAUDE.md is deferred to Part 03.
