"""Parse an AOP Markdown file into an `AOP`. Deterministic and rule-based, no LLM.

File shape: YAML frontmatter between `---` lines, then numbered steps (`1. ...`). A step can
wrap onto following lines. Inside a step the parser recognises:

- tool references: `@tool_name`
- jumps: "go to step 3", "skip to 3", "go back to step 1", "repeat step 2", "-> step 4", ...
- terminals: the upper-case words `ESCALATE` and `RESOLVE`. Case matters, so the prose
  "we'll resolve it" is not a terminal; authors write the keyword when they mean the outcome.
- conditions: a clause that starts with "If"/"When"/"Once"/"In case"/"Unless", or an
  "if ..."/"unless ..." right after the target ("ESCALATE if they insist"). An "if" before the
  target is read as "whether" ("Ask if they want a refund, then go to step 4" is unconditional).
  "Otherwise"/"else" at the start of a clause marks the default edge.

Phrasings the parser won't guess about are parse errors with the step and line: a negated jump
("Never RESOLVE ...", "Don't go to step 3") and "in which case".

A step without an unconditional exit falls through to the next step (an `implicit` transition).
The last step gets no fall-through; the validator reports it if it can end without a terminal.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from baton.aop.models import AOP, Step, Target, Terminal, Transition


class AOPParseError(ValueError):
    def __init__(self, message: str, *, line: int | None = None, path: str | None = None) -> None:
        self.message = message
        self.line = line
        self.path = path
        where = f"{path or '<source>'}" + (f":{line}" if line is not None else "")
        super().__init__(f"{where}: {message}")


class _Frontmatter(BaseModel):
    """The YAML header. Unknown keys are rejected so a typo can't silently use a default."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: str = Field(min_length=1)
    version: int = Field(ge=1, strict=True)
    triggers: list[str] | None = None
    requires_verification: bool = False
    policies: list[str] | None = None

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value == "none":  # the router's "no procedure" answer
            raise ValueError("'none' is reserved")
        return value

    @field_validator("triggers", "policies")
    @classmethod
    def _drop_blank(cls, items: list[str] | None) -> list[str]:
        return [i.strip() for i in items or [] if i.strip()]


# ---------------------------------------------------------------------- regexes

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_STEP_START = re.compile(r"^(\d+)[.)]\s+(\S.*)$")
_TOOL_REF = re.compile(r"(?<![\w.@])@([A-Za-z_]\w*)")

_STEP_NUM = r"(?:step\s+)?#?(\d+)\b"
_GOTO = re.compile(
    r"\b(?:"
    # go to 3 / skip to 3 / jump to step 3 / go back to step 1 ("step" optional)
    r"(?:go|skip|jump)(?:\s+(?:back|on|ahead|straight))?\s+to\s+" + _STEP_NUM + r"|"
    # proceed to / continue with / return to / move on to ... ("step" required, so
    # "continue with 2 of them" is not a jump)
    r"(?:proceed|continue|return|move|loop|head|come)"
    r"(?:\s+(?:back|on|ahead|straight))?\s+(?:to|with|at)\s+step\s+#?(\d+)\b|"
    # repeat step 2 / restart from step 1 / start over at step 1 ("step" required here)
    r"(?:repeat|redo|restart\s+(?:from|at)|start\s+over\s+(?:from|at))\s+step\s+#?(\d+)\b"
    r")"
    # -> step 4 / → step 4
    r"|(?:->|→)\s*" + _STEP_NUM,
    re.IGNORECASE,
)
_TERMINAL = re.compile(r"\b(ESCALATE|RESOLVE)\b")  # deliberately case-sensitive

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+")
# "otherwise"/"else" starts a new clause only after a comma ("..., otherwise RESOLVE"), so
# "something else, go to step 2" keeps its condition.
_OTHERWISE_SPLIT = re.compile(r",\s+(?=(?:otherwise|else)\b)", re.IGNORECASE)
_LEADING_CONDITION = re.compile(r"^(if|when|once|in case|unless)\b\s*", re.IGNORECASE)
_LEADING_DEFAULT = re.compile(r"^(otherwise|else)\b", re.IGNORECASE)
# Only an "if"/"unless" *after* the target guards it ("ESCALATE if they insist"); one before
# it is usually "whether" ("Ask if they want a refund, then go to step 4").
_INNER_CONDITION = re.compile(r"\b(if|unless)\s+([^,.;]+)", re.IGNORECASE)
_CONDITION_END = re.compile(r",|\bthen\b", re.IGNORECASE)
_NEGATED = re.compile(
    r"\b(?:not|never|don't|do not|cannot|can't|won't)\s+(?:\w+\s+)?$", re.IGNORECASE
)
_AMBIGUOUS = re.compile(r"\bin which case\b", re.IGNORECASE)


class _StepTextError(ValueError):
    """A step phrasing the parser refuses to guess about; becomes an AOPParseError."""


@dataclass(frozen=True)
class _Found:
    start: int
    target: Target


def _targets_in(clause: str) -> list[_Found]:
    found: list[_Found] = []
    for m in _GOTO.finditer(clause):
        number = next(g for g in m.groups() if g is not None)
        found.append(_Found(m.start(), int(number)))
    for m in _TERMINAL.finditer(clause):
        found.append(_Found(m.start(), Terminal(m.group(1))))
    for f in found:
        if _NEGATED.search(clause[: f.start]):
            raise _StepTextError(
                f"negated jump {clause.strip()!r}: write when the jump DOES happen instead "
                "(e.g. 'If they insist, ESCALATE.')"
            )
    if found and _AMBIGUOUS.search(clause):
        raise _StepTextError(
            f"'in which case' is ambiguous in {clause.strip()!r}: split it into "
            "'If ..., go to step N.' and 'Otherwise ...'"
        )
    return sorted(found, key=lambda f: f.start)


def _clean(text: str) -> str:
    return text.strip().strip(" ,.;:").strip()


def _condition_for(clause: str, target_start: int, next_target: int | None) -> str | None:
    """The condition guarding a target in this clause, or None if the edge is the default.

    `next_target` is where the following target in the clause starts, so a trailing
    "if ..." only guards the target right before it.
    """
    stripped = clause.lstrip()
    offset = len(clause) - len(stripped)
    if _LEADING_DEFAULT.match(stripped):
        return None
    lead = _LEADING_CONDITION.match(stripped)
    if lead:
        keyword = lead.group(1).lower()
        rest_start = lead.end()
        end = target_start - offset
        stop = _CONDITION_END.search(stripped, rest_start)
        if stop and stop.start() < end:
            end = stop.start()
        condition = _clean(stripped[rest_start:end])
        if condition:
            return f"unless {condition}" if keyword == "unless" else condition
    inner = _INNER_CONDITION.search(clause, target_start, next_target or len(clause))
    if inner:
        condition = _clean(inner.group(2))
        if condition:
            return f"unless {condition}" if inner.group(1).lower() == "unless" else condition
    return None


def _clauses(text: str) -> list[str]:
    clauses: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        clauses.extend(c for c in _OTHERWISE_SPLIT.split(sentence) if c.strip())
    return clauses


def parse_transitions(text: str) -> list[Transition]:
    """Explicit transitions in a step's text, in order, without duplicates."""
    transitions: list[Transition] = []
    for clause in _clauses(text):
        found = _targets_in(clause)
        for i, f in enumerate(found):
            following = found[i + 1].start if i + 1 < len(found) else None
            t = Transition(condition=_condition_for(clause, f.start, following), target=f.target)
            if t not in transitions:
                transitions.append(t)
    return transitions


def parse_tool_refs(text: str) -> list[str]:
    return list(dict.fromkeys(_TOOL_REF.findall(text)))


def _normalise(source: str) -> str:
    return source.replace("\r\n", "\n").lstrip("\ufeff")


def source_hash(source: str) -> str:
    """sha256 of the source with CRLF and a leading BOM normalised, so editors and OSes agree."""
    return hashlib.sha256(_normalise(source).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- file parsing


def _parse_frontmatter(source: str, path: str | None) -> tuple[_Frontmatter, str, int]:
    match = _FRONTMATTER.match(source)
    if match is None:
        raise AOPParseError(
            "missing YAML frontmatter: the file must start with a '---' line and close it "
            "with another '---' line",
            line=1,
            path=path,
        )
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise AOPParseError(f"invalid YAML in frontmatter: {exc}", line=1, path=path) from exc
    if not isinstance(data, dict):
        raise AOPParseError("frontmatter must be a YAML mapping", line=1, path=path)
    try:
        front = _Frontmatter.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'frontmatter'}: {err['msg']}"
            for err in exc.errors()
        )
        raise AOPParseError(f"invalid frontmatter: {problems}", line=1, path=path) from exc
    body_first_line = match.group(0).count("\n") + 1
    return front, source[match.end() :], body_first_line


def _parse_steps(body: str, first_line: int, path: str | None) -> list[tuple[int, str, int]]:
    """(number, text, line) for each step; a step may wrap onto several lines."""
    steps: list[tuple[int, list[str], int]] = []
    for offset, raw in enumerate(body.split("\n")):
        line = raw.strip()
        lineno = first_line + offset
        if not line:
            continue
        start = _STEP_START.match(line)
        if start:
            number = int(start.group(1))
            expected = len(steps) + 1
            if number != expected:
                raise AOPParseError(
                    f"steps must be numbered 1, 2, 3, ... in order; expected step {expected} "
                    f"but found {number}",
                    line=lineno,
                    path=path,
                )
            steps.append((number, [start.group(2)], lineno))
        elif steps:
            steps[-1][1].append(line)  # a wrapped continuation of the current step
        elif not line.startswith("#"):  # headings before step 1 are allowed and ignored
            raise AOPParseError(
                f"expected a numbered step like '1. ...', found {line[:40]!r}",
                line=lineno,
                path=path,
            )
    if not steps:
        raise AOPParseError("no steps: add numbered steps after the frontmatter", path=path)
    return [(number, " ".join(parts), line) for number, parts, line in steps]


def parse_aop(source: str, path: str | Path | None = None) -> AOP:
    """Parse AOP Markdown. Raises `AOPParseError` with a line number when the shape is wrong."""
    path_str = str(path) if path is not None else None
    normalised = _normalise(source)
    front, body, body_line = _parse_frontmatter(normalised, path_str)
    raw_steps = _parse_steps(body, body_line, path_str)

    steps: list[Step] = []
    for number, text, line in raw_steps:
        try:
            transitions = parse_transitions(text)
        except _StepTextError as exc:
            raise AOPParseError(f"step {number}: {exc}", line=line, path=path_str) from exc
        has_default = any(t.condition is None for t in transitions)
        if not has_default and number < len(raw_steps):
            transitions.append(Transition(condition=None, target=number + 1, implicit=True))
        steps.append(
            Step(number=number, text=text, tool_refs=parse_tool_refs(text), transitions=transitions)
        )

    return AOP(
        id=front.id,
        name=front.name,
        version=front.version,
        triggers=front.triggers or [],
        requires_verification=front.requires_verification,
        policies=front.policies or [],
        steps=steps,
        source_hash=source_hash(source),
        source_path=path_str,
    )


def parse_aop_file(path: Path) -> AOP:
    return parse_aop(path.read_text(encoding="utf-8"), path)
