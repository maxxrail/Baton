# Start here — Baton

**Baton**: an AI customer-support agent platform. The name comes from a relay race. The AI agent and the human agent pass the conversation back and forth cleanly, and nothing gets dropped.

This kit is the plan, the specs, and the Claude Code setup. Claude Code writes all of the code, one part at a time.

## What's in here
Same layout as the frontdesk-agent repo. Part 00 creates `src/`, `tests/`, `migrations/` and the rest.
```
CLAUDE.md                 rules Claude Code reads every session
START_HERE.md             this file
CHANGELOG.md              one entry per part
.claude/                  settings, auto-format hook, slash commands, subagents
.env.example              copy to .env
.gitignore .gitattributes .dockerignore .pre-commit-config.yaml .gitleaksignore LICENSE
docs/
  ROADMAP.md              the 9 parts and why they matter to Decagon
  PROGRESS.md             Claude Code updates this after each part
  parts/part-00..08       one spec per part, with acceptance criteria
```

## One-time setup (Windows)
1. Install **uv**: in PowerShell run `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
2. Install **Node.js LTS** (needed from Part 04 on).
3. GitHub Desktop → File → New repository → name `baton` → create. Copy everything in this kit into that folder, **including the hidden dot-files and the `.claude` folder** (turn on View → Hidden items in Explorer). Don't let GitHub Desktop create its own .gitignore/LICENSE. Commit "docs: project plan + Claude Code setup". Publish to GitHub (public).
4. Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` (set `BATON_LLM=anthropic` when you want real calls). It's in `.gitignore`. Check that GitHub Desktop doesn't list it before you commit.

## Slash commands
| Command | What it does |
|---|---|
| `/build-part 00` | Builds a whole part, tests it, reviews it, updates PROGRESS.md, commits |
| `/continue-part 02` | Picks up a part that didn't finish |
| `/verify` | Runs lint, types, tests and the web build, and fixes failures |
| `/review` | Code review of your changes by the `code-reviewer` subagent |
| `/red-team return_item` | Tries to break the guardrails and adds tests for every hole (from Part 02 on) |
| `/sim --filter adversarial` | Runs simulations and explains failures (from Part 05 on) |
| `/status` | Where the project stands |
| `/interview-prep 02` | Explains a part and quizzes you like a Decagon interviewer |

## The loop
1. `/build-part 00` (then 01, 02, …)
2. Run the part's **Verify** commands yourself and click around.
3. `/interview-prep NN` so you can explain what was built.
4. Push with GitHub Desktop.

Extra passes worth doing: `/red-team` after Parts 02 and 03, and `/sim` repeatedly in Part 05 until the numbers are good.
