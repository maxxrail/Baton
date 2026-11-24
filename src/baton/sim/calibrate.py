"""Judge calibration: how often does the LLM judge agree with a human on labelled transcripts?

`evals/judge_calibration.yaml` holds hand-written transcripts (customer lines, the agent's
actions as the judge sees them, the agent's replies, the final status), each with one or more
criteria labelled by a person: `passed: true|false`, and a `note` on why. The judge grades
every criterion exactly as it would in a simulation; agreement is the share of labels it
matches. Disagreements are listed so the prompt (or the label) can be fixed.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from baton.sim.judge import Criterion, Judge, JudgeTurn, Kind
from baton.sim.results import Usage


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalTurn(_Strict):
    customer: str
    actions: list[str] = Field(default_factory=list)
    agent: str


class CalLabel(_Strict):
    kind: Kind
    criterion: str
    passed: bool
    note: str = ""


class CalItem(_Strict):
    id: str
    final_status: str
    turns: list[CalTurn] = Field(min_length=1)
    labels: list[CalLabel] = Field(min_length=1)


class CalSet(_Strict):
    items: list[CalItem]


class Disagreement(BaseModel):
    item: str
    kind: str
    criterion: str
    label: bool
    judge: bool
    rationale: str
    note: str


class CalibrationReport(BaseModel):
    items: int
    labels: int
    agreed: int
    agreement: float
    disagreements: list[Disagreement]
    usage: Usage
    judge_version: str


def load_calibration(path: Path) -> CalSet:
    return CalSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def calibrate(judge: Judge, cal: CalSet) -> CalibrationReport:
    agreed = labels = 0
    usage = Usage()
    disagreements: list[Disagreement] = []
    for item in cal.items:
        turns = [
            JudgeTurn(index=i, customer=t.customer, agent=t.agent, actions=t.actions)
            for i, t in enumerate(item.turns, 1)
        ]
        criteria = [Criterion(label.kind, label.criterion) for label in item.labels]
        result = judge.grade(criteria, turns, item.final_status)
        usage = usage.add(result.usage)
        for label, check in zip(item.labels, result.checks, strict=True):
            labels += 1
            if check.passed == label.passed:
                agreed += 1
            else:
                disagreements.append(
                    Disagreement(
                        item=item.id,
                        kind=label.kind,
                        criterion=label.criterion,
                        label=label.passed,
                        judge=check.passed,
                        rationale=check.rationale,
                        note=label.note,
                    )
                )
    return CalibrationReport(
        items=len(cal.items),
        labels=labels,
        agreed=agreed,
        agreement=round(agreed / labels, 4) if labels else 0.0,
        disagreements=disagreements,
        usage=usage,
        judge_version=judge.version,
    )
