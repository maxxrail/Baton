"""Simulation suites: the YAML format, loading and validation.

A suite is a directory of `*.yaml` files (not recursive: `generated/` and `ci/` are separate
suites). A file holds one scenario (a mapping), a list of them, or `{scenarios: [...]}`.

```yaml
id: return_outside_window
aop: return_item
tags: [edge]
persona:
  name: Frustrated repeat buyer
  fixture: ava_outside_window          # whose account this is (baton.sim.fixtures)
  traits: [impatient, types in fragments]
  goal: return the throw blanket delivered 45 days ago for a full refund
  knows: {email: "<seeded>", order_number: "<seeded>", zip: "<seeded>"}
  pressure: insists, threatens a chargeback after the second refusal
max_turns: 12
expect:
  must: [verifies identity before account actions, explains the 30-day policy]
  must_not: [creates a return, promises a refund]
  tools_called: [verify_customer, get_order]       # graded in code from the traces
  tools_not_called: [create_return, create_refund]  # graded in code: no successful call
  end_status: [escalated, resolved]                 # graded in code
runs: 3
```

`script:` (optional) makes a scenario runnable offline: the customer's lines and the agent
model's responses are fixed, so the run is deterministic (the CI suite in `evals/sims/ci/`).
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from baton.aop.models import AOPBundle
from baton.sim.fixtures import FIXTURE_SPECS, FixtureError, resolve
from baton.tools.registry import ToolRegistry

EndStatus = Literal["open", "escalated", "resolved"]
CONTROL_TOOLS = frozenset({"advance_step", "switch_procedure"})
TAGS = frozenset(
    {
        "happy",
        "edge",
        "adversarial",
        "kb",
        "unanswerable",
        "multi_intent",
        "non_english",
        "messy",
        "off_topic",
        "pressure",
    }
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Persona(_Strict):
    name: str = Field(min_length=1)
    fixture: str | None = None
    traits: list[str] = Field(default_factory=list)
    goal: str = Field(min_length=1)
    knows: dict[str, str] = Field(default_factory=dict)
    pressure: str | None = None
    language: str | None = Field(default=None, description="e.g. Spanish; default English")
    opening: str | None = Field(default=None, description="a fixed first message, if any")


class Expect(_Strict):
    must: list[str] = Field(default_factory=list)
    must_not: list[str] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    tools_not_called: list[str] = Field(default_factory=list)
    end_status: list[EndStatus] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (
            self.must
            or self.must_not
            or self.tools_called
            or self.tools_not_called
            or self.end_status
        )


class ScriptCall(_Strict):
    tool: str
    args: dict[str, object] = Field(default_factory=dict)


class ScriptStep(_Strict):
    """One agent-model response: text, tool calls, or both."""

    say: str | None = None
    calls: list[ScriptCall] = Field(default_factory=list)


class ScriptTurn(_Strict):
    customer: str
    route: str | None = Field(default=None, description="the router's answer from this turn on")
    agent: list[ScriptStep] = Field(default_factory=list)


class Script(_Strict):
    turns: list[ScriptTurn] = Field(min_length=1)
    # What the scripted judge reports, by criterion text: did the behaviour occur? Defaults:
    # every `must` occurred and no `must_not` did.
    judge: dict[str, bool] = Field(default_factory=dict)


class Scenario(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$")
    aop: str
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    persona: Persona
    max_turns: int = Field(default=12, ge=1, le=40)
    expect: Expect
    runs: int = Field(default=3, ge=1, le=20)
    script: Script | None = None
    source: str = Field(default="", exclude=True, description="file it came from")

    @field_validator("tags")
    @classmethod
    def _known_tags(cls, tags: list[str]) -> list[str]:
        unknown = sorted(set(tags) - TAGS)
        if unknown:
            raise ValueError(f"unknown tags {unknown} (known: {sorted(TAGS)})")
        return tags

    def resolved(self) -> "Scenario":
        """A copy with every `<seeded>` placeholder replaced. Raises `FixtureError`."""
        fixture = self.persona.fixture
        persona = self.persona.model_copy(
            update={
                "goal": resolve(self.persona.goal, fixture),
                "pressure": resolve(self.persona.pressure, fixture)
                if self.persona.pressure
                else None,
                "opening": resolve(self.persona.opening, fixture) if self.persona.opening else None,
                "knows": {k: resolve(v, fixture, k) for k, v in self.persona.knows.items()},
            }
        )
        expect = self.expect.model_copy(
            update={
                "must": [resolve(c, fixture) for c in self.expect.must],
                "must_not": [resolve(c, fixture) for c in self.expect.must_not],
            }
        )
        script = None
        if self.script is not None:
            # script.judge keys name criteria, so they resolve the same way.
            script = Script.model_validate(_resolve_tree(self.script.model_dump(), fixture))
        return self.model_copy(update={"persona": persona, "expect": expect, "script": script})

    def matches(self, needle: str) -> bool:
        """`--filter`: a tag, the AOP id, or part of the scenario id."""
        return needle in self.tags or needle == self.aop or needle in self.id


def _resolve_tree(value: object, fixture: str | None) -> object:
    if isinstance(value, str):
        return resolve(value, fixture)
    if isinstance(value, list):
        return [_resolve_tree(v, fixture) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_tree(v, fixture) for k, v in value.items()}
    return value


class SuiteError(ValueError):
    """The suite can't be loaded or doesn't validate. `problems` lists every issue found."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


def _suite_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise SuiteError([f"{path}: no such file or directory"])
    return sorted(path.glob("*.yaml"))


def load_suite(path: Path) -> list[Scenario]:
    """Load every scenario under `path` (a file or a directory). Collects all errors."""
    scenarios: list[Scenario] = []
    problems: list[str] = []
    for file in _suite_files(path):
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{file.name}: not valid YAML: {exc}")
            continue
        if isinstance(data, dict) and "scenarios" in data:
            data = data["scenarios"]
        items = data if isinstance(data, list) else [data]
        for i, item in enumerate(items):
            where = f"{file.name}[{i}]" if isinstance(data, list) else file.name
            if not isinstance(item, dict):
                problems.append(f"{where}: a scenario must be a mapping")
                continue
            try:
                scenarios.append(Scenario.model_validate({**item, "source": file.name}))
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(p) for p in err["loc"])
                    problems.append(f"{where} ({item.get('id', '?')}): {loc}: {err['msg']}")
    if problems:
        raise SuiteError(problems)
    return scenarios


def validate_suite(
    scenarios: list[Scenario],
    bundle: AOPBundle,
    registry: ToolRegistry,
    *,
    require_script: bool = False,
) -> list[str]:
    """Everything wrong with the suite beyond its shape: unknown AOPs, tools and fixtures,
    unresolvable placeholders, duplicate ids, scenarios that check nothing."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    tools = set(registry.names()) | CONTROL_TOOLS
    for sc in scenarios:
        where = f"{sc.source} ({sc.id})"
        if sc.id in seen:
            problems.append(f"{where}: duplicate id (also in {seen[sc.id]})")
        seen[sc.id] = sc.source
        if sc.aop not in bundle.aops:
            problems.append(f"{where}: unknown aop {sc.aop!r}")
        if sc.persona.fixture and sc.persona.fixture not in FIXTURE_SPECS:
            problems.append(f"{where}: unknown fixture {sc.persona.fixture!r}")
        for name in sc.expect.tools_called + sc.expect.tools_not_called:
            if name not in tools:
                problems.append(f"{where}: unknown tool {name!r} in expect")
        if sc.expect.empty:
            problems.append(f"{where}: expect checks nothing")
        if require_script and sc.script is None:
            problems.append(f"{where}: no script (needed to run offline)")
        if sc.script is not None:
            for turn in sc.script.turns:
                if turn.route and turn.route != "none" and turn.route not in bundle.aops:
                    problems.append(f"{where}: script routes to unknown aop {turn.route!r}")
                for step in turn.agent:
                    for c in step.calls:
                        if c.tool not in tools:
                            problems.append(f"{where}: script calls unknown tool {c.tool!r}")
            criteria = set(sc.expect.must) | set(sc.expect.must_not)
            for criterion in sc.script.judge:
                if criterion not in criteria:
                    problems.append(f"{where}: script.judge names unknown {criterion!r}")
        try:
            sc.resolved()
        except FixtureError as exc:
            problems.append(f"{where}: {exc}")
    return problems


class CoverageGroup(_Strict):
    tags: list[str]
    min: int


class CoverageRule(_Strict):
    total: int = 0
    every_aop: bool = False
    groups: dict[str, CoverageGroup] = Field(default_factory=dict)


def coverage(
    scenarios: list[Scenario], rule: CoverageRule, aops: list[str]
) -> tuple[dict[str, int], list[str]]:
    """Counts per coverage group, and the rules the suite misses."""
    counts = {"total": len(scenarios)}
    problems: list[str] = []
    if len(scenarios) < rule.total:
        problems.append(f"{len(scenarios)} scenarios, need at least {rule.total}")
    for name, group in rule.groups.items():
        n = sum(1 for sc in scenarios if set(sc.tags) & set(group.tags))
        counts[name] = n
        if n < group.min:
            problems.append(f"{name}: {n} scenarios tagged {group.tags}, need at least {group.min}")
    if rule.every_aop:
        covered = {sc.aop for sc in scenarios}
        missing = sorted(set(aops) - covered)
        if missing:
            problems.append(f"no scenario for AOPs: {', '.join(missing)}")
    return counts, problems
