"""`FakeLLM`: a scripted, offline LLM for tests and for running without an API key."""

from collections import deque
from collections.abc import Callable, Iterable, Iterator

from baton.llm.types import LLMResponse, Msg, StreamEvent, ToolSpec, stream_response

Responder = Callable[[str, list[Msg], list[ToolSpec] | None], LLMResponse]


class FakeLLMExhaustedError(RuntimeError):
    """The test asked for more turns than it scripted."""


class FakeLLM:
    """Returns pre-scripted responses.

    Pass either a sequence of `LLMResponse`s (returned in order, one per call) or a callable
    `(system, messages, tools) -> LLMResponse`. Every call is recorded in `calls` so tests can
    assert on the exact prompt, history and tool list the runtime sent.
    """

    def __init__(self, script: Iterable[LLMResponse] | Responder | None = None) -> None:
        self._responder: Responder | None = None
        self._queue: deque[LLMResponse] = deque()
        if callable(script):
            self._responder = script
        elif script is not None:
            self._queue.extend(script)
        self.calls: list[tuple[str, list[Msg], list[ToolSpec] | None]] = []

    def push(self, *responses: LLMResponse) -> None:
        """Append more scripted responses."""
        self._queue.extend(responses)

    @property
    def remaining(self) -> int:
        return len(self._queue)

    def complete(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 16000,
    ) -> LLMResponse:
        self.calls.append((system, list(messages), tools))
        if self._responder is not None:
            return self._responder(system, messages, tools)
        if not self._queue:
            raise FakeLLMExhaustedError(
                f"FakeLLM ran out of scripted responses after {len(self.calls) - 1} call(s). "
                "Add more LLMResponse objects to the script or use a callable responder."
            )
        return self._queue.popleft()

    def stream(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 16000,
    ) -> Iterator[StreamEvent]:
        """The next scripted response, yielded in chunks (see `stream_response`)."""
        yield from stream_response(self.complete(system, messages, tools, max_tokens=max_tokens))
