import pytest
from pydantic import SecretStr

from baton.config import DEFAULT_MODEL, Settings
from baton.llm import (
    AnthropicClient,
    FakeLLM,
    HashingEmbedder,
    LLMConfigError,
    get_embedder,
    get_llm,
)


def _settings(**kwargs: object) -> Settings:
    # _env_file=None keeps a developer's real .env out of the test.
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_defaults_to_fake_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BATON_LLM", raising=False)
    settings = _settings()
    assert settings.baton_llm == "fake"
    assert settings.baton_model == DEFAULT_MODEL
    assert isinstance(get_llm(settings), FakeLLM)


def test_defaults_to_anthropic_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BATON_LLM", raising=False)
    settings = _settings(anthropic_api_key=SecretStr("test-key-not-real"))
    assert settings.baton_llm == "anthropic"
    llm = get_llm(settings)
    assert isinstance(llm, AnthropicClient)
    assert llm.model == DEFAULT_MODEL


def test_explicit_fake_wins_over_key() -> None:
    settings = _settings(anthropic_api_key=SecretStr("k"), baton_llm="fake")
    assert isinstance(get_llm(settings), FakeLLM)


def test_anthropic_without_key_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        get_llm(_settings(baton_llm="anthropic"))


def test_model_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BATON_MODEL", "claude-opus-5-5")
    assert _settings().baton_model == "claude-opus-5-5"


def test_api_key_is_not_in_repr() -> None:
    assert "super-secret" not in repr(_settings(anthropic_api_key=SecretStr("super-secret")))


def test_default_embedder_is_hashing() -> None:
    assert isinstance(get_embedder(_settings()), HashingEmbedder)
