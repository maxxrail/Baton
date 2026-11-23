import pytest

from baton.llm import FakeLLM, FakeLLMExhaustedError, LLMResponse, Msg, ToolCall, ToolSpec


def _user(text: str) -> list[Msg]:
    return [Msg(role="user", content=text)]


def test_returns_scripted_responses_in_order() -> None:
    llm = FakeLLM([LLMResponse(text="one"), LLMResponse(text="two")])
    assert llm.complete("sys", _user("a")).text == "one"
    assert llm.complete("sys", _user("b")).text == "two"
    assert llm.remaining == 0


def test_records_every_call() -> None:
    tool = ToolSpec(name="lookup", description="d", input_schema={"properties": {}})
    llm = FakeLLM([LLMResponse(text="ok")])
    llm.complete("system prompt", _user("hi"), tools=[tool])
    system, messages, tools = llm.calls[0]
    assert system == "system prompt"
    assert messages[0].content == "hi"
    assert tools == [tool]


def test_raises_clear_error_when_script_runs_out() -> None:
    llm = FakeLLM([LLMResponse(text="only")])
    llm.complete("s", _user("1"))
    with pytest.raises(FakeLLMExhaustedError, match="ran out of scripted responses after 1"):
        llm.complete("s", _user("2"))


def test_empty_fake_raises_immediately() -> None:
    with pytest.raises(FakeLLMExhaustedError):
        FakeLLM().complete("s", _user("1"))


def test_callable_responder_sees_system_messages_and_tools() -> None:
    def echo(system: str, messages: list[Msg], tools: list[ToolSpec] | None) -> LLMResponse:
        last = messages[-1].content
        assert isinstance(last, str)
        names = ",".join(t.name for t in tools or [])
        return LLMResponse(text=f"{system}:{last}:{names}")

    tool = ToolSpec(name="lookup", description="d", input_schema={"properties": {}})
    llm = FakeLLM(echo)
    assert llm.complete("S", _user("x")).text == "S:x:"
    assert llm.complete("S", _user("y"), tools=[tool]).text == "S:y:lookup"  # never exhausts


def test_push_appends_responses() -> None:
    llm = FakeLLM()
    llm.push(LLMResponse(text="late"))
    assert llm.complete("s", _user("1")).text == "late"


def test_as_assistant_msg_builds_blocks_from_tool_calls() -> None:
    response = LLMResponse(
        text="Let me check.",
        tool_calls=[ToolCall(id="t1", name="lookup", input={"email": "a@b.c"})],
        stop_reason="tool_use",
    )
    msg = response.as_assistant_msg()
    assert msg.role == "assistant"
    assert isinstance(msg.content, list)
    assert [b.type for b in msg.content] == ["text", "tool_use"]
