"""Metrics over conversation results.

- **pass rate**: passed runs / runs, per scenario and overall.
- **pass^k**: a scenario passes^k only if *all* k of its runs passed. The headline number is
  the share of scenarios that pass^k. It punishes flakiness, which an average hides: a scenario
  that passes 2 of 3 times has a 67% pass rate but fails pass^3, and a customer who gets the
  bad run doesn't care about the average.
- mean customer turns, tool error rate (store tool calls that failed, not counting guardrail
  blocks), escalation rate, p50/p95 agent turn latency, and agent tokens + estimated cost per
  conversation. `sim_cost_usd` adds the persona and judge, i.e. what the run itself cost.
"""

import math
from collections import Counter

from baton.sim.results import ConversationResult, ScenarioSummary, Totals
from baton.sim.suite import Scenario


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 1)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def summarize_scenario(scenario: Scenario, results: list[ConversationResult]) -> ScenarioSummary:
    failed: Counter[str] = Counter()
    for r in results:
        for c in r.checks:
            if not c.passed:
                failed[f"{c.kind}: {c.criterion}"] += 1
    passed = sum(r.passed for r in results)
    calls = sum(r.tool_calls for r in results)
    return ScenarioSummary(
        scenario_id=scenario.id,
        aop=scenario.aop,
        tags=scenario.tags,
        description=scenario.description,
        runs=len(results),
        passed_runs=passed,
        pass_rate=_rate(passed, len(results)),
        pass_k=bool(results) and passed == len(results),
        mean_turns=_mean([len(r.turns) for r in results]),
        tool_error_rate=_rate(sum(r.tool_errors for r in results), calls),
        escalation_rate=_rate(sum(r.escalated for r in results), len(results)),
        p50_latency_ms=percentile([ms for r in results for ms in r.turn_latencies_ms], 50),
        p95_latency_ms=percentile([ms for r in results for ms in r.turn_latencies_ms], 95),
        mean_tokens=_mean(
            [r.agent_usage.input_tokens + r.agent_usage.output_tokens for r in results]
        ),
        mean_cost_usd=_mean([r.agent_usage.cost_usd for r in results]),
        failed_criteria=dict(failed.most_common()),
    )


def summarize(
    scenarios: list[Scenario], results: list[ConversationResult]
) -> tuple[list[ScenarioSummary], Totals]:
    by_id: dict[str, list[ConversationResult]] = {}
    for r in results:
        by_id.setdefault(r.scenario_id, []).append(r)
    summaries = [summarize_scenario(sc, by_id.get(sc.id, [])) for sc in scenarios]
    calls = sum(r.tool_calls for r in results)
    latencies = [ms for r in results for ms in r.turn_latencies_ms]
    totals = Totals(
        scenarios=len(summaries),
        conversations=len(results),
        pass_rate=_rate(sum(r.passed for r in results), len(results)),
        pass_k_rate=_rate(sum(s.pass_k for s in summaries), len(summaries)),
        mean_turns=_mean([len(r.turns) for r in results]),
        tool_error_rate=_rate(sum(r.tool_errors for r in results), calls),
        escalation_rate=_rate(sum(r.escalated for r in results), len(results)),
        p50_latency_ms=percentile(latencies, 50),
        p95_latency_ms=percentile(latencies, 95),
        mean_tokens=_mean(
            [r.agent_usage.input_tokens + r.agent_usage.output_tokens for r in results]
        ),
        mean_cost_usd=_mean([r.agent_usage.cost_usd for r in results]),
        sim_cost_usd=round(
            sum(
                r.agent_usage.cost_usd + r.persona_usage.cost_usd + r.judge_usage.cost_usd
                for r in results
            ),
            4,
        ),
    )
    return summaries, totals


def markdown_table(summaries: list[ScenarioSummary], totals: Totals) -> str:
    """Results as Markdown: one row per AOP plus the total (for the README and EVAL_REPORT)."""
    by_aop: dict[str, list[ScenarioSummary]] = {}
    for s in summaries:
        by_aop.setdefault(s.aop, []).append(s)
    lines = [
        "| Procedure | Scenarios | pass^k | Pass rate | Escalated | Mean turns |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for aop, rows in sorted(by_aop.items()):
        runs = sum(r.runs for r in rows)
        passed = sum(r.passed_runs for r in rows)
        escalated = sum(r.escalation_rate * r.runs for r in rows)
        turns = sum(r.mean_turns * r.runs for r in rows)
        lines.append(
            f"| `{aop}` | {len(rows)} | {sum(r.pass_k for r in rows)}/{len(rows)} | "
            f"{_pct(_rate(passed, runs))} | {_pct(escalated / runs if runs else 0)} | "
            f"{turns / runs if runs else 0:.1f} |"
        )
    t = totals
    lines.append(
        f"| **All** | **{t.scenarios}** | **{_pct(t.pass_k_rate)}** | **{_pct(t.pass_rate)}** | "
        f"{_pct(t.escalation_rate)} | {t.mean_turns:.1f} |"
    )
    return "\n".join(lines)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"
