---
description: Resume a part that didn't finish in the last session
argument-hint: <part number>
---
Resume Part $ARGUMENTS. Read `CLAUDE.md`, `docs/PROGRESS.md` and the matching `docs/parts/part-$ARGUMENTS-*.md`. Run `uv run pytest -q` and `git status` to see the real state, compare it with the acceptance criteria, list what's left, then finish it following the same steps as `/build-part`. If a `part-$ARGUMENTS-*` branch already exists, check it out and keep working there (don't create a second one). If its PR is already open, pushing new commits updates it; don't open another.
