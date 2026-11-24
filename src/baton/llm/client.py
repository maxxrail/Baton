"""The `LLMClient` protocol: the one interface every component uses to call a model."""

from collections.abc import Iterator
from typing import Protocol

from baton.llm.types import LLMResponse, Msg, StreamEvent, ToolSpec


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

    def stream(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 16000,
    ) -> Iterator[StreamEvent]:
        """Run one model turn as a stream: `TextDelta`s and `ToolStart`s as they are generated,
        then the complete `LLMResponse` (identical to what `complete` returns) as the last item.
        """
        ...
