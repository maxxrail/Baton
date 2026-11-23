Alembic migrations for Baton's own database.

- Apply: `uv run alembic upgrade head`
- New migration: `uv run alembic revision --autogenerate -m "<what changed>"`
