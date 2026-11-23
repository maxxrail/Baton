"""Data models for AOPs (Agent Operating Procedures).

An `AOP` is what the parser reads out of one Markdown file. Each `Step` has its plain-English
text, the `@tools` it mentions, and its outgoing `Transition`s. A transition target is either
another step number or one of the two terminals, `ESCALATE` (hand off to a human) and `RESOLVE`
(done).

The compiler turns a validated `AOP` into a `CompiledAOP`, which adds the per-step prompt the
runtime injects (Part 02). An `AOPBundle` is every compiled AOP in a directory plus a hash of
the whole set.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Terminal(StrEnum):
    ESCALATE = "ESCALATE"
    RESOLVE = "RESOLVE"


Target = int | Terminal


def target_label(target: Target) -> str:
    return target.value if isinstance(target, Terminal) else f"step {target}"


class Transition(BaseModel):
    """An edge out of a step. `condition` is None for the unconditional/default edge."""

    model_config = ConfigDict(frozen=True)

    condition: str | None
    target: Target
    implicit: bool = Field(
        default=False, description="True for the automatic fall-through to the next step"
    )


class Step(BaseModel):
    number: int
    text: str
    tool_refs: list[str] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)


class AOP(BaseModel):
    id: str
    name: str
    version: int
    triggers: list[str]
    requires_verification: bool
    policies: list[str]
    steps: list[Step]
    source_hash: str
    source_path: str | None = None

    @property
    def ref(self) -> str:
        """`aop_id@version`, the form traces record."""
        return f"{self.id}@{self.version}"

    def step(self, number: int) -> Step:
        for step in self.steps:
            if step.number == number:
                return step
        raise KeyError(f"{self.id} has no step {number}")

    def tool_refs(self) -> list[str]:
        """Every tool the AOP mentions, in first-use order, without duplicates."""
        return list(dict.fromkeys(t for step in self.steps for t in step.tool_refs))


DiagnosticLevel = Literal["error", "warning"]


class DiagnosticCode(StrEnum):
    PARSE_ERROR = "PARSE_ERROR"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    BAD_TARGET = "BAD_TARGET"
    UNREACHABLE_STEP = "UNREACHABLE_STEP"
    NO_TERMINAL = "NO_TERMINAL"
    CYCLE_WITHOUT_EXIT = "CYCLE_WITHOUT_EXIT"
    SIDE_EFFECT_WITHOUT_VERIFICATION = "SIDE_EFFECT_WITHOUT_VERIFICATION"
    DUPLICATE_ID = "DUPLICATE_ID"
    MISSING_TRIGGERS = "MISSING_TRIGGERS"
    LONG_STEP = "LONG_STEP"


class Diagnostic(BaseModel):
    level: DiagnosticLevel
    aop_id: str
    step: int | None
    code: DiagnosticCode
    message: str
    source_path: str | None = None

    def format(self) -> str:
        where = self.aop_id if self.step is None else f"{self.aop_id}:{self.step}"
        return f"{self.level:<7} {where:<24} {self.code.value:<34} {self.message}"


class CompiledStep(BaseModel):
    number: int
    text: str
    allowed_tools: list[str]
    transitions: list[Transition]
    prompt: str = Field(description="What the runtime injects while the agent is on this step")


class CompiledAOP(BaseModel):
    id: str
    name: str
    version: int
    source_hash: str
    triggers: list[str]
    requires_verification: bool
    policies: list[str]
    steps: list[CompiledStep]
    step_prompt_version: str

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def step(self, number: int) -> CompiledStep:
        for step in self.steps:
            if step.number == number:
                return step
        raise KeyError(f"{self.id} has no step {number}")


class AOPBundle(BaseModel):
    """Every compiled AOP from one directory. `bundle_hash` changes if any AOP's source does."""

    aops: dict[str, CompiledAOP]
    diagnostics: list[Diagnostic] = Field(description="Warnings only; errors stop compilation")
    bundle_hash: str

    def get(self, aop_id: str) -> CompiledAOP:
        try:
            return self.aops[aop_id]
        except KeyError:
            raise KeyError(f"unknown AOP {aop_id!r}") from None
