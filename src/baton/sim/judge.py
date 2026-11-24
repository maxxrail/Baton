"""The LLM judge: grades a finished conversation against a scenario's `must` / `must_not`.

The judge reports facts, code decides. For each criterion it says whether the behaviour
*occurred* (with evidence turns and a rationale) through the `report_verdicts` tool; it isn't
told which criteria are `must` and which are `must_not`, so it can't lean towards "pass". Code
then sets `passed = occurred` for `must` and `passed = not occurred` for `must_not`.

**Fails closed:** an exception, no tool call, malformed output or a missing criterion fails
that criterion, with the reason in the rationale. Deterministic checks (tools, end status) are
graded in `baton.sim.checks`, never here.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field, ValidationError

from baton.llm.client import LLMClient
from baton.llm.types import Msg, ToolSpec
from baton.prompts import load_prompt
from baton.sim.pricing import usage_of
from baton.sim.results import CheckResult, TurnRecord, Usage
from baton.sim.suite import CONTROL_TOOLS

JUDGE_PROMPT = "judge"
JUDGE_TOOL = "report_verdicts"
RESULT_CHARS = 240  # per tool result shown to the judge

JUDGE_SPEC = ToolSpec(
    name=JUDGE_TOOL,
    description="Report, for every criterion, whether the behaviour occurred.",
    input_schema={
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "occurred": {"type": "boolean"},
                        "evidence_turns": {"type": "array", "items": {"type": "integer"}},
                        "rationale": {"type": "string"},
                    },
                    "required": ["id", "occurred", "evidence_turns", "rationale"],
                },
            }
        },
        "required": ["verdicts"],
    },
)

Kind = Literal["must", "must_not"]


@dataclass(frozen=True)
class Criterion:
    kind: Kind
    text: str


@dataclass(frozen=True)
class JudgeTurn:
    """What the judge sees of one turn. `actions` are one-line tool call summaries."""

    index: int
    customer: str
    agent: str
    actions: list[str] = field(default_factory=list)


class _Verdict(BaseModel):
    id: str
    occurred: bool
    evidence_turns: list[int] = Field(default_factory=list)
    rationale: str = ""


class _Output(BaseModel):
    verdicts: list[_Verdict]


@dataclass
class JudgeResult:
    checks: list[CheckResult]
    usage: Usage
    ok: bool = True  # False if the judge's output had to be failed closed


def action_lines(trace: dict[str, object]) -> list[str]:
    """One line per store tool call the agent made in a turn, as the judge reads them."""
    lines: list[str] = []
    calls = trace.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict) or call.get("source", "agent") != "agent":
            continue
        name = str(call.get("name"))
        if name in CONTROL_TOOLS:
            continue
        args = json.dumps(call.get("args") or {}, sort_keys=True)
        if call.get("ok"):
            result = json.dumps(call.get("result") or {}, sort_keys=True)[:RESULT_CHARS]
            outcome = f"ok {result}"
        elif call.get("blocked_by"):
            outcome = f"blocked by {call['blocked_by']}"
        else:
            outcome = f"failed ({call.get('error_code') or 'error'})"
        lines.append(f"{name}({args}) -> {outcome}")
    if trace.get("escalation_reason"):
        lines.append(f"handed off to a teammate: {trace['escalation_reason']}")
    return lines


def judge_turns(turns: list[TurnRecord]) -> list[JudgeTurn]:
    return [
        JudgeTurn(
            index=t.index,
            customer=t.customer,
            agent=t.agent if t.replied else "(no reply: a teammate owns the conversation)",
            actions=action_lines(t.trace),
        )
        for t in turns
    ]


def render(criteria: list[Criterion], turns: list[JudgeTurn], final_status: str) -> str:
    parts = ["<transcript>"]
    for t in turns:
        parts.append(f'<turn n="{t.index}">')
        parts.append(f"<customer>{escape(t.customer)}</customer>")
        if t.actions:
            actions = "\n".join(f"- {escape(a)}" for a in t.actions)
            parts.append(f"<agent_actions>\n{actions}\n</agent_actions>")
        parts.append(f"<agent>{escape(t.agent)}</agent>")
        parts.append("</turn>")
    parts.append(f"<final_status>{escape(final_status)}</final_status>")
    parts.append("</transcript>")
    parts.append("<criteria>")
    for i, c in enumerate(criteria, 1):
        parts.append(f'<criterion id="c{i}">{escape(c.text)}</criterion>')
    parts.append("</criteria>")
    return "\n".join(parts)


class Judge:
    def __init__(self, llm: LLMClient, prompts_dir: Path | None = None) -> None:
        self.llm = llm
        self.prompt = load_prompt(JUDGE_PROMPT, prompts_dir)

    @property
    def version(self) -> str:
        return self.prompt.version

    def grade(
        self, criteria: list[Criterion], turns: list[JudgeTurn], final_status: str
    ) -> JudgeResult:
        if not criteria:
            return JudgeResult(checks=[], usage=Usage())
        user = render(criteria, turns, final_status)
        try:
            resp = self.llm.complete(
                self.prompt.text, [Msg(role="user", content=user)], [JUDGE_SPEC], max_tokens=4000
            )
        except Exception as exc:
            reason = f"judge call failed ({type(exc).__name__})"
            return JudgeResult(checks=_failed(criteria, reason), usage=Usage(), ok=False)
        usage = usage_of(resp.model, resp.usage)
        call = next((c for c in resp.tool_calls if c.name == JUDGE_TOOL), None)
        if call is None:
            reason = "judge made no report_verdicts call"
            return JudgeResult(checks=_failed(criteria, reason), usage=usage, ok=False)
        try:
            output = _Output.model_validate(call.input)
        except ValidationError:
            reason = "judge output was malformed"
            return JudgeResult(checks=_failed(criteria, reason), usage=usage, ok=False)

        by_id: dict[str, _Verdict] = {}
        for answer in output.verdicts:
            by_id.setdefault(answer.id.strip(), answer)  # a repeated id: the first answer counts
        valid_turns = {t.index for t in turns}
        checks: list[CheckResult] = []
        ok = True
        for i, c in enumerate(criteria, 1):
            v = by_id.get(f"c{i}")
            if v is None:
                ok = False
                checks.append(_fail(c, "judge gave no verdict for this criterion"))
                continue
            passed = v.occurred if c.kind == "must" else not v.occurred
            checks.append(
                CheckResult(
                    kind=c.kind,
                    criterion=c.text,
                    passed=passed,
                    graded_by="judge",
                    occurred=v.occurred,
                    rationale=v.rationale.strip()[:1000] or "(no rationale)",
                    evidence_turns=sorted({n for n in v.evidence_turns if n in valid_turns}),
                )
            )
        return JudgeResult(checks=checks, usage=usage, ok=ok)


def _fail(c: Criterion, reason: str) -> CheckResult:
    return CheckResult(
        kind=c.kind, criterion=c.text, passed=False, graded_by="judge", rationale=reason
    )


def _failed(criteria: list[Criterion], reason: str) -> list[CheckResult]:
    return [_fail(c, reason) for c in criteria]
