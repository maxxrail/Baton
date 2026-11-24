"""`baton-sim compare A B`: what changed between two runs, regressions first.

A scenario **regressed** if it was in both runs and either lost pass^k or its pass rate
dropped. It **improved** in the opposite case. For every scenario the checks that fail more
often in B than in A are listed, so a regression comes with its reason.
"""

from typing import Literal

from pydantic import BaseModel, Field

from baton.sim.results import SimRun

DiffStatus = Literal["regressed", "improved", "unchanged", "added", "removed"]


class ScenarioDiff(BaseModel):
    scenario_id: str
    status: DiffStatus
    a_pass_rate: float | None = None
    b_pass_rate: float | None = None
    a_pass_k: bool | None = None
    b_pass_k: bool | None = None
    newly_failing: list[str] = Field(default_factory=list)
    newly_passing: list[str] = Field(default_factory=list)


class Comparison(BaseModel):
    a: str
    b: str
    a_pass_k_rate: float
    b_pass_k_rate: float
    a_pass_rate: float
    b_pass_rate: float
    diffs: list[ScenarioDiff]

    @property
    def regressions(self) -> list[ScenarioDiff]:
        return [d for d in self.diffs if d.status == "regressed"]


def _fail_rates(run: SimRun) -> dict[str, dict[str, float]]:
    """scenario -> check -> share of that scenario's runs in which it failed."""
    return {
        s.scenario_id: {name: n / max(1, s.runs) for name, n in s.failed_criteria.items()}
        for s in run.scenarios
    }


def compare(a: SimRun, b: SimRun) -> Comparison:
    a_by = {s.scenario_id: s for s in a.scenarios}
    b_by = {s.scenario_id: s for s in b.scenarios}
    a_fail, b_fail = _fail_rates(a), _fail_rates(b)
    diffs: list[ScenarioDiff] = []
    for sid in sorted(set(a_by) | set(b_by)):
        sa, sb = a_by.get(sid), b_by.get(sid)
        if sa is None and sb is not None:
            diffs.append(
                ScenarioDiff(
                    scenario_id=sid, status="added", b_pass_rate=sb.pass_rate, b_pass_k=sb.pass_k
                )
            )
            continue
        if sb is None and sa is not None:
            diffs.append(
                ScenarioDiff(
                    scenario_id=sid, status="removed", a_pass_rate=sa.pass_rate, a_pass_k=sa.pass_k
                )
            )
            continue
        assert sa is not None
        assert sb is not None
        fa, fb = a_fail.get(sid, {}), b_fail.get(sid, {})
        names = set(fa) | set(fb)
        worse = sorted(c for c in names if fb.get(c, 0.0) > fa.get(c, 0.0))
        better = sorted(c for c in names if fb.get(c, 0.0) < fa.get(c, 0.0))
        status: DiffStatus = "unchanged"
        if (sa.pass_k and not sb.pass_k) or sb.pass_rate < sa.pass_rate:
            status = "regressed"
        elif (sb.pass_k and not sa.pass_k) or sb.pass_rate > sa.pass_rate:
            status = "improved"
        diffs.append(
            ScenarioDiff(
                scenario_id=sid,
                status=status,
                a_pass_rate=sa.pass_rate,
                b_pass_rate=sb.pass_rate,
                a_pass_k=sa.pass_k,
                b_pass_k=sb.pass_k,
                newly_failing=worse,
                newly_passing=better,
            )
        )
    order = {"regressed": 0, "improved": 1, "added": 2, "removed": 3, "unchanged": 4}
    diffs.sort(key=lambda d: (order[d.status], d.scenario_id))
    return Comparison(
        a=a.id,
        b=b.id,
        a_pass_k_rate=a.totals.pass_k_rate,
        b_pass_k_rate=b.totals.pass_k_rate,
        a_pass_rate=a.totals.pass_rate,
        b_pass_rate=b.totals.pass_rate,
        diffs=diffs,
    )


def _rate(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def render_text(c: Comparison) -> str:
    lines = [
        f"A = {c.a}   B = {c.b}",
        f"pass^k   {c.a_pass_k_rate * 100:.1f}% -> {c.b_pass_k_rate * 100:.1f}%",
        f"pass     {c.a_pass_rate * 100:.1f}% -> {c.b_pass_rate * 100:.1f}%",
        "",
    ]
    marks = {
        "regressed": "REGRESSED",
        "improved": "improved",
        "added": "added",
        "removed": "removed",
    }
    for d in c.diffs:
        if d.status == "unchanged" and not d.newly_failing:
            continue
        lines.append(
            f"{marks.get(d.status, d.status):10} {d.scenario_id}: "
            f"{_rate(d.a_pass_rate)} -> {_rate(d.b_pass_rate)}"
        )
        lines.extend(f"           now failing more: {name}" for name in d.newly_failing)
    unchanged = sum(1 for d in c.diffs if d.status == "unchanged")
    lines.append(f"\n{len(c.regressions)} regression(s), {unchanged} unchanged")
    return "\n".join(lines)
