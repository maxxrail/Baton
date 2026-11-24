"""The retrieval eval. The acceptance gate (recall@5 >= 0.85, default embedder) runs in CI."""

import json
from pathlib import Path

import pytest

from baton.knowledge.evaluate import evaluate, load_cases
from baton.knowledge.retrieve import Retriever
from scripts import eval_retrieval
from tests.kb_support import REPO

CASES = REPO / "evals" / "retrieval_qa.yaml"


def test_eval_set_has_the_planned_shape(knowledge_ids: set[str]) -> None:
    cases = load_cases(CASES)
    assert len(cases) == 40
    assert sum(not c.answerable for c in cases) == 8
    for case in cases:
        assert set(case.expected) <= knowledge_ids, case.question  # labels name real articles


@pytest.fixture(scope="module")
def knowledge_ids() -> set[str]:
    return {p.stem for p in (REPO / "data" / "kb").glob("*.md") if p.stem != "README"}


def test_recall_at_5_meets_the_gate_with_the_default_embedder(retriever: Retriever) -> None:
    report = evaluate(retriever, load_cases(CASES))
    assert report.recall_at_k >= eval_retrieval.min_recall() == 0.85
    assert report.mrr > 0.6
    # Not a gate in the spec, but a regression here means the abstention signal broke.
    assert report.abstained_when_unanswerable >= 0.75
    assert report.abstention_accuracy >= 0.8


def test_script_reports_metrics_and_writes_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "retrieval.json"
    assert eval_retrieval.main(["--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "recall@5:" in printed
    assert "MRR:" in printed
    assert "abstention acc:" in printed
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["embedder"] == "hashing-v1-512"
    assert len(data["results"]) == 40


def test_script_fails_below_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eval_retrieval, "min_recall", lambda: 1.01)
    assert eval_retrieval.main([]) == 1


def test_voyage_without_a_key_is_a_clear_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert eval_retrieval.main(["--embedder", "voyage"]) == 2
    assert "VOYAGE_API_KEY" in capsys.readouterr().err
