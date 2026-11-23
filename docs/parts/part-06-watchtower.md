# Part 06 — Watchtower (always-on QA) + insights

## Goal
Once the agent is live, every conversation is scored automatically, policy problems get flagged for review, and ops can see what customers contact you about and how well the agent handles it.

## Scope
1. **Scorers** (`src/baton/watchtower/scorers.py`), run async when a conversation resolves/escalates or goes idle 30 min:
   - Deterministic: guardrail blocks occurred, tool errors, turns, time to resolution, escalation reason, whether the customer repeated themselves (simple similarity check).
   - LLM rubric (structured, versioned prompt): resolution (resolved / partially / not), policy adherence vs. the AOP's `policies`, tone/brand voice, factual grounding (KB answers), customer sentiment start→end. Each with a score and a one-sentence rationale citing turn numbers.
2. **Flags**: rules in `src/baton/watchtower/rules.yaml` (e.g. `policy_adherence < 3`, `sentiment_end == negative and status == resolved`, any guardrail block, refund tool called) → flag with severity. Review queue in the console: open the conversation + trace, mark "confirmed issue / false positive", add a note. Confirmed issues can be **exported as a new simulation scenario** (prefilled YAML into `evals/sims/generated/`) — closing the loop between production and testing.
3. **Topic insights**: cluster first customer messages (embeddings + simple k-means or HDBSCAN), LLM-name each cluster; show volume, resolution rate, and escalation rate per topic and per AOP; flag "unrouted" topics (no AOP matched) as candidates for new AOPs.
4. **Metrics + dashboard** (`/console/insights`): conversations/day by channel, **automated resolution rate** (resolved without human), escalation rate + top reasons, avg turns, p95 latency, cost per conversation, Watchtower flag counts, per-AOP table. Date-range filter.
5. **Traffic generator** for the demo: `scripts/generate_traffic.py` runs ~200 simulated conversations (reusing Part 05 personas with random mixes) across channels over a fake date range so the dashboard has realistic data.
6. **API**: `/watchtower/flags`, `/watchtower/flags/{id}` (review), `/insights/summary`, `/insights/topics`.

## Acceptance criteria
- [ ] Scoring is idempotent per conversation + scorer version; rescoring after a prompt version bump creates new scores and keeps the old ones.
- [ ] Rules engine tested with fixtures (each rule fires/doesn't fire as expected).
- [ ] Flag → "export as simulation" produces a scenario that `baton-sim validate` accepts.
- [ ] Topic clustering runs on generated traffic and produces named clusters (test with HashingEmbedder + FakeLLM naming).
- [ ] Dashboard renders with generated traffic; numbers match a SQL check in tests.
- [ ] Lint, types, tests clean; `npm run build` passes.

## Verify
```
uv run python -m scripts.generate_traffic --n 200
uv run pytest -q
uv run python -m scripts.dev     # open http://localhost:5173/console/insights and /console/watchtower
```
