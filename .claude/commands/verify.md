---
description: Run every check (lint, types, tests, web build) and report
---
Run, in order, and report pass/fail for each with the key error lines:

1. `uv run ruff check .`
2. `uv run ruff format --check .`
3. `uv run mypy src`
4. `uv run pre-commit run --all-files` (if `.pre-commit-config.yaml` hooks are installed)
5. `uv run pytest -q`
6. If `web/package.json` exists: `cd web && npm test -- --run && npm run build`
7. If `evals/sims/` exists: `uv run baton-sim validate`

If anything fails, fix it and rerun until all pass, unless the fix would change behavior a part file specifies. In that case, stop and ask me.
