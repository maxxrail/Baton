"""Score a `Retriever` on `evals/retrieval_qa.yaml`.

Metrics, over the questions that have an answer in the help center:
- **recall@k**: the share of each question's expected articles found among the top-k chunks,
  averaged over questions;
- **MRR**: the mean of 1 / (rank of the first chunk from an expected article), 0 if none;

and over all questions:
- **abstention accuracy**: the retriever's `covered` flag is right, i.e. False for the
  questions the help center doesn't answer and True for the ones it does. Reported both
  overall and split, because "always abstain" would score 8/40 right on one side only.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from baton.knowledge.retrieve import Retriever


class RetrievalCase(BaseModel):
    question: str
    expected: list[str] = Field(default_factory=list)

    @property
    def answerable(self) -> bool:
        return bool(self.expected)


class _CaseFile(BaseModel):
    questions: list[RetrievalCase]


class CaseResult(BaseModel):
    case: RetrievalCase
    retrieved: list[str]  # article id of each top-k chunk, in rank order
    covered: bool
    coverage: float

    @property
    def recall(self) -> float:
        if not self.case.answerable:
            return 0.0
        found = set(self.retrieved) & set(self.case.expected)
        return len(found) / len(set(self.case.expected))

    @property
    def reciprocal_rank(self) -> float:
        for rank, article_id in enumerate(self.retrieved, start=1):
            if article_id in self.case.expected:
                return 1 / rank
        return 0.0

    @property
    def abstention_correct(self) -> bool:
        return self.covered == self.case.answerable


class RetrievalReport(BaseModel):
    k: int
    results: list[CaseResult]

    @property
    def answerable(self) -> list[CaseResult]:
        return [r for r in self.results if r.case.answerable]

    @property
    def unanswerable(self) -> list[CaseResult]:
        return [r for r in self.results if not r.case.answerable]

    @property
    def recall_at_k(self) -> float:
        rows = self.answerable
        return sum(r.recall for r in rows) / len(rows) if rows else 0.0

    @property
    def mrr(self) -> float:
        rows = self.answerable
        return sum(r.reciprocal_rank for r in rows) / len(rows) if rows else 0.0

    @property
    def abstention_accuracy(self) -> float:
        return sum(r.abstention_correct for r in self.results) / len(self.results)

    @property
    def abstained_when_unanswerable(self) -> float:
        rows = self.unanswerable
        return sum(not r.covered for r in rows) / len(rows) if rows else 0.0

    @property
    def answered_when_answerable(self) -> float:
        rows = self.answerable
        return sum(r.covered for r in rows) / len(rows) if rows else 0.0

    def misses(self) -> list[CaseResult]:
        return [r for r in self.answerable if r.recall < 1.0]

    def summary_lines(self) -> list[str]:
        n_ans, n_un = len(self.answerable), len(self.unanswerable)
        return [
            f"questions: {len(self.results)} ({n_ans} answerable, {n_un} not in the KB)",
            f"recall@{self.k}:           {self.recall_at_k:.3f}",
            f"MRR:                {self.mrr:.3f}",
            f"abstention acc:     {self.abstention_accuracy:.3f} "
            f"(abstained on {self.abstained_when_unanswerable:.0%} of unanswerable, "
            f"answered {self.answered_when_answerable:.0%} of answerable)",
        ]


def load_cases(path: Path) -> list[RetrievalCase]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _CaseFile.model_validate(data).questions


def evaluate(retriever: Retriever, cases: list[RetrievalCase], k: int = 5) -> RetrievalReport:
    results = []
    for case in cases:
        found = retriever.search(case.question, top_k=k)
        results.append(
            CaseResult(
                case=case,
                retrieved=[h.chunk.article_id for h in found.hits],
                covered=found.covered,
                coverage=found.coverage,
            )
        )
    return RetrievalReport(k=k, results=results)
