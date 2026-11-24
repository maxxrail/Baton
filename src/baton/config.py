"""Application settings, read from the environment and `.env`.

Every tunable lives here so the rest of the code never touches `os.environ` directly.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "claude-sonnet-5-5"

# src/baton/config.py -> repo root. The package is installed editable, so this is the checkout.
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Baton settings. Field names map to upper-case env vars (`database_url` -> DATABASE_URL)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./baton.db"

    anthropic_api_key: SecretStr | None = None
    baton_model: str = DEFAULT_MODEL
    # None means "decide from the key": anthropic if a key is set, otherwise fake.
    baton_llm: Literal["anthropic", "fake"] | None = None
    # Ask the API to retry a refused request on a fallback model (server-side fallbacks beta).
    baton_llm_fallbacks: bool = True

    mock_commerce_url: str = "http://localhost:8100"

    # Guardrails. Refunds up to this amount (cents) are issued automatically; larger ones, or
    # a running total above it in one conversation, are escalated to a human.
    baton_refund_auto_limit_cents: int = Field(default=10_000, ge=0)

    # Where the customer's procedures, help-center articles and our prompt files live.
    aops_dir: Path = REPO_ROOT / "aops"
    prompts_dir: Path = REPO_ROOT / "prompts"
    kb_dir: Path = REPO_ROOT / "data" / "kb"

    # Knowledge base. `hashing` is offline and needs no key; `voyage` needs VOYAGE_API_KEY.
    # Changing the embedder means re-running `baton-kb ingest` (it re-embeds everything).
    baton_embedder: Literal["hashing", "voyage"] = "hashing"
    voyage_api_key: SecretStr | None = None
    baton_embed_model: str = "voyage-3.5"
    # Re-order the top retrieval candidates with an LLM call (off by default: costs a call).
    baton_kb_rerank: bool = False
    # Where `[kb:article_id]` citations link to when a channel renders them.
    baton_kb_base_url: str = "https://help.northwind.example/articles/"

    log_level: str = "INFO"

    @model_validator(mode="after")
    def _resolve_llm(self) -> "Settings":
        if self.baton_llm is None:
            has_key = self.anthropic_api_key is not None and bool(
                self.anthropic_api_key.get_secret_value()
            )
            self.baton_llm = "anthropic" if has_key else "fake"
        return self


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Tests construct `Settings(...)` directly instead."""
    return Settings()
