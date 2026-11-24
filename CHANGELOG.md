# Changelog

All notable changes to Baton. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). One entry per roadmap part.

## [Unreleased]
### Changed
- The Northwind mock store now defaults to port 8300 (was 8100): `scripts/dev.py`, the `MOCK_COMMERCE_URL` default and `.env.example`. Override with `--mock-port`.

## [Part 04] - 2026-11-24 — Channels + human handoff console
### Added
- Channel adapter interface (`baton.channels.base`): inbound normalisation and per-channel reply formatting. Web: markdown with numbered citation links. Email: plain text, footnote citations, signature. SMS: at most 2 segments (GSM-7/UCS-2 aware, smart punctuation mapped to GSM), cut at a word with a signed, expiring link to the full transcript (`baton.channels.links`, `BATON_LINK_SECRET`).
- `ConversationService` (`baton.channels.service`): one path from web, email and SMS into `Agent.handle_turn` and back out through the conversation's own channel, with live events on an in-process `EventHub`.
- Web chat: `WS /ws/chat/{id}` (typing activity, replies, status) with the HTTP fallback; `LLMClient.stream()` (Anthropic + `FakeLLM`), used to announce which tool the agent is calling. Reply text is sent only after the reply guardrails.
- Email: `POST /channels/email/inbound` (generic JSON, `X-Baton-Webhook-Secret`), threading by `In-Reply-To`/`References` (sender must match) and by subject for provider-authenticated senders, quoted-history stripping, retry dedupe. Console, Resend and SMTP senders.
- SMS: `POST /channels/sms/inbound` with Twilio signature validation (403 on mismatch, `BATON_PUBLIC_URL` for proxies), STOP/START/HELP, opt-out suppression, `MessageSid` dedupe. Console and Twilio senders.
- Human handoff (`baton.runtime.handoff`): take over, reply as `human_agent` through the original channel, resolve or return to the AI. Operator actions share the agent's turn lease.
- Console API (`/ops/...`, `WS /ws/ops`) with shared-secret login (`BATON_CONSOLE_SECRET`, signed HttpOnly SameSite=Strict cookie, Origin checks) and bearer auth for scripts.
- Web app (`web/`, React + Vite + TS + Tailwind): `/widget` chat with "talk to a human", `public/widget.js` iframe embed, `demo-store.html`, `/console` (live inbox, conversation + handoff summary, trace panel, take over / reply / hand back, AOP browser with Mermaid and draft validation). Vitest tests; CI job for `npm test` + `npm run build`.
- `scripts/simulate_inbound.py` (signed SMS and email webhooks for demos); `scripts/dev.py` also starts Vite and fills dev-only defaults for unset channel secrets.
- Migration `0005`: `conversations.contact`/`subject`, `messages.meta`, `channel_threads`, `sms_opt_outs`.

### Changed
- An escalated conversation is human-owned: the agent stores and traces customer messages but no longer sends the fixed holding reply (`reply_source="human_owned"`, `AgentReply.replied=False`).
- `GET /conversations/{id}` and `/traces` are operator-only; customers read `GET /conversations/{id}/messages`. `POST /conversations/{id}/messages` accepts web conversations only and returns the rendered `body`.
- `acquire_turn` returns a lease token; a turn that outlived its lease raises `LeaseLostError` instead of saving over whoever claimed the conversation since.
- Agent replies store their citations in `messages.meta`; `AgentReply` gains `message_id` and `replied`.

### Fixed (found in the browser check)
- The widget crashed when opened in current Chrome (`scrollIntoView` returns a Promise, which an effect returned to React).
- Handoff notes were written from the escalation turn's tool log only, so a verified customer could be described as unverified. The note writer now gets the verified state and the whole conversation's tool log.
- An answer written next to a tool call could be lost when the model returns that text as a hidden progress block. The runtime now tells the model when the customer has seen nothing yet this turn.

## [Part 03] - 2026-11-24 — Knowledge base (RAG) with citations
### Added
- 28 Northwind help-center articles in `data/kb/` (shipping by region, Canada, costs, processing, tracking, returns, exchanges, refunds, damaged items, warranty, sizing, care, loyalty, subscriptions, payments, gift cards, promo codes, support, account, coffee freshness). Two deliberate old-vs-new conflicts: `return-policy` vs `return-policy-2024` and `shipping-costs` vs `free-shipping-offer-2025`.
- `baton.knowledge`: strict frontmatter loader; heading-aware chunker (~300–500 tokens, heading paths kept, oversized sections split at paragraphs); incremental ingest into `kb_chunks` (migration `0004`) keyed by a content hash that includes the embedder name; hybrid retrieval (BM25 + embedding cosine, reciprocal-rank fusion, top-5) with a newer-policy-wins rule and a query-coverage signal for abstention; optional LLM reranker (`BATON_KB_RERANK`); `KnowledgeBase` facade.
- `search_knowledge` tool, offered on every step of every procedure, needing no identity verification. `general_question` (v2) answers from it.
- Grounding guardrail (`baton.knowledge.grounding`, `prompts/grounding.md`): the reply is split into sentences in code, an LLM judge labels each sentence via structured output, and code removes unsupported claims (adding an offer of a teammate) or replaces the reply with an abstention. Citations are added for supported claims and stripped when they point at articles that weren't retrieved. The check fails closed on any judge error.
- `[kb:article_id]` citations: `AgentReply.citations` (id, title, URL) in the API response, and a channel renderer (`baton.channels.citations`: markdown links for web, footnotes for CLI/email, one link for SMS).
- Traces record every knowledge search (query, covered, articles, chunk ids), every grounding outcome (verdicts, removed sentences, citations added/stripped, evidence ids, prompt version) and the reply's citations. `baton-chat --trace` shows them.
- `baton-kb ingest | search` CLI. `scripts/eval_retrieval.py` + `evals/retrieval_qa.yaml` (40 questions, 8 unanswerable) + `evals/thresholds.yaml`. CI runs the eval, which fails below recall@5 0.85. Result: recall@5 0.969, MRR 0.768, abstention accuracy 0.900.
- Optional `VoyageEmbedder` (`BATON_EMBEDDER=voyage`, `VOYAGE_API_KEY`) over plain httpx.
- Sample live transcript `docs/samples/chat-knowledge.md`.

### Changed
- `Embedder` protocol gains `name` (stored per chunk, so switching embedders forces a re-embed).
- `ConversationState.kb_evidence` keeps the last search's chunk ids so follow-up answers are grounded against them.
- **Fix (found live):** text the model writes in the same response as tool calls (often the answer itself, alongside `advance_step(RESOLVE)`) is now part of the reply, but only if every tool call in that response succeeded. Before, only the final text-only response was sent, so answers could be silently dropped. The model is told the draft will be sent, repeats aren't sent twice, and output guardrails see the combined text.
- Abstentions keep runtime-verified facts: an escalation reply, or the side effects the store confirmed this turn.
- `truncate` never ends inside a `[...]` tag, and on SMS the offer of a teammate is never truncated away.
- `kb_chunks` uses `sqlite_autoincrement`, so chunk ids are never reused after a re-ingest.

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
