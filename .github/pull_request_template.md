## Summary
<!-- 2–4 sentences a reviewer can read first: what this PR does, which roadmap part it belongs to
     (link docs/parts/part-NN-*.md), and the one or two things that matter most. -->

## What's included
<!-- Group by area (### headings). For each, say what was added or changed and where it lives.
     Call out anything a reviewer should look at closely. -->

## Design decisions
<!-- Every non-obvious choice and why, especially deviations from the part spec.
     A table works well: | Decision | Why | -->

## Testing
<!-- Commands run, with results. Name the failure paths covered (not just happy paths). -->
- [ ] `uv run pytest -q`
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy src`
- [ ] `uv run pre-commit run --all-files`
- [ ] Manual check (describe):

## Guardrails & safety
<!-- Any policy enforced in code (verification, refund limits, PII), with the test that proves
     the LLM can't talk its way past it. Write "None in this PR" if it doesn't apply. -->

## Known gaps / follow-ups
<!-- Anything deferred, with the part it belongs to. Must match docs/PROGRESS.md. -->

## How to try it
<!-- Exact commands and URLs so a reviewer can see it working in under two minutes. -->
