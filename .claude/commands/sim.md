---
description: Run the simulation suite and analyze failures
argument-hint: [--filter tag] [--runs N]
---
Run `uv run baton-sim run $ARGUMENTS`. Then use the `eval-analyst` subagent on the newest `evals/reports/sim-*/results.json`. Show me: overall pass rate and pass^k, the table of failing scenarios, the failure groups with root causes, and the top 3 proposed fixes. Don't apply the fixes until I say so.
