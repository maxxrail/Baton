"""`AnthropicClient`: the `LLMClient` backed by the official Anthropic Python SDK.

All SDK-specific shapes stay in this file. The rest of Baton only sees `baton.llm.types`.

Notes on the current Claude API:
- Sampling parameters (`temperature`, ...) are no longer accepted, so `temperature` is ignored.
- Thinking blocks come back in `content` and must be sent back unchanged on the next turn;
  we keep them as `ThinkingBlock` / `RedactedThinkingBlock` for exactly that reason.
- A safety classifier may stop a turn with `stop_reason == "refusal"`. With fallbacks enabled
  the API retries on a fallback model server-side before giving up.
"""

import anthropic
from anthropic.types.beta import (
    BetaAssistantMessageParam,
    BetaContentBlockParam,
    BetaMessage,
    BetaMessageParam,
    BetaToolParam,
    BetaUserMessageParam,
)

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

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "refusal": "refusal",
}


class AnthropicClient:
    """Calls Claude through `client.beta.messages.create`.

    Pass `client` to inject a pre-configured SDK client (tests use one with a mock transport).
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        client: anthropic.Anthropic | None = None,
        fallbacks: bool = True,
    ) -> None:
        self.model = model
        self.fallbacks = fallbacks
        self._client = client or anthropic.Anthropic(api_key=api_key)

    def complete(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 16000,
    ) -> LLMResponse:
        del temperature  # not accepted by the current API; see module docstring
        wire_messages = [_to_wire_message(m) for m in messages]
        wire_tools = [_to_wire_tool(t) for t in tools] if tools else anthropic.omit
        if self.fallbacks:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=wire_messages,
                tools=wire_tools,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        else:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=wire_messages,
                tools=wire_tools,
            )
        return _from_wire_response(response)


def _to_wire_tool(tool: ToolSpec) -> BetaToolParam:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": {"type": "object", **tool.input_schema},
    }


def _to_wire_message(msg: Msg) -> BetaMessageParam:
    content: str | list[BetaContentBlockParam] = (
        msg.content if isinstance(msg.content, str) else [_to_wire_block(b) for b in msg.content]
    )
    if msg.role == "user":
        return BetaUserMessageParam(role="user", content=content)
    return BetaAssistantMessageParam(role="assistant", content=content)


def _to_wire_block(block: ContentBlock) -> BetaContentBlockParam:
    match block:
        case TextBlock():
            return {"type": "text", "text": block.text}
        case ToolUseBlock():
            return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
        case ToolResultBlock():
            return {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
                "is_error": block.is_error,
            }
        case ThinkingBlock():
            return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
        case RedactedThinkingBlock():
            return {"type": "redacted_thinking", "data": block.data}


def _from_wire_response(response: BetaMessage) -> LLMResponse:
    content: list[ContentBlock] = []
    for block in response.content:
        if block.type == "text":
            content.append(TextBlock(text=block.text))
        elif block.type == "tool_use":
            content.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input)))
        elif block.type == "thinking":
            content.append(ThinkingBlock(thinking=block.thinking, signature=block.signature))
        elif block.type == "redacted_thinking":
            content.append(RedactedThinkingBlock(data=block.data))
        # Server-tool blocks are not used by Baton; they are dropped.

    texts = [b.text for b in content if isinstance(b, TextBlock)]
    calls = [
        ToolCall(id=b.id, name=b.name, input=b.input)
        for b in content
        if isinstance(b, ToolUseBlock)
    ]
    stop = _STOP_REASONS.get(response.stop_reason or "end_turn", "other")
    return LLMResponse(
        text="".join(texts),
        tool_calls=calls,
        usage=Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        ),
        stop_reason=stop,
        content=content,
        model=response.model,
    )
