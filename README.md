# Baton

**Baton is an open-source support-agent platform where procedures are written in plain English, compiled into validated workflows, executed by an LLM agent whose guardrails are enforced in code, tested with simulated customers before every deploy, and monitored by always-on QA after.**

The name comes from a relay baton: the AI agent and the human agent pass the conversation back
and forth cleanly, and nothing gets dropped. The demo customer is **Northwind Goods**, a
fictional DTC home-goods brand, backed by a mock commerce API that ships in this repo.

## Does it work? Simulations

Every change is tested with simulated customers before it ships: an LLM persona holds a full
conversation with the real agent runtime, the outcome checks (which tools ran, how it ended)
are graded in code from the traces, and an LLM judge grades the behaviour ("explains the 30-day
policy", "never promises a refund"). The headline metric is **pass^k**: a scenario counts only
if all k runs pass.

| Suite | Scenarios | Result |
|---|---:|---|
| Offline, scripted (CI, every push): the model misbehaves on purpose, the runtime must stop it | 8 | **8/8 pass^k** |
| Live, real model (49 scenarios, 12 adversarial, 8 help-center, 3 multi-intent, 5 non-English/messy) | 49 | not measured yet: see [docs/EVAL_REPORT.md](docs/EVAL_REPORT.md) |

```
uv run baton-sim validate                                     # suites + coverage mix
uv run baton-sim run --suite evals/sims/ci --scripted --gate  # offline, no key
uv run baton-sim run --runs 3                                 # live suite (ANTHROPIC_API_KEY)
uv run baton-sim compare evals/reports/sim-A evals/reports/sim-B   # exit 1 on a regression
```

Each run writes `evals/reports/sim-<timestamp>/report.html`, a single file that opens offline:
summary, per-scenario drill-down, full transcripts with the agent's tool calls, judge
rationales and every turn's trace. The console's **Simulations** tab lists runs, opens reports
and reruns a scenario. Format and fixtures: [`evals/sims/README.md`](evals/sims/README.md).

## Quickstart

Needs [uv](https://docs.astral.sh/uv/) and, for the web app, Node 20+. Works on Windows, macOS
and Linux. Tests need no API key (they use an offline scripted LLM); chatting with the agent needs
`ANTHROPIC_API_KEY`.

```
uv sync                           # install (Python 3.12 is fetched if needed)
cp .env.example .env              # add ANTHROPIC_API_KEY for real model calls
uv run alembic upgrade head       # create baton.db
uv run baton-kb ingest            # index the help center (data/kb) for grounded answers
cd web && npm install && cd ..    # the widget + console
uv run python -m scripts.dev      # API :8000, mock store :8300, web app :5173
```

Then open:

- <http://localhost:5173/demo-store.html>: a fake Northwind store with the chat widget embedded
  (one `<script src="/widget.js">` tag). Try "where is NW-10002? I'm ava.thompson@example.com,
  zip 98101", "do you ship to Canada?", or press **Talk to a human**.
- <http://localhost:5173/console>: the ops console. Log in with any name and the console secret
  (`dev-console-secret` unless you set `BATON_CONSOLE_SECRET`). Live inbox, per-turn traces,
  take over / reply / hand back, AOP graphs and a draft validator, simulation runs.

If a port is taken: `uv run python -m scripts.dev --baton-port 8001` (the web app follows it).

Email and SMS without Twilio or a mail provider (signed exactly like the real webhooks; outgoing
replies are printed by the API):

```
uv run python -m scripts.simulate_inbound --channel sms --from "+15555550123" --text "where is my order"
uv run python -m scripts.simulate_inbound --channel email --from ava.thompson@example.com --subject "Shipping to Canada?" --text "How long does it take?"
```

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

Or over HTTP: `POST /conversations`, then `POST /conversations/{id}/messages {"text": "..."}`
(or the widget's WebSocket, `/ws/chat/{id}`). Every turn's trace is at
`GET /conversations/{id}/traces` (operator-only: `Authorization: Bearer <BATON_CONSOLE_SECRET>`). Replies cite help-center
articles as `[kb:article_id]`, and the response's `citations` list gives each one's title and URL.
Recorded sessions: [`docs/samples/chat-return.md`](docs/samples/chat-return.md) (return flow) and
[`docs/samples/chat-knowledge.md`](docs/samples/chat-knowledge.md) (grounded answers and abstention).

Checks:

```
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pre-commit run --all-files
uv run baton-sim validate && uv run baton-sim run --suite evals/sims/ci --scripted --gate
cd web && npm test && npm run build
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
| Channels | `src/baton/channels/`, `src/baton/api/{chat,channels}.py` | One `ConversationService` path from web chat (WebSocket + HTTP fallback), email (JSON webhook, threaded by Message-ID, quoted history stripped) and SMS (Twilio webhook with signature validation, STOP/HELP) into the same agent. Replies formatted per channel: markdown + citation links, plain text + footnotes + signature, ≤ 2 SMS segments with a link to the full answer |
| Human handoff | `src/baton/runtime/handoff.py`, `src/baton/api/console.py` | Once escalated the agent stays silent; a teammate takes over, replies through the customer's own channel, then resolves or returns the conversation to the AI. Operator actions share the agent's turn lease |
| Web app | `web/` | React + Vite + TS + Tailwind. `/widget` (floating chat, typing indicator showing what the agent is doing, citations as links, "talk to a human") with a `widget.js` embed snippet; `/console` (live inbox, conversation + handoff summary, trace panel, take over, AOP browser with Mermaid). Shared-secret login |
| AOP API | `src/baton/api/aops.py` | `GET /aops`, `GET /aops/{id}` (graph + Mermaid), `POST /aops/validate` |
| Database | `src/baton/db/`, `migrations/` | SQLAlchemy 2.0 + Alembic: `conversations` (with JSON runtime state), `messages`, `traces`, `aop_versions`, `kb_chunks`, `channel_threads`, `sms_opt_outs`. SQLite locally, Postgres via `DATABASE_URL` |
| LLM layer | `src/baton/llm/` | `LLMClient` protocol, `AnthropicClient` (official SDK), `FakeLLM` for offline tests, `HashingEmbedder` (default) and optional `VoyageEmbedder` |
| Northwind mock store | `src/mock_commerce/` | Customers, orders, returns, refunds, subscriptions, loyalty. Rules enforced server-side. See its [README](src/mock_commerce/README.md) |
| Seed data | `data/seed/` | Deterministic, with deliberate tricky cases (expired return window, partial refund, shared phone) |
| Simulations | `src/baton/sim/`, `evals/sims/`, `prompts/{persona,judge,generate}.md` | Persona-driven conversations against the real runtime, per-worker store reset to the seed, code-graded outcome checks, LLM judge (calibration set of 20 labelled transcripts), pass^k and cost metrics, offline HTML report, `compare` for regressions, LLM-drafted scenarios for review, `baton-sim` CLI, console page |
| CI | `.github/workflows/ci.yml`, `sims-live.yml` | ruff, mypy, pytest on Ubuntu and Windows, the offline simulation gate, Vitest + web build, gitleaks; a manual workflow runs the live suite with a repo secret |

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
| 04 | Channels + human handoff | done |
| 05 | Simulations + eval harness | done (live numbers pending) |
| 06 | Watchtower + insights | |
| 07 | Ship it | |
| 08 | *(stretch)* Voice + experiments | |

Details: [docs/ROADMAP.md](docs/ROADMAP.md). Build log, decisions and known gaps:
[docs/PROGRESS.md](docs/PROGRESS.md).

## License

MIT. See [LICENSE](LICENSE).
