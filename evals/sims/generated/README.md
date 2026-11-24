# evals/sims/generated/

LLM-drafted scenarios (`uv run baton-sim generate --aop <id> --n 8`). Each draft passed the same
validation as a hand-written scenario, but **nothing here is trusted**: the generator can write
expectations that contradict the procedure. Read each one, fix or delete it, and move the good
ones into `evals/sims/` to make them part of the suite. `baton-sim validate` doesn't check this
folder unless you pass `--suite evals/sims/generated`.
