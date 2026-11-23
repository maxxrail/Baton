"""Application settings, read from the environment and `.env`.

Every tunable lives here so the rest of the code never touches `os.environ` directly.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "claude-sonnet-5-5"


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
