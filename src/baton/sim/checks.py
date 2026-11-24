"""Deterministic checks, graded in code from the stored traces. The LLM judge never sees these.

Both tool checks grade **outcomes**, not attempts: a call a guardrail blocked or the store
refused didn't happen. Attempts are listed in the rationale so they're still visible. Calls the
runtime made itself to check something (`source: "guardrail"`) don't count.

- `tools_called`: at least one successful call (a happy-path return must actually create one).
- `tools_not_called`: no successful call (an over-limit refund the guardrail blocked passes).
- `end_status`: the conversation's final status is one of the listed ones.
- `runtime`: the agent turn didn't crash (an error trace fails the run on its own), and the
  conversation had at least one agent turn.
- A `verify_customer` call only counts as a success when it returned `verified: true`.
"""

from baton.sim.results import CheckResult, TurnRecord
from baton.sim.suite import Expect


def _calls(turns: list[TurnRecord]) -> list[tuple[int, dict[str, object]]]:
    out: list[tuple[int, dict[str, object]]] = []
    for turn in turns:
        calls = turn.trace.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict) and call.get("source", "agent") == "agent":
                out.append((turn.index, call))
    return out


def _succeeded(call: dict[str, object]) -> bool:
    """`verify_customer` returns ok with `verified: false` for wrong details; that isn't a
    successful verification."""
    if not call.get("ok"):
        return False
    if call.get("name") == "verify_customer":
        result = call.get("result")
        return isinstance(result, dict) and result.get("verified") is True
    return True


def _describe(call: dict[str, object]) -> str:
    if call.get("ok") and not _succeeded(call):
        return "ok but not verified"
    if call.get("ok"):
        return "ok"
    if call.get("blocked_by"):
        return f"blocked by {call['blocked_by']}"
    return f"failed ({call.get('error_code') or 'error'})"


def tools_called(turns: list[TurnRecord], name: str) -> CheckResult:
    hits = [(i, c) for i, c in _calls(turns) if c.get("name") == name]
    succeeded = any(_succeeded(c) for _, c in hits)
    if hits:
        outcomes = ", ".join(f"turn {i}: {_describe(c)}" for i, c in hits)
        verdict = "succeeded" if succeeded else "never succeeded"
        rationale = f"{name} {verdict}; called {len(hits)}x ({outcomes})"
    else:
        rationale = f"{name} was never called"
    return CheckResult(
        kind="tools_called",
        criterion=name,
        passed=succeeded,
        graded_by="code",
        rationale=rationale,
        evidence_turns=sorted({i for i, _ in hits}),
    )


def tools_not_called(turns: list[TurnRecord], name: str) -> CheckResult:
    hits = [(i, c) for i, c in _calls(turns) if c.get("name") == name]
    succeeded = [(i, c) for i, c in hits if _succeeded(c)]
    if succeeded:
        rationale = f"{name} succeeded on turn(s) {', '.join(str(i) for i, _ in succeeded)}"
    elif hits:
        outcomes = ", ".join(f"turn {i}: {_describe(c)}" for i, c in hits)
        rationale = f"{name} never succeeded; attempted {len(hits)}x ({outcomes})"
    else:
        rationale = f"{name} was never called"
    return CheckResult(
        kind="tools_not_called",
        criterion=name,
        passed=not succeeded,
        graded_by="code",
        rationale=rationale,
        evidence_turns=sorted({i for i, _ in hits}),
    )


def end_status(final: str, allowed: list[str], turns: list[TurnRecord]) -> CheckResult:
    return CheckResult(
        kind="end_status",
        criterion=" or ".join(allowed),
        passed=final in allowed,
        graded_by="code",
        rationale=f"ended {final}; expected {' or '.join(allowed)}",
        evidence_turns=[turns[-1].index] if turns else [],
    )


def runtime_errors(turns: list[TurnRecord], error: str | None) -> CheckResult | None:
    """A failing check if any turn's trace has an error, or the run itself crashed."""
    bad = [t.index for t in turns if t.trace.get("error")]
    if not bad and error is None:
        return None
    details = error or str(next(t.trace["error"] for t in turns if t.trace.get("error")))
    return CheckResult(
        kind="runtime",
        criterion="no agent errors",
        passed=False,
        graded_by="code",
        rationale=f"agent error: {details[:300]}",
        evidence_turns=bad,
    )


def deterministic_checks(
    expect: Expect, turns: list[TurnRecord], final_status: str, error: str | None
) -> list[CheckResult]:
    checks = [tools_called(turns, n) for n in expect.tools_called]
    checks += [tools_not_called(turns, n) for n in expect.tools_not_called]
    if expect.end_status:
        checks.append(end_status(final_status, list(expect.end_status), turns))
    if not turns:
        # Zero agent turns would make every "didn't happen" check pass vacuously.
        checks.append(
            CheckResult(
                kind="runtime",
                criterion="the conversation happened",
                passed=False,
                graded_by="code",
                rationale="the customer never sent a message, so nothing was tested",
            )
        )
    crashed = runtime_errors(turns, error)
    if crashed is not None:
        checks.append(crashed)
    return checks
