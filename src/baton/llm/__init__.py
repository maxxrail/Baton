"""LLM and embedding interfaces. Import from here: `from baton.llm import LLMClient, FakeLLM`."""

from baton.config import Settings, get_settings
from baton.llm.anthropic_client import AnthropicClient
from baton.llm.client import LLMClient
from baton.llm.embedder import Embedder, HashingEmbedder
from baton.llm.fake import FakeLLM, FakeLLMExhaustedError
from baton.llm.types import (
    ContentBlock,
    LLMResponse,
    Msg,
    RedactedThinkingBlock,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from baton.llm.voyage import DEFAULT_VOYAGE_MODEL, EmbeddingError, VoyageEmbedder


class LLMConfigError(RuntimeError):
    pass


def get_llm(settings: Settings | None = None) -> LLMClient:
    """Build the configured LLM client. `BATON_LLM=fake` gives an empty `FakeLLM`."""
    settings = settings or get_settings()
    if settings.baton_llm == "fake":
        return FakeLLM()
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        raise LLMConfigError("BATON_LLM=anthropic requires ANTHROPIC_API_KEY to be set")
    return AnthropicClient(
        model=settings.baton_model,
        api_key=settings.anthropic_api_key.get_secret_value(),
        fallbacks=settings.baton_llm_fallbacks,
    )


def get_embedder(settings: Settings | None = None) -> Embedder:
    """The configured embedder: offline hashing by default, Voyage with `BATON_EMBEDDER=voyage`."""
    settings = settings or get_settings()
    if settings.baton_embedder == "hashing":
        return HashingEmbedder()
    key = settings.voyage_api_key.get_secret_value() if settings.voyage_api_key else ""
    if not key:
        raise LLMConfigError("BATON_EMBEDDER=voyage requires VOYAGE_API_KEY to be set")
    return VoyageEmbedder(key, model=settings.baton_embed_model)


__all__ = [
    "DEFAULT_VOYAGE_MODEL",
    "AnthropicClient",
    "ContentBlock",
    "Embedder",
    "EmbeddingError",
    "FakeLLM",
    "FakeLLMExhaustedError",
    "HashingEmbedder",
    "LLMClient",
    "LLMConfigError",
    "LLMResponse",
    "Msg",
    "RedactedThinkingBlock",
    "StopReason",
    "TextBlock",
    "ThinkingBlock",
    "ToolCall",
    "ToolResultBlock",
    "ToolSpec",
    "ToolUseBlock",
    "Usage",
    "VoyageEmbedder",
    "get_embedder",
    "get_llm",
]
