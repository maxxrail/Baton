"""Render an AOP as a Mermaid flowchart (for the CLI, the API and the console's AOP browser).

Step nodes are `s1`, `s2`, ...; the terminals are `resolve` and `escalate`. Conditional edges
carry their condition as a label, the default edge out of a branching step is labelled
"otherwise", and implicit fall-through edges are dotted.
"""

from collections.abc import Sequence

from baton.aop.models import AOP, CompiledAOP, CompiledStep, Step, Target, Terminal

LABEL_MAX = 48


def _escape(text: str) -> str:
    """Make text safe inside a quoted Mermaid label."""
    return text.replace('"', "#quot;").replace("\n", " ")


def _short(text: str, limit: int = LABEL_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _node(target: Target) -> str:
    return target.value.lower() if isinstance(target, Terminal) else f"s{target}"


def to_mermaid(aop: AOP | CompiledAOP) -> str:
    steps: Sequence[Step | CompiledStep] = aop.steps
    lines = ["flowchart TD", f'    start(["{_escape(_short(f"{aop.ref}: {aop.name}"))}"])']
    for step in steps:
        lines.append(f'    s{step.number}["{_escape(f"{step.number}. {_short(step.text)}")}"]')

    used_terminals: set[Terminal] = set()
    lines.append(f"    start --> s{steps[0].number}")
    for step in steps:
        branches = any(t.condition is not None for t in step.transitions)
        for t in step.transitions:
            if isinstance(t.target, Terminal):
                used_terminals.add(t.target)
            arrow = "-.->" if t.implicit else "-->"
            label = t.condition if t.condition is not None else ("otherwise" if branches else None)
            label_part = f'|"{_escape(_short(label, 40))}"|' if label else ""
            lines.append(f"    s{step.number} {arrow}{label_part} {_node(t.target)}")

    if Terminal.RESOLVE in used_terminals:
        lines.append('    resolve(["RESOLVE"])')
    if Terminal.ESCALATE in used_terminals:
        lines.append('    escalate{{"ESCALATE"}}')
    lines.append("    classDef terminal fill:#e8f5e9,stroke:#2e7d32")
    lines.append("    classDef handoff fill:#fff3e0,stroke:#ef6c00")
    if Terminal.RESOLVE in used_terminals:
        lines.append("    class resolve terminal")
    if Terminal.ESCALATE in used_terminals:
        lines.append("    class escalate handoff")
    return "\n".join(lines) + "\n"
