"""`baton-sim`: validate, run, compare, calibrate and generate simulation suites.

```
uv run baton-sim validate                       # evals/sims (+ coverage) and evals/sims/ci
uv run baton-sim run [--suite evals/sims] [--filter adversarial] [--runs 3] [--gate]
uv run baton-sim run --suite evals/sims/ci --scripted --gate     # offline, what CI runs
uv run baton-sim compare evals/reports/sim-A evals/reports/sim-B  # exit 1 on a regression
uv run baton-sim table evals/reports/sim-A       # Markdown results table (README, EVAL_REPORT)
uv run baton-sim calibrate [--gate]              # judge vs. evals/judge_calibration.yaml
uv run baton-sim generate --aop return_item --n 8   # drafts into evals/sims/generated/
```
Exit codes: 0 ok, 1 a gate or comparison failed, 2 bad input or configuration.
"""

import argparse
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel

from baton.aop.compiler import AOPCompileError, compile_aops
from baton.api.logging import configure_logging
from baton.config import REPO_ROOT, Settings, get_settings
from baton.llm import AnthropicClient
from baton.runtime.factory import AgentUnavailableError
from baton.sim.calibrate import calibrate, load_calibration
from baton.sim.compare import compare, render_text
from baton.sim.generate import generate, write_drafts
from baton.sim.judge import Judge
from baton.sim.metrics import markdown_table
from baton.sim.results import ConversationResult, SimRun
from baton.sim.runner import RunOptions, load_checked, load_run, run_suite
from baton.sim.suite import CoverageRule, SuiteError, coverage
from baton.tools.registry import default_registry

SIMS_DIR = REPO_ROOT / "evals" / "sims"
CI_DIR = SIMS_DIR / "ci"
CALIBRATION = REPO_ROOT / "evals" / "judge_calibration.yaml"
THRESHOLDS = REPO_ROOT / "evals" / "thresholds.yaml"


class Gate(BaseModel):
    pass_k_min: float
    pass_rate_min: float


class SimThresholds(BaseModel):
    coverage: CoverageRule
    gates: dict[str, Gate]
    judge_agreement_min: float


def thresholds(path: Path = THRESHOLDS) -> SimThresholds:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SimThresholds.model_validate(data["sims"])


def _fail(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def _progress(result: ConversationResult) -> None:
    mark = "PASS" if result.passed else "FAIL"
    failed = [f"{c.kind}: {c.criterion}" for c in result.checks if not c.passed]
    extra = f"  ({'; '.join(failed)[:160]})" if failed else ""
    print(
        f"  {mark} {result.scenario_id} #{result.run}  {len(result.turns)} turns, "
        f"{result.end_status}{extra}",
        flush=True,
    )


def gate_failures(run: SimRun, gate: Gate) -> list[str]:
    problems = []
    if run.totals.pass_k_rate < gate.pass_k_min:
        problems.append(f"pass^k {run.totals.pass_k_rate:.3f} < {gate.pass_k_min}")
    if run.totals.pass_rate < gate.pass_rate_min:
        problems.append(f"pass rate {run.totals.pass_rate:.3f} < {gate.pass_rate_min}")
    return problems


# ---------------------------------------------------------------- commands


def cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    bundle = compile_aops(settings.aops_dir, prompts_dir=settings.prompts_dir)
    rules = thresholds()
    suites = [Path(args.suite)] if args.suite else [SIMS_DIR, CI_DIR]
    failed = False
    for suite in suites:
        scripted = suite.resolve() == CI_DIR.resolve()
        try:
            scenarios = load_checked(suite, bundle, require_script=scripted)
        except SuiteError as exc:
            print(f"{suite}: INVALID")
            for problem in exc.problems:
                print(f"  - {problem}")
            failed = True
            continue
        print(f"{suite}: {len(scenarios)} scenarios valid")
        if suite.resolve() == SIMS_DIR.resolve():
            counts, problems = coverage(scenarios, rules.coverage, sorted(bundle.aops))
            print("  coverage: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
            per_aop: dict[str, int] = {}
            for sc in scenarios:
                per_aop[sc.aop] = per_aop.get(sc.aop, 0) + 1
            print("  per AOP:  " + ", ".join(f"{k} {v}" for k, v in sorted(per_aop.items())))
            for problem in problems:
                print(f"  - coverage: {problem}")
            failed = failed or bool(problems)
    return 1 if failed else 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    options = RunOptions(
        suite=Path(args.suite),
        filter=args.filter,
        runs=args.runs,
        scripted=args.scripted,
        concurrency=args.concurrency,
        out_dir=Path(args.out) if args.out else None,
    )
    mode = "scripted" if args.scripted else "live"
    print(f"running {options.suite} ({mode}, filter={args.filter}, runs={args.runs or 'per file'})")
    try:
        run, out = run_suite(settings, options, progress=_progress)
    except (SuiteError, AgentUnavailableError, AOPCompileError) as exc:
        return _fail(str(exc))
    t = run.totals
    print(
        f"\n{t.scenarios} scenarios, {t.conversations} conversations: pass^k "
        f"{t.pass_k_rate * 100:.1f}%, pass rate {t.pass_rate * 100:.1f}%, mean turns "
        f"{t.mean_turns:.1f}, escalated {t.escalation_rate * 100:.0f}%, p50/p95 "
        f"{t.p50_latency_ms / 1000:.1f}s/{t.p95_latency_ms / 1000:.1f}s, agent "
        f"${t.mean_cost_usd:.4f}/conversation, run cost ${t.sim_cost_usd:.2f}"
    )
    print(f"report: {out / 'report.html'}")
    if args.gate:
        problems = gate_failures(run, thresholds().gates[mode])
        for problem in problems:
            print(f"GATE FAILED: {problem}", file=sys.stderr)
        if problems:
            return 1
        print("gate passed")
    return 0


def cmd_compare(args: argparse.Namespace, _settings: Settings) -> int:
    try:
        a, b = load_run(Path(args.a)), load_run(Path(args.b))
    except (OSError, ValueError) as exc:
        return _fail(f"can't read a run: {exc}")
    result = compare(a, b)
    print(render_text(result))
    return 1 if result.regressions else 0


def cmd_table(args: argparse.Namespace, _settings: Settings) -> int:
    try:
        run = load_run(Path(args.run), summary=True)
    except (OSError, ValueError) as exc:
        return _fail(f"can't read the run: {exc}")
    print(markdown_table(run.scenarios, run.totals))
    t = run.totals
    print(
        f"\n{t.conversations} conversations ({run.mode}, {run.id}); p50/p95 turn latency "
        f"{t.p50_latency_ms / 1000:.1f}s/{t.p95_latency_ms / 1000:.1f}s; tool error rate "
        f"{t.tool_error_rate * 100:.1f}%; agent cost ${t.mean_cost_usd:.4f}/conversation"
    )
    return 0


def _judge_client(settings: Settings) -> AnthropicClient:
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if settings.baton_llm == "fake" or not key:
        raise AgentUnavailableError("this command needs ANTHROPIC_API_KEY")
    return AnthropicClient(
        model=settings.baton_sim_judge_model or settings.baton_model,
        api_key=key,
        fallbacks=settings.baton_llm_fallbacks,
    )


def cmd_calibrate(args: argparse.Namespace, settings: Settings) -> int:
    try:
        llm = _judge_client(settings)
    except AgentUnavailableError as exc:
        return _fail(str(exc))
    report = calibrate(Judge(llm, settings.prompts_dir), load_calibration(Path(args.file)))
    print(
        f"judge {report.judge_version} ({llm.model}): {report.agreed}/{report.labels} labels "
        f"agree = {report.agreement * 100:.1f}% over {report.items} transcripts "
        f"(${report.usage.cost_usd:.3f})"
    )
    for d in report.disagreements:
        print(f"  {d.item} [{d.kind}] {d.criterion}: label {d.label}, judge {d.judge}")
        print(f"      judge: {d.rationale[:200]}")
    if args.gate and report.agreement < thresholds().judge_agreement_min:
        print(f"GATE FAILED: agreement < {thresholds().judge_agreement_min}", file=sys.stderr)
        return 1
    return 0


def cmd_generate(args: argparse.Namespace, settings: Settings) -> int:
    try:
        llm = _judge_client(settings)
    except AgentUnavailableError as exc:
        return _fail(str(exc))
    bundle = compile_aops(settings.aops_dir, prompts_dir=settings.prompts_dir)
    if args.aop not in bundle.aops:
        return _fail(f"unknown aop {args.aop!r} (known: {', '.join(sorted(bundle.aops))})")
    drafts = generate(llm, bundle, default_registry(), args.aop, args.n, settings.prompts_dir)
    for reason in drafts.rejected:
        print(f"  dropped {reason}")
    if not drafts.scenarios:
        return _fail("no valid drafts", 1)
    path = write_drafts(drafts, args.aop, Path(args.out), model=llm.model)
    print(f"wrote {len(drafts.scenarios)} draft scenario(s) to {path}; review before using")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baton-sim", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="load and validate suites (and the coverage mix)")
    v.add_argument("--suite", help="one suite (default: evals/sims and evals/sims/ci)")

    r = sub.add_parser("run", help="run a suite and write results.json + report.html")
    r.add_argument("--suite", default=str(SIMS_DIR))
    r.add_argument("--filter", help="tag, AOP id or part of a scenario id (comma-separated)")
    r.add_argument("--runs", type=int, help="runs per scenario (default: each scenario's own)")
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--scripted", action="store_true", help="offline: play each scenario's script")
    r.add_argument("--gate", action="store_true", help="exit 1 below evals/thresholds.yaml")
    r.add_argument("--out", help="reports directory (default: evals/reports)")

    c = sub.add_parser("compare", help="diff two runs; exit 1 on a regression")
    c.add_argument("a", help="baseline run directory or results.json")
    c.add_argument("b", help="new run directory or results.json")

    t = sub.add_parser("table", help="a run's results as a Markdown table")
    t.add_argument("run", help="run directory or results.json")

    k = sub.add_parser("calibrate", help="judge agreement with hand labels")
    k.add_argument("--file", default=str(CALIBRATION))
    k.add_argument("--gate", action="store_true")

    g = sub.add_parser("generate", help="draft scenarios for an AOP (for review)")
    g.add_argument("--aop", required=True)
    g.add_argument("--n", type=int, default=8)
    g.add_argument("--out", default=str(SIMS_DIR / "generated"))
    return parser


COMMANDS = {
    "validate": cmd_validate,
    "run": cmd_run,
    "compare": cmd_compare,
    "table": cmd_table,
    "calibrate": cmd_calibrate,
    "generate": cmd_generate,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging("WARNING")  # agent turn logs would drown the progress lines
    settings = get_settings()
    try:
        return COMMANDS[args.command](args, settings)
    except AOPCompileError as exc:
        return _fail(f"AOPs don't compile: {exc}")


if __name__ == "__main__":
    sys.exit(main())
