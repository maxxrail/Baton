# scripts/

Python helper scripts, all Windows-safe. Run them as modules from the repo root:

| Command | What it does |
|---|---|
| `uv run python -m scripts.dev` | Start the Baton API (:8000) and Northwind mock commerce (:8100) together. Reseeds the mock store on start; `--keep-data` keeps it. |

Later parts add a traffic generator and eval scripts here.
