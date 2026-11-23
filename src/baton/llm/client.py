"""The `LLMClient` protocol: the one interface every component uses to call a model."""

from typing import Protocol

from baton.llm.types import LLMResponse, Msg, ToolSpec


class LLMClient(Protocol):
    def complete(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 16000,
    ) -> LLMResponse:
        """Run one model turn and return its text, tool calls, usage and stop reason.

        `temperature` is a hint: providers that no longer accept sampling parameters ignore it.
        """
        ...
