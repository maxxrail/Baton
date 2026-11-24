"""Scripted (offline) models for a scenario with a `script:`: the deterministic CI suite.

Everything still goes through the real code: the real agent runtime, tools, guardrails and mock
store, the real persona and judge plumbing. Only the model outputs are fixed:

- the persona says the script's customer lines in order, then `[DONE]`;
- the agent model plays the router (`script.turns[i].route`), the handoff writer (a fixed
  summary), the grounding judge (every sentence labelled `other`, so it passes) and the agent
  itself (the turn's `agent` steps, in order);
- the judge answers from `script.judge`, defaulting to "every `must` happened, no `must_not`
  did".

A script that runs out mid-turn makes that agent turn fail (an error trace, so the run fails
with the reason); steps left over at the end of a turn are reported the same way.
"""

import re
from collections import deque
from collections.abc import Iterator

from baton.aop.router import ROUTE_TOOL
from baton.knowledge.grounding import GROUNDING_TOOL
from baton.llm.fake import FakeLLM
from baton.llm.types import (
    LLMResponse,
    Msg,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
    stream_response,
)
from baton.runtime.escalation import HANDOFF_TOOL
from baton.sim.judge import JUDGE_TOOL
from baton.sim.persona import DONE
from baton.sim.suite import Scenario, ScriptStep

SCRIPTED_MODEL = "scripted"
_USAGE = Usage(input_tokens=100, output_tokens=20)
_SENTENCE_RE = re.compile(r'<sentence n="(\d+)">')
_CRITERION_RE = re.compile(r'<criterion id="(c\d+)">(.*?)</criterion>', re.DOTALL)


class ScriptExhaustedError(RuntimeError):
    pass


def _response(step: ScriptStep, turn: int, n: int) -> LLMResponse:
    calls = [
        ToolCall(id=f"s{turn}_{n}_{i}", name=c.tool, input=dict(c.args))
        for i, c in enumerate(step.calls)
    ]
    return LLMResponse(
        text=step.say or "",
        tool_calls=calls,
        usage=_USAGE,
        stop_reason="tool_use" if calls else "end_turn",
        model=SCRIPTED_MODEL,
    )


class ScriptedAgentLLM:
    def __init__(self, scenario: Scenario) -> None:
        assert scenario.script is not None
        self.turns = scenario.script.turns
        self.route_to = "none"
        self.turn = 0
        self._steps: deque[LLMResponse] = deque()
        self.problems: list[str] = []

    def before_turn(self, index: int) -> None:
        """Load turn `index`'s (1-based) script; note any steps the last turn didn't use."""
        if self._steps:
            self.problems.append(f"turn {self.turn}: {len(self._steps)} scripted step(s) unused")
        self.turn = index
        self._steps.clear()
        if index <= len(self.turns):
            turn = self.turns[index - 1]
            if turn.route is not None:
                self.route_to = turn.route
            self._steps.extend(_response(s, index, n) for n, s in enumerate(turn.agent))

    def finish(self) -> str | None:
        self.before_turn(self.turn + 1)
        return "; ".join(self.problems) or None

    def complete(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 16000,
    ) -> LLMResponse:
        names = {t.name for t in tools or []}
        if ROUTE_TOOL in names:
            args = {"aop_id": self.route_to, "confidence": 0.9, "rationale": "scripted"}
            return LLMResponse(
                tool_calls=[ToolCall(id="route", name=ROUTE_TOOL, input=args)],
                usage=_USAGE,
                model=SCRIPTED_MODEL,
            )
        if HANDOFF_TOOL in names:
            args = {
                "customer": "scripted customer",
                "intent": "scripted scenario",
                "tried": ["followed the script"],
                "needed": "review the scripted conversation",
            }
            return LLMResponse(
                tool_calls=[ToolCall(id="handoff", name=HANDOFF_TOOL, input=args)],
                usage=_USAGE,
                model=SCRIPTED_MODEL,
            )
        if GROUNDING_TOOL in names:
            content = str(messages[-1].content)
            labels = [{"n": int(n), "kind": "other"} for n in _SENTENCE_RE.findall(content)]
            return LLMResponse(
                tool_calls=[ToolCall(id="g", name=GROUNDING_TOOL, input={"sentences": labels})],
                usage=_USAGE,
                model=SCRIPTED_MODEL,
            )
        if not self._steps:
            raise ScriptExhaustedError(f"turn {self.turn}: the agent script ran out")
        return self._steps.popleft()

    def stream(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 16000,
    ) -> Iterator[StreamEvent]:
        yield from stream_response(self.complete(system, messages, tools, max_tokens=max_tokens))


def scripted_persona(scenario: Scenario) -> FakeLLM:
    assert scenario.script is not None
    lines = [t.customer for t in scenario.script.turns] + [DONE]
    return FakeLLM([LLMResponse(text=line, usage=_USAGE, model=SCRIPTED_MODEL) for line in lines])


def scripted_judge(scenario: Scenario) -> FakeLLM:
    assert scenario.script is not None
    occurred = {text: True for text in scenario.expect.must}
    occurred.update({text: False for text in scenario.expect.must_not})
    occurred.update(scenario.script.judge)

    def respond(system: str, messages: list[Msg], tools: list[ToolSpec] | None) -> LLMResponse:
        content = str(messages[-1].content)
        verdicts = [
            {
                "id": cid,
                "occurred": occurred.get(_unescape(text), False),
                "evidence_turns": [],
                "rationale": "scripted verdict",
            }
            for cid, text in _CRITERION_RE.findall(content)
        ]
        return LLMResponse(
            tool_calls=[ToolCall(id="j", name=JUDGE_TOOL, input={"verdicts": verdicts})],
            usage=_USAGE,
            model=SCRIPTED_MODEL,
        )

    return FakeLLM(respond)


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
