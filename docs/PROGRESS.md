# Progress

Claude Code updates this file at the end of every part.

## Status
| Part | Status | Date | Notes |
|------|--------|------|-------|
| 00 | done | 2026-11-23 | Repo skeleton, CI, DB + Alembic, LLM layer (Anthropic/Fake/Hashing), Northwind mock commerce + deterministic seed, API skeleton, dev runner |
| 01 | done | 2026-11-23 | AOP format + 8 Northwind AOPs, parser, validator (10 codes), compiler + step prompts, Mermaid, `baton-aop` CLI, router (pre-filter + LLM), `aop_versions`, `/aops` API |
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

## Part 01 — what was built
- **AOP format + 8 AOPs** (`aops/*.md`, documented in `aops/README.md`): `return_item`, `where_is_my_order`, `cancel_order`, `change_address`, `refund_request`, `manage_subscription`, `loyalty_points`, `general_question`. All compile with zero diagnostics.
- **Models** (`baton.aop.models`): `AOP`, `Step`, `Transition(condition, target, implicit)`, `Terminal` (ESCALATE/RESOLVE), `Diagnostic`, `CompiledAOP`/`CompiledStep`, `AOPBundle`.
- **Parser** (`baton.aop.parser`): rule-based, deterministic. Frontmatter is a strict Pydantic model (`extra="forbid"`). Steps must be numbered 1..N. Jump phrasings, terminals, leading/inner conditions and `Otherwise` defaults; implicit fall-through. 45 phrasings table-tested (38 with a transition, 7 that must *not* parse as one), plus negated jumps and "in which case" rejected as parse errors with step + line.
- **Validator** (`baton.aop.validator`): all codes from the spec plus `PARSE_ERROR`. One fixture per code in `tests/aop/fixtures/bad/`, each asserting exactly that code.
- **Compiler** (`baton.aop.compiler`): `compile_aops(dir) -> AOPBundle`, raises `AOPCompileError` on any error; renders a step prompt per step from `prompts/aop_step.md` (step text, policies, allowed tools with flags, exits, verification note).
- **Router** (`baton.aop.router`): hashing-embedding pre-filter (top-k=5 + `general_question` fallback) then an LLM classifier through a `choose_procedure` tool whose `aop_id` is an enum of the candidates. Output is validated in code; anything not offered or malformed routes to `None`. `evals/router_cases.yaml` (40 cases) + `scripts/eval_router.py`.
  - **Router eval (2026-11-23, `claude-sonnet-5-5`, top_k=5): accuracy 40/40 = 100.0%.** Pre-filter recall@5 38/38 = 100.0%; pre-filter top-1 alone 27/38 = 71.1% (so the LLM stage is doing real work: it fixes the 11 cases where the best trigger match is wrong).
- **Tool registry** (`baton.tools.registry`, `baton.tools.schemas`): contracts for 13 Northwind tools; implementations come in Part 02.
- **Versioning**: `aop_versions` table (migration `0002`), `baton.aop.store.sync_versions` inserts only unseen (aop_id, source_hash, step_prompt_version) keys; `baton-aop compile` runs it.
- **Code review** (`code-reviewer` subagent) fixes applied: `else` only marks a default at a clause start ("something else, go to step 2" kept its condition); a trailing `if`/`unless` only guards the target just before it and an `if` before the target is read as "whether"; negated jumps and "in which case" are rejected; `continue/return/move ... to N` require the word "step"; `NO_TERMINAL`/`BAD_TARGET` steps no longer also produce a `CYCLE_WITHOUT_EXIT`; `aop_versions` keys on the step-prompt template version too (compiled JSON includes rendered prompts); customer text is `<`/`>`-escaped in the router's data tags; `none` is a reserved AOP id; the source hash ignores a leading BOM; `Router` accepts `prompts_dir`.
- **CLI** `baton-aop validate|graph|show|compile`; **API** `GET /aops`, `GET /aops/{id}`, `POST /aops/validate`.
- **Prompt loader** (`baton.prompts`): `load_prompt(name)` → text + version `name@sha256[:8]`.

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

- 2026-11-23 — (Part 01) **Conditions are sentence-scoped.** `If X, do Y and RESOLVE.` is conditional; `If X, do Y. RESOLVE.` resolves always. This keeps the parser predictable; the validator's `UNREACHABLE_STEP` message explains the pattern when it bites (it did on the first draft of two Northwind AOPs).
- 2026-11-23 — (Part 01) `ESCALATE`/`RESOLVE` are recognised only in upper case, so prose ("we'll resolve it") never ends a procedure.
- 2026-11-23 — (Part 01) `NO_TERMINAL` is reported per step: a reachable step with no default exit (only possible for the last step, since others fall through). `CYCLE_WITHOUT_EXIT` is reported for a trapped strongly connected set of steps. Steps with a `BAD_TARGET` are excluded from those checks, so each mistake is reported once at its root.
- 2026-11-23 — (Part 01) Added `PARSE_ERROR` (not in the spec's list) so malformed files surface as diagnostics with line numbers instead of exceptions.
- 2026-11-23 — (Part 01) `aop_versions.version` is the author's frontmatter version; uniqueness is on (aop_id, source_hash, step_prompt_version), because the stored compiled JSON contains the rendered step prompts and must match what the agent saw. Editing an AOP without bumping `version` still stores a row and `sync_versions` warns (a template-only change doesn't warn). Source hash normalises CRLF→LF and strips a BOM so editors and OSes don't create versions. Compiling is assumed single-writer (documented in `store.py`). Migration `0002` was amended in place before merge since it hasn't shipped.
- 2026-11-23 — (Part 01) Every Northwind tool except `lookup_customer`/`verify_customer` is `requires_verification=True` (reads of account data included), so all account AOPs set `requires_verification: true`. The validator enforces this only for side-effecting tools, as specified; the Part 02 identity gate covers the rest.
- 2026-11-23 — (Part 01) Router pre-filter drops tokens that appear in the triggers of more than half the AOPs (`customer`, `wants`, `the`, ...) and scores the previous customer turn separately (×0.9) instead of concatenating it. Both came from measuring recall on the fixture: without them 2/38 labeled cases fell outside the top 5. One trigger was also added to `return_item` (size/colour exchanges). With these, recall@5 = 38/38 and top-1 by similarity alone = 27/38.
- 2026-11-23 — (Part 01) `LLMClient` has no `tool_choice`, so the router can't force the tool call; it also accepts a bare JSON answer and otherwise routes to `None`.
- 2026-11-23 — (Part 01) The API compiles AOPs in `create_app`, so a broken AOP directory fails startup (and tests) rather than live conversations.

## Known gaps
- Port 8000 is used by Docker Desktop/WSL on the dev machine, so the live dev-runner check ran with `--baton-port 8001`. On a machine with 8000 free, the default command works unchanged; `scripts/dev.py` now fails fast with a clear message when a port is taken.
- `.claude/commands/verify.md` step 7 runs `uv run baton-sim validate` when `evals/sims/` exists. That directory exists now (with a README) but `baton-sim` is built in Part 05, so skip step 7 until then.
- The CI workflow has not run on GitHub yet; it will on the first push. The gitleaks job uses `gitleaks/gitleaks-action@v2`, which needs a `GITLEAKS_LICENSE` secret only for organisation-owned repos (personal repos are fine).
- `redact.py` limits (documented in its docstring, pinned by a test): non-North-American phone formats aren't masked; any 13-19 digit run is masked as a card.
- Mock `verify` has no attempt limit. **Part 02 tools must cap identity-verification attempts per conversation** (guardrail in code).
- `get_llm()` without a key returns an empty `FakeLLM`, so a real agent turn would raise `FakeLLMExhaustedError`. Fine until Part 02 adds the runtime; it should then fail with a clear "set ANTHROPIC_API_KEY or script the fake" message.
- Mock commerce has no reset endpoint; tests and the dev runner reseed directly. Part 05 may want `POST /admin/reset` for simulations.
- Only `HashingEmbedder` exists; the optional real embedding provider from CLAUDE.md is deferred to Part 03.
- (Part 01) The 40-case router fixture is small and was written alongside the triggers, so 40/40 is a sanity check, not a generalisation claim. Part 05 simulations exercise routing on persona-generated messages.
- (Part 01) Parser heuristics are conservative but not exhaustive: unusual phrasings may still mis-parse. The safety net is `baton-aop show <id>` (prints every parsed edge) and the validator; Part 05 simulations will catch behavioural fallout.
- (Part 01) The router *unit* test uses a FakeLLM that answers with the label, so in CI it proves pre-filter recall and output validation, not classifier quality; classifier quality is measured by `scripts/eval_router.py` with a real key (not run in CI).
- (Part 01) Nothing in the running API writes `aop_versions` yet; `baton-aop compile` does. Part 02 should call `sync_versions` at startup so every trace's `aop_id@version` has a stored row.
- (Part 01) Allowed tools per step are exactly the step's `@refs`; Part 02 adds the always-allowed read-only lookups (`lookup_customer`, `verify_customer`) in the runtime allow-list.
- (Part 01) `search_knowledge` is not in the registry yet (Part 03); `general_question` therefore has no tool refs.
- (Part 01) The parser does not understand cross-AOP handoffs ("start a return"); a topic change is the router's job in Part 02.
