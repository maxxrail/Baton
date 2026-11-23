# scripts/

Python helper scripts, all Windows-safe. Run them as modules from the repo root:

| Command | What it does |
|---|---|
| `uv run python -m scripts.dev` | Start the Baton API (:8000) and Northwind mock commerce (:8100) together. Reseeds the mock store on start; `--keep-data` keeps it. |

| `uv run python -m scripts.eval_router` | Router accuracy on `evals/router_cases.yaml`. Needs `ANTHROPIC_API_KEY` for the LLM stage; without it, reports pre-filter recall only. `--json <path>` writes per-case results. |

Later parts add a traffic generator and more eval scripts here.
