---
description: Build one part of the roadmap end to end (e.g. /build-part 02)
argument-hint: <part number, e.g. 02>
---
Build Part $ARGUMENTS of Baton.

1. Read `CLAUDE.md`, `docs/ROADMAP.md`, `docs/PROGRESS.md`, and the file in `docs/parts/` whose name starts with `part-$ARGUMENTS-`.
2. If an earlier part is not marked done in PROGRESS.md, stop and tell me which one is missing.
3. Create a branch `part-$ARGUMENTS-<slug>` from an up-to-date `main` (slug from the part file name, e.g. `part-01-aop-compiler`). Never commit a part on `main`. Then make a task list from the part's Scope and Acceptance criteria before writing code.
4. Build it. Write tests alongside the code, not at the end.
5. Run every command under **Verify** (skip only the ones that need a real API key or a browser, and list those for me to run).
6. Use the `code-reviewer` subagent on your changes and fix what it finds.
7. Tick each acceptance criterion in your final message with evidence (test name or command output).
8. Update `docs/PROGRESS.md` (status, decisions, known gaps), add a `CHANGELOG.md` entry, and update `README.md` if behavior changed.
9. Commit with `feat(part-$ARGUMENTS): <summary>`, then push the branch (`git push -u origin <branch>`) and open a pull request into `main` with `gh pr create --body-file <file>`. Title: the commit subject. The body is a professional, extended description that fills **every** section of `.github/pull_request_template.md` (Summary, What's included, Design decisions, Testing, Guardrails & safety, Known gaps / follow-ups, How to try it). Write it for an engineer at an AI company reviewing this as a portfolio piece: concrete file paths, real test counts and command results, failure paths covered, and the reasoning behind each decision, with no filler. If later commits change the scope, update the body with `gh pr edit --body-file`. Give me the PR link. Never push to `main` and never merge the PR; I review and merge it.

Don't stop early and don't cut scope silently. If you can't finish in this session, write exactly where you stopped in PROGRESS.md.
