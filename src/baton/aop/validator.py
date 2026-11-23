"""Check parsed AOPs and return `Diagnostic`s. Any `error` stops compilation.

Graph checks treat steps as nodes and transitions as edges. ESCALATE and RESOLVE are the exits.
Each problem is reported once, at its root, so one mistake doesn't produce a cascade:

- `UNREACHABLE_STEP`: no path from step 1 reaches the step.
- `NO_TERMINAL`: a reachable step has no default exit, so when none of its conditions hold the
  procedure just stops without RESOLVE or ESCALATE. (Only the last step can be like this, since
  every other step falls through to the next one.)
- `CYCLE_WITHOUT_EXIT`: a reachable loop of steps with no edge leading out to an exit.
"""

import re
from collections.abc import Iterable, Sequence

from baton.aop.models import AOP, Diagnostic, DiagnosticCode, Terminal, target_label
from baton.tools.registry import ToolRegistry

_LEADING_CONDITION = re.compile(r"^(if|when|once|in case|unless)\b", re.IGNORECASE)

LONG_STEP_WORDS = 60


def _diag(
    aop: AOP, code: DiagnosticCode, message: str, step: int | None = None, *, warning: bool = False
) -> Diagnostic:
    return Diagnostic(
        level="warning" if warning else "error",
        aop_id=aop.id,
        step=step,
        code=code,
        message=message,
        source_path=aop.source_path,
    )


def _edges(aop: AOP) -> dict[int, set[int]]:
    """Step -> the existing steps it can jump to. Bad targets are left out (reported apart)."""
    numbers = {s.number for s in aop.steps}
    return {
        s.number: {
            t.target
            for t in s.transitions
            if not isinstance(t.target, Terminal) and t.target in numbers
        }
        for s in aop.steps
    }


def _reachable_from(start: int, edges: dict[int, set[int]]) -> set[int]:
    seen: set[int] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges[node] - seen)
    return seen


def _can_finish(aop: AOP, edges: dict[int, set[int]]) -> set[int]:
    """Steps from which some path reaches ESCALATE or RESOLVE."""
    done = {
        s.number for s in aop.steps if any(isinstance(t.target, Terminal) for t in s.transitions)
    }
    changed = True
    while changed:
        changed = False
        for node, targets in edges.items():
            if node not in done and targets & done:
                done.add(node)
                changed = True
    return done


def _check_tools(aop: AOP, registry: ToolRegistry) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    for step in aop.steps:
        for name in step.tool_refs:
            if name not in registry:
                known = ", ".join(registry.names())
                out.append(
                    _diag(
                        aop,
                        DiagnosticCode.UNKNOWN_TOOL,
                        f"@{name} is not a registered tool. Known tools: {known}",
                        step.number,
                    )
                )
            elif registry.get(name).side_effect and not aop.requires_verification:
                out.append(
                    _diag(
                        aop,
                        DiagnosticCode.SIDE_EFFECT_WITHOUT_VERIFICATION,
                        f"@{name} changes customer data, so this AOP must set "
                        "'requires_verification: true'",
                        step.number,
                    )
                )
    return out


def _unreachable_hint(aop: AOP, number: int) -> str:
    """Explain the most common cause: "If X, do Y. RESOLVE." in the step before.

    Conditions apply per sentence, so a terminal in its own sentence is unconditional and the
    step never falls through. Authors almost always meant "If X, do Y and RESOLVE."
    """
    if number == 1:
        return ""
    previous = aop.step(number - 1)
    default = next((t for t in previous.transitions if t.condition is None), None)
    opens_with_condition = _LEADING_CONDITION.match(previous.text) is not None
    if default is None or default.implicit or not opens_with_condition:
        return ""
    return (
        f". Step {previous.number} always goes to {target_label(default.target)}: that keyword "
        "is in its own sentence, so the 'If ...' at the start of the step doesn't apply to it. "
        f"Put it in the same sentence (e.g. 'If ..., ... and {target_label(default.target)}.')"
    )


def _check_graph(aop: AOP) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    numbers = {s.number for s in aop.steps}
    for step in aop.steps:
        for t in step.transitions:
            if not isinstance(t.target, Terminal) and t.target not in numbers:
                out.append(
                    _diag(
                        aop,
                        DiagnosticCode.BAD_TARGET,
                        f"goes to step {t.target}, but the AOP has steps 1-{len(aop.steps)}",
                        step.number,
                    )
                )

    edges = _edges(aop)
    first = aop.steps[0].number
    reachable = _reachable_from(first, edges)
    for step in aop.steps:
        if step.number not in reachable:
            out.append(
                _diag(
                    aop,
                    DiagnosticCode.UNREACHABLE_STEP,
                    "no path from step 1 leads here; add a 'go to step "
                    f"{step.number}' or remove the step" + _unreachable_hint(aop, step.number),
                    step.number,
                )
            )

    has_bad_target = {d.step for d in out if d.code is DiagnosticCode.BAD_TARGET}
    for step in aop.steps:
        if step.number in reachable and not any(t.condition is None for t in step.transitions):
            out.append(
                _diag(
                    aop,
                    DiagnosticCode.NO_TERMINAL,
                    "the procedure can end here without RESOLVE or ESCALATE: add a default "
                    "exit (e.g. 'Otherwise RESOLVE.')",
                    step.number,
                )
            )

    # Loops with no way out. A trapped loop is a set of reachable steps that can't finish and
    # only lead to each other. A loop containing a step already reported (bad target, missing
    # default exit) is skipped: fixing that step is what gives the loop its exit.
    already = has_bad_target | {d.step for d in out if d.code is DiagnosticCode.NO_TERMINAL}
    finishing = _can_finish(aop, edges)
    stuck = reachable - finishing
    reported: set[int] = set()
    for node in sorted(stuck):
        if node in reported:
            continue
        component = {m for m in _reachable_from(node, edges) if node in _reachable_from(m, edges)}
        leads_out = any(edges[m] - component for m in component)
        is_loop = len(component) > 1 or node in edges[node]
        reported |= component
        if is_loop and not leads_out and not component & already:
            steps = ", ".join(str(n) for n in sorted(component))
            out.append(
                _diag(
                    aop,
                    DiagnosticCode.CYCLE_WITHOUT_EXIT,
                    f"steps {steps} loop forever: none of them leads to RESOLVE, ESCALATE or "
                    "a step outside the loop",
                    min(component),
                )
            )
    return out


def _check_content(aop: AOP) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    if not aop.triggers:
        out.append(
            _diag(
                aop,
                DiagnosticCode.MISSING_TRIGGERS,
                "add at least one trigger so the router knows when to use this AOP",
            )
        )
    for step in aop.steps:
        words = len(step.text.split())
        if words > LONG_STEP_WORDS:
            out.append(
                _diag(
                    aop,
                    DiagnosticCode.LONG_STEP,
                    f"{words} words (limit {LONG_STEP_WORDS}); long steps are followed less "
                    "reliably, consider splitting it",
                    step.number,
                    warning=True,
                )
            )
    return out


def validate_aop(aop: AOP, registry: ToolRegistry) -> list[Diagnostic]:
    """Every check that only needs one AOP."""
    return _check_content(aop) + _check_tools(aop, registry) + _check_graph(aop)


def check_duplicate_ids(aops: Sequence[AOP]) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    first_seen: dict[str, AOP] = {}
    for aop in aops:
        if aop.id in first_seen:
            out.append(
                _diag(
                    aop,
                    DiagnosticCode.DUPLICATE_ID,
                    f"id {aop.id!r} is already used by {first_seen[aop.id].source_path}",
                )
            )
        else:
            first_seen[aop.id] = aop
    return out


def validate_aops(aops: Iterable[AOP], registry: ToolRegistry) -> list[Diagnostic]:
    aop_list = list(aops)
    diagnostics = check_duplicate_ids(aop_list)
    for aop in aop_list:
        diagnostics.extend(validate_aop(aop, registry))
    return diagnostics


def has_errors(diagnostics: Iterable[Diagnostic]) -> bool:
    return any(d.level == "error" for d in diagnostics)
