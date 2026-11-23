# data/

- `kb/`: Northwind Goods help-center articles (`*.md`) for the knowledge base (Part 03).
- `seed/`: fixed seed data for the mock commerce store. Generated deterministically by
  `uv run python -m mock_commerce.seed --generate` and loaded by `src/mock_commerce/seed.py`.
  See `src/mock_commerce/README.md` for what's in it.
