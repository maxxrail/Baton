# evals/

- `sims/`: simulation suites (`*.yaml`): customer personas plus pass/fail criteria (Part 05).
  `sims/generated/` holds LLM-drafted suites awaiting review.
- `reports/`: simulation run output (gitignored).
- `router_cases.yaml` (Part 01): 40 messages -> expected AOP. `uv run python -m scripts.eval_router`.
- `retrieval_qa.yaml` (Part 03): 40 questions -> the help-center article(s) that answer them,
  8 of them deliberately unanswerable. `uv run python -m scripts.eval_retrieval` reports
  recall@5, MRR and abstention accuracy, and exits 1 below the gate.
- `thresholds.yaml`: the regression gates scripts and CI enforce.
- `judge_calibration.yaml`: added in Part 05.
