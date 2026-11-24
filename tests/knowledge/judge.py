"""A scripted grounding judge for tests.

`judge(rule)` returns an LLM-shaped responder that reads the numbered sentences out of the
grounding prompt and labels each with `rule(sentence) -> (kind, supported, sources)`, the way
the real judge would via the `report_grounding` tool.
"""

import re
from collections.abc import Callable

from baton.knowledge.grounding import GROUNDING_TOOL
from baton.llm import LLMResponse, Msg, ToolCall, ToolSpec, Usage

Label = tuple[str, bool, list[str]]
Rule = Callable[[str], Label]

_SENTENCE_RE = re.compile(r'<sentence n="(\d+)">(.*?)</sentence>')
_SOURCE_RE = re.compile(r'<source id="([^"]+)"')


def sentences_in(messages: list[Msg]) -> list[tuple[int, str]]:
    content = str(messages[-1].content)
    return [(int(n), text) for n, text in _SENTENCE_RE.findall(content)]


def sources_in(messages: list[Msg]) -> list[str]:
    return _SOURCE_RE.findall(str(messages[-1].content))


def judge(rule: Rule) -> Callable[[str, list[Msg], list[ToolSpec] | None], LLMResponse]:
    def respond(system: str, messages: list[Msg], tools: list[ToolSpec] | None) -> LLMResponse:
        assert tools is not None
        assert tools[0].name == GROUNDING_TOOL
        labels = []
        for n, text in sentences_in(messages):
            kind, supported, sources = rule(text)
            labels.append({"n": n, "kind": kind, "supported": supported, "sources": sources})
        return LLMResponse(
            tool_calls=[ToolCall(id="g", name=GROUNDING_TOOL, input={"sentences": labels})],
            usage=Usage(input_tokens=200, output_tokens=40),
            model="fake-judge",
        )

    return respond


def supported_if(**facts: str) -> Rule:
    """Claims containing a key phrase are supported by that source; questions and offers of
    help are `other`; every other sentence is an unsupported claim.

        supported_if(**{"6-10 business days": "kb:shipping-international"})
    """

    def rule(text: str) -> Label:
        lowered = text.lower()
        not_claims = ("hi ", "hello", "thanks", "i can ", "i'm not sure")
        if text.rstrip().endswith("?") or lowered.startswith(not_claims) or "teammate" in lowered:
            return ("other", False, [])
        for phrase, source in facts.items():
            if phrase.lower() in lowered:
                return ("claim", True, [source])
        return ("claim", False, [])

    return rule
