# Part 07 — Ship it

## Goal
A live URL a recruiter can click, a README that sells the project in 30 seconds, and the operational basics a production agent needs.

## Scope
1. **Containers**: multi-stage `Dockerfile` (API serves the built web app as static files), `docker-compose.yml` (api, mock-commerce, postgres). Keep `.dockerignore` tight (no .venv, evals/reports, node_modules, .env). Verify all migrations and tests pass against **Postgres** (add a CI job with a Postgres service).
2. **Deploy**: Fly.io *or* Render config (pick one, document why), managed Postgres, secrets via the platform. Platform config, dashboards and runbooks (LLM outage, bad deploy, cost spike) go in `ops/`; step-by-step instructions in `docs/DEPLOY.md`. Public demo mode: the widget on a public demo-store page, console behind login, a "reset demo data" job nightly.
3. **Auth**: console users with hashed passwords (argon2) + session cookies, roles `admin` and `agent`; API keys for channel webhooks. Remove the shared-secret login from Part 04.
4. **Abuse + cost protection** for the public demo: per-IP and per-conversation rate limits, max turns per conversation, daily token budget with graceful "demo limit reached" message, input length cap, basic prompt-injection detection logged to Watchtower.
5. **Reliability**: LLM call timeouts + retries with backoff, fallback model on overload, circuit breaker that escalates to human with an apology when the LLM is down, idempotency keys on side-effecting tools, graceful shutdown.
6. **Observability**: OpenTelemetry traces for API → agent → LLM → tools (console exporter by default, OTLP via env); `/metrics` (Prometheus format: request latency, LLM latency/tokens, tool errors, escalations).
7. **Security pass**: dependency audit (`pip-audit`, `npm audit`), CORS locked down, security headers, secrets scan in CI, PII redaction re-checked, `SECURITY.md`.
8. **Docs that sell**:
   - `README.md`: one-line pitch, **live demo link**, 30–60s demo GIF (widget conversation + console trace panel), the **Simulations results table** from EVAL_REPORT.md near the top, architecture diagram (Mermaid), feature list mapped to concepts (AOPs, guardrails, simulations, Watchtower), quickstart (≤ 5 commands), tech stack, "what I'd build next".
   - `docs/ARCHITECTURE.md`: components, data flow for one turn, why guardrails live in code, trace schema, trade-offs made.
   - `docs/EVAL_REPORT.md` finalized.
   - A short script for a 2–3 min Loom walkthrough in `docs/DEMO_SCRIPT.md`.

## Acceptance criteria
- [ ] `docker compose up` brings up everything locally; tests pass against Postgres in CI.
- [ ] Live URL works: widget conversation from a phone browser, console login, trace panel.
- [ ] Rate limits and daily budget tested.
- [ ] LLM outage test (FakeLLM raises) → customer gets apology + escalation, not a 500.
- [ ] `/metrics` exposes the listed metrics.
- [ ] CI green on all jobs (lint, types, tests Linux/Windows, Postgres, deterministic sims, audits).
- [ ] README has the live link, GIF, results table, architecture diagram, and a quickstart a stranger can follow.

## Verify
```
docker compose up --build
uv run pytest -q
# follow docs/DEPLOY.md, then open the live URL on your phone
```
