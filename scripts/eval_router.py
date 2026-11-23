"""Measure router accuracy on the labeled cases in `evals/router_cases.yaml`.

    uv run python -m scripts.eval_router            # real model if ANTHROPIC_API_KEY is set
    uv run python -m scripts.eval_router --top-k 3  # tighter pre-filter
    uv run python -m scripts.eval_router --json evals/reports/router.json

Without a key it can't run the LLM classifier, so it reports the offline numbers only: the
pre-filter's recall (is the right AOP among the candidates?) and its top-1 accuracy alone.
"""

import argparse
import json
import sys
from pathlib import Path

from baton.aop.compiler import compile_aops
from baton.aop.router import Router
from baton.aop.router_eval import evaluate, load_router_cases
from baton.config import REPO_ROOT, get_settings
from baton.llm import FakeLLM, get_llm

DEFAULT_CASES = REPO_ROOT / "evals" / "router_cases.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None, help="write per-case results here")
    args = parser.parse_args(argv)

    settings = get_settings()
    bundle = compile_aops(settings.aops_dir, prompts_dir=settings.prompts_dir)
    cases = load_router_cases(args.cases)
    llm = get_llm(settings)

    labeled = [c for c in cases if c.expected is not None]
    offline = Router(bundle, FakeLLM(), top_k=args.top_k)
    recall = sum(
        c.expected in [x.aop_id for x in offline.prefilter(c.message, c.history_msgs())]
        for c in labeled
    )
    top1 = sum(offline.best_match(c.message) == c.expected for c in labeled)
    print(f"cases: {len(cases)} ({len(labeled)} with an AOP label), top_k={args.top_k}")
    print(f"pre-filter recall@{args.top_k}: {recall}/{len(labeled)} = {recall / len(labeled):.1%}")
    print(f"pre-filter top-1 alone:      {top1}/{len(labeled)} = {top1 / len(labeled):.1%}")

    if isinstance(llm, FakeLLM):
        print("\nNo ANTHROPIC_API_KEY set (or BATON_LLM=fake): skipping the LLM classifier.")
        return 0

    report = evaluate(Router(bundle, llm, top_k=args.top_k), cases)
    correct = sum(r.correct for r in report.results)
    model = settings.baton_model
    print(f"\nrouter accuracy ({model}): {correct}/{len(cases)} = {report.accuracy:.1%}")
    for miss in report.misses():
        print(
            f"  MISS expected={miss.case.expected} got={miss.decision.aop_id} "
            f"conf={miss.decision.confidence:.2f} | {miss.case.message!r} | "
            f"{miss.decision.rationale}"
        )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "model": settings.baton_model,
                    "top_k": args.top_k,
                    "accuracy": report.accuracy,
                    "prefilter_recall": report.prefilter_recall,
                    "results": [r.model_dump(mode="json") for r in report.results],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
