"""Provider-neutral message and tool types shared by every LLM client.

The runtime only ever speaks these types. `AnthropicClient` maps them to the SDK's wire format,
and `FakeLLM` returns them directly, so the runtime code is identical in tests and production.
"""

from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import BaseModel, Field

JsonDict = dict[str, object]


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """The model asking us to run a tool."""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: JsonDict = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """Our answer to a `ToolUseBlock`, sent back in the next user message."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


class ThinkingBlock(BaseModel):
    """Model reasoning. We never show it, but it must be sent back unchanged on the next turn."""

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str


class RedactedThinkingBlock(BaseModel):
    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str


ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock | RedactedThinkingBlock,
    Field(discriminator="type"),
]


class Msg(BaseModel):
    """One conversation message. `content` is plain text or a list of blocks."""

    role: Literal["user", "assistant"]
    content: str | list[ContentBlock]


class ToolSpec(BaseModel):
    """A tool the model may call. `input_schema` is a JSON Schema object."""

    name: str
    description: str
    input_schema: JsonDict


class ToolCall(BaseModel):
    id: str
    name: str
    input: JsonDict = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "other"]


class LLMResponse(BaseModel):
    """What a completion returns.

    `text` and `tool_calls` are the convenient views; `content` holds every block in order
    (including thinking) so the runtime can append it to history verbatim.
    """

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    stop_reason: StopReason = "end_turn"
    content: list[ContentBlock] = Field(default_factory=list)
    model: str = ""

    def as_assistant_msg(self) -> Msg:
        """The assistant message to append to history after this response."""
        if self.content:
            return Msg(role="assistant", content=list(self.content))
        blocks: list[ContentBlock] = []
        if self.text:
            blocks.append(TextBlock(text=self.text))
        blocks.extend(ToolUseBlock(id=c.id, name=c.name, input=c.input) for c in self.tool_calls)
        return Msg(role="assistant", content=blocks)


# ---------------------------------------------------------------- streaming


class TextDelta(BaseModel):
    """A piece of the model's text, as it is generated."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolStart(BaseModel):
    """The model has started writing a call to `name` (its input isn't complete yet)."""

    type: Literal["tool_start"] = "tool_start"
    name: str


# A stream yields deltas and tool starts in order, then exactly one `LLMResponse` (the same
# value `complete` would have returned) as its last item.
StreamEvent = TextDelta | ToolStart | LLMResponse


def stream_response(response: LLMResponse, chunk_chars: int = 24) -> Iterator[StreamEvent]:
    """Replay a finished response as a stream: text in chunks, a start per tool call, then the
    response. Used by `FakeLLM` and by clients that can't stream."""
    for i in range(0, len(response.text), chunk_chars):
        yield TextDelta(text=response.text[i : i + chunk_chars])
    for call in response.tool_calls:
        yield ToolStart(name=call.name)
    yield response
