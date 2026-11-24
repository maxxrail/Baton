"""Measure help-center retrieval on `evals/retrieval_qa.yaml`.

    uv run python -m scripts.eval_retrieval                 # configured embedder (default: hashing)
    uv run python -m scripts.eval_retrieval --embedder voyage   # needs VOYAGE_API_KEY
    uv run python -m scripts.eval_retrieval --json evals/reports/retrieval.json

Ingests `data/kb` into a throwaway in-memory database (your real index is untouched), runs every
question through the same `Retriever` the agent uses, and reports recall@5, MRR and abstention
accuracy. Exits 1 if recall@5 is below the gate in `evals/thresholds.yaml`, so CI can use it.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from baton.config import REPO_ROOT, get_settings
from baton.db import Base, make_engine
from baton.knowledge.evaluate import evaluate, load_cases
from baton.knowledge.ingest import ingest
from baton.knowledge.retrieve import Retriever
from baton.llm import EmbeddingError, LLMConfigError, get_embedder

DEFAULT_CASES = REPO_ROOT / "evals" / "retrieval_qa.yaml"
THRESHOLDS = REPO_ROOT / "evals" / "thresholds.yaml"


def min_recall(path: Path = THRESHOLDS) -> float:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return float(data["retrieval"]["recall_at_5"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--kb-dir", type=Path, default=None)
    parser.add_argument("--embedder", choices=["hashing", "voyage"], default=None)
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None, help="write per-question results here")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.embedder:
        settings = settings.model_copy(update={"baton_embedder": args.embedder})
    try:
        embedder = get_embedder(settings)
        engine = make_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            ingest(session, args.kb_dir or settings.kb_dir, embedder)
            retriever = Retriever.load(session, embedder)
    except (LLMConfigError, EmbeddingError) as exc:
        print(f"eval_retrieval: {exc}", file=sys.stderr)
        return 2

    report = evaluate(retriever, load_cases(args.cases), k=args.k)
    print(f"embedder: {embedder.name}, {len(retriever)} chunks")
    print("\n".join(report.summary_lines()))
    for miss in report.misses():
        print(f"  MISS {miss.case.question!r}: expected {miss.case.expected}, got {miss.retrieved}")
    for row in report.results:
        if not row.abstention_correct:
            want = "covered" if row.case.answerable else "not covered"
            print(f"  ABSTENTION want {want} (coverage {row.coverage:.2f}): {row.case.question!r}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "embedder": embedder.name,
                    "k": args.k,
                    "recall_at_k": report.recall_at_k,
                    "mrr": report.mrr,
                    "abstention_accuracy": report.abstention_accuracy,
                    "results": [r.model_dump(mode="json") for r in report.results],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")

    gate = min_recall()
    if report.recall_at_k < gate:
        print(f"FAIL: recall@{args.k} {report.recall_at_k:.3f} is below the {gate} gate")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
