---
description: Build one part of the roadmap end to end (e.g. /build-part 02)
argument-hint: <part number, e.g. 02>
---
Build Part $ARGUMENTS of Baton.

1. Read `CLAUDE.md`, `docs/ROADMAP.md`, `docs/PROGRESS.md`, and the file in `docs/parts/` whose name starts with `part-$ARGUMENTS-`.
2. If an earlier part is not marked done in PROGRESS.md, stop and tell me which one is missing.
3. Make a task list from the part's Scope and Acceptance criteria before writing code.
4. Build it. Write tests alongside the code, not at the end.
5. Run every command under **Verify** (skip only the ones that need a real API key or a browser, and list those for me to run).
6. Use the `code-reviewer` subagent on your changes and fix what it finds.
7. Tick each acceptance criterion in your final message with evidence (test name or command output).
8. Update `docs/PROGRESS.md` (status, decisions, known gaps), add a `CHANGELOG.md` entry, and update `README.md` if behavior changed.
9. Commit with `feat(part-$ARGUMENTS): <summary>`. Don't push.

Don't stop early and don't cut scope silently. If you can't finish in this session, write exactly where you stopped in PROGRESS.md.
