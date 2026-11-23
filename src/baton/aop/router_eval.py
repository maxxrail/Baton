"""Load the labeled router cases (`evals/router_cases.yaml`) and score a router on them."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from baton.aop.router import RouteDecision, Router
from baton.llm.types import Msg


class RouterCase(BaseModel):
    message: str
    expected: str | None
    history: list[str] = Field(default_factory=list)

    def history_msgs(self) -> list[Msg]:
        return [Msg(role="user", content=text) for text in self.history]


class _CaseFile(BaseModel):
    cases: list[RouterCase]


class CaseResult(BaseModel):
    case: RouterCase
    decision: RouteDecision
    prefilter_hit: bool = Field(description="expected AOP was among the candidates (or None)")

    @property
    def correct(self) -> bool:
        return self.decision.aop_id == self.case.expected


class RouterReport(BaseModel):
    results: list[CaseResult]

    @property
    def accuracy(self) -> float:
        return sum(r.correct for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def prefilter_recall(self) -> float:
        labeled = [r for r in self.results if r.case.expected is not None]
        return sum(r.prefilter_hit for r in labeled) / len(labeled) if labeled else 0.0

    def misses(self) -> list[CaseResult]:
        return [r for r in self.results if not r.correct]


def load_router_cases(path: Path) -> list[RouterCase]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _CaseFile.model_validate(data).cases


def evaluate(router: Router, cases: list[RouterCase]) -> RouterReport:
    results = []
    for case in cases:
        decision = router.route(case.message, case.history_msgs())
        hit = case.expected is None or case.expected in decision.candidates
        results.append(CaseResult(case=case, decision=decision, prefilter_hit=hit))
    return RouterReport(results=results)
