"""AnthropicClient against a mocked HTTP layer: real SDK, no network.

The SDK is built on `httpx2`, so we plug an `httpx2.MockTransport` into it (respx only patches
the `httpx` package). This exercises request serialisation and response parsing for real.
"""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from baton.llm import (
    AnthropicClient,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from baton.llm.anthropic_client import FALLBACK_BETA

Handler = Callable[[httpx2.Request], httpx2.Response]


def _message(content: list[dict[str, object]], stop_reason: str) -> dict[str, object]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 42, "output_tokens": 7},
    }


def _client(
    handler: Handler, *, fallbacks: bool = True
) -> tuple[AnthropicClient, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            {
                "path": request.url.path,
                "headers": dict(request.headers),
                "body": json.loads(request.content),
            }
        )
        return handler(request)

    sdk = anthropic.Anthropic(
        api_key="test-key-not-real",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(recording)),
        max_retries=0,
    )
    return AnthropicClient(model="claude-sonnet-5-5", client=sdk, fallbacks=fallbacks), seen


def test_text_response_maps_to_llm_response() -> None:
    client, seen = _client(
        lambda _: httpx2.Response(200, json=_message([{"type": "text", "text": "Hi!"}], "end_turn"))
    )
    result = client.complete("Be brief.", [Msg(role="user", content="hello")], max_tokens=256)

    assert result.text == "Hi!"
    assert result.tool_calls == []
    assert result.stop_reason == "end_turn"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (42, 7)

    body = seen[0]["body"]
    assert seen[0]["path"] == "/v1/messages"
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "Be brief."
    assert body["max_tokens"] == 256
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert "temperature" not in body  # rejected by the current API
    assert "tools" not in body


def test_fallbacks_beta_is_sent_by_default_and_can_be_disabled() -> None:
    ok = lambda _: httpx2.Response(200, json=_message([], "end_turn"))  # noqa: E731
    client, seen = _client(ok)
    client.complete("s", [Msg(role="user", content="x")])
    assert seen[0]["body"]["fallbacks"] == "default"
    assert FALLBACK_BETA in seen[0]["headers"]["anthropic-beta"]

    client, seen = _client(ok, fallbacks=False)
    client.complete("s", [Msg(role="user", content="x")])
    assert "fallbacks" not in seen[0]["body"]
    assert "anthropic-beta" not in seen[0]["headers"]


def test_tool_use_round_trip() -> None:
    client, seen = _client(
        lambda _: httpx2.Response(
            200,
            json=_message(
                [
                    {"type": "thinking", "thinking": "", "signature": "sig-1"},
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "lookup_order",
                        "input": {"order_number": "NW-10001"},
                    },
                ],
                "tool_use",
            ),
        )
    )
    tool = ToolSpec(
        name="lookup_order",
        description="Look up an order",
        input_schema={
            "properties": {"order_number": {"type": "string"}},
            "required": ["order_number"],
        },
    )
    result = client.complete("s", [Msg(role="user", content="where is it")], tools=[tool])

    assert result.stop_reason == "tool_use"
    assert result.text == "Checking."
    assert [(c.id, c.name, c.input) for c in result.tool_calls] == [
        ("toolu_1", "lookup_order", {"order_number": "NW-10001"})
    ]
    # Thinking is kept (in order) so it can be replayed unchanged.
    assert [b.type for b in result.content] == ["thinking", "text", "tool_use"]

    sent_tool = seen[0]["body"]["tools"][0]
    assert sent_tool["name"] == "lookup_order"
    assert sent_tool["input_schema"]["type"] == "object"
    assert sent_tool["input_schema"]["required"] == ["order_number"]


def test_history_with_blocks_serialises_to_wire_format() -> None:
    client, seen = _client(lambda _: httpx2.Response(200, json=_message([], "end_turn")))
    history = [
        Msg(role="user", content="where is NW-10001"),
        Msg(
            role="assistant",
            content=[
                ThinkingBlock(thinking="", signature="sig-1"),
                TextBlock(text="Checking."),
                ToolUseBlock(id="toolu_1", name="lookup_order", input={"order_number": "NW-10001"}),
            ],
        ),
        Msg(
            role="user",
            content=[ToolResultBlock(tool_use_id="toolu_1", content='{"status":"shipped"}')],
        ),
    ]
    client.complete("s", history)

    sent = seen[0]["body"]["messages"]
    assert sent[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}
    assert sent[1]["content"][2]["type"] == "tool_use"
    assert sent[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": '{"status":"shipped"}',
        "is_error": False,
    }


def test_refusal_and_unknown_stop_reasons() -> None:
    client, _ = _client(lambda _: httpx2.Response(200, json=_message([], "refusal")))
    assert client.complete("s", [Msg(role="user", content="x")]).stop_reason == "refusal"

    client, _ = _client(lambda _: httpx2.Response(200, json=_message([], "pause_turn")))
    assert client.complete("s", [Msg(role="user", content="x")]).stop_reason == "other"


def test_api_errors_surface_as_sdk_exceptions() -> None:
    client, _ = _client(
        lambda _: httpx2.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "bad"},
            },
        )
    )
    with pytest.raises(anthropic.BadRequestError):
        client.complete("s", [Msg(role="user", content="x")])
