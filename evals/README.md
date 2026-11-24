# evals/

- `sims/` (Part 05): simulation suites, see [`sims/README.md`](sims/README.md). `sims/*.yaml`
  is the live suite (49 scenarios), `sims/ci/` the offline scripted suite CI runs on every push,
  `sims/generated/` LLM-drafted scenarios awaiting review. `uv run baton-sim --help`.
- `reports/`: simulation run output, `sim-<timestamp>/` with `results.json`, `summary.json`
  and `report.html` (gitignored).
- `router_cases.yaml` (Part 01): 40 messages -> expected AOP. `uv run python -m scripts.eval_router`.
- `retrieval_qa.yaml` (Part 03): 40 questions -> the help-center article(s) that answer them,
  8 of them deliberately unanswerable. `uv run python -m scripts.eval_retrieval` reports
  recall@5, MRR and abstention accuracy, and exits 1 below the gate.
- `thresholds.yaml`: the regression gates scripts and CI enforce.
- `judge_calibration.yaml` (Part 05): 20 hand-labelled transcripts (45 labels) for measuring
  how often the simulation judge agrees with a person. `uv run baton-sim calibrate`.
