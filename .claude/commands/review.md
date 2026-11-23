---
description: Review uncommitted changes (or the last commit) with the code-reviewer subagent
---
Use the `code-reviewer` subagent to review `git diff` (or `git diff HEAD~1` if the tree is clean). Show me its findings ranked by severity, then fix the high and medium ones and rerun `/verify`.
