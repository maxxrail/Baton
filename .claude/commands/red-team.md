---
description: Try to break the agent's guardrails and add tests for every hole found
argument-hint: [aop id, optional]
---
Use the `guardrail-red-teamer` subagent on: $ARGUMENTS (if empty, all AOPs). It should write new adversarial pytest cases (FakeLLM scripted to misbehave) and, if Part 05 exists, new scenarios in `evals/sims/`. Run them. For any that fail, fix the guardrail in code (not the prompt), rerun, and summarize what was found and fixed. Add the findings to `docs/EVAL_REPORT.md` if it exists.
