"""Compile a directory of AOP Markdown files into a validated `AOPBundle`.

`check_directory` parses and validates without raising (the CLI and API use it to show every
problem at once). `compile_aops` does the same and raises `AOPCompileError` if anything is an
error, so the runtime can never load a broken procedure.

Each compiled step carries the prompt the runtime injects while the agent is on that step: the
step text, the AOP's policies, the tools this step allows and where it can go next. The template
is `prompts/aop_step.md`; its version is stored on every `CompiledAOP`.
"""

import hashlib
from collections.abc import Sequence
from pathlib import Path

from baton.aop.models import (
    AOP,
    AOPBundle,
    CompiledAOP,
    CompiledStep,
    Diagnostic,
    DiagnosticCode,
    Step,
    Terminal,
    Transition,
    target_label,
)
from baton.aop.parser import AOPParseError, parse_aop, parse_aop_file
from baton.aop.validator import has_errors, validate_aop, validate_aops
from baton.config import get_settings
from baton.prompts import Prompt, load_prompt
from baton.tools.registry import ToolRegistry, default_registry

STEP_PROMPT = "aop_step"


class AOPCompileError(Exception):
    def __init__(self, diagnostics: list[Diagnostic], message: str | None = None) -> None:
        self.diagnostics = diagnostics
        if message is None:
            errors = [d for d in diagnostics if d.level == "error"]
            lines = "\n".join(d.format() for d in errors)
            message = f"{len(errors)} AOP error(s):\n{lines}"
        super().__init__(message)


def aop_files(directory: Path) -> list[Path]:
    """Every `*.md` in the directory except README.md, in a stable order."""
    return sorted(p for p in directory.glob("*.md") if p.name.lower() != "readme.md")


def _parse_error(exc: AOPParseError, fallback_id: str) -> Diagnostic:
    return Diagnostic(
        level="error",
        aop_id=fallback_id,
        step=None,
        code=DiagnosticCode.PARSE_ERROR,
        message=exc.message if exc.line is None else f"line {exc.line}: {exc.message}",
        source_path=exc.path,
    )


def load_directory(directory: Path) -> tuple[list[AOP], list[Diagnostic]]:
    """Parse every AOP file. A file that fails to parse becomes a PARSE_ERROR diagnostic."""
    aops: list[AOP] = []
    diagnostics: list[Diagnostic] = []
    for path in aop_files(directory):
        try:
            aops.append(parse_aop_file(path))
        except AOPParseError as exc:
            diagnostics.append(_parse_error(exc, path.stem))
    return aops, diagnostics


def check_directory(
    directory: Path, registry: ToolRegistry | None = None
) -> tuple[list[AOP], list[Diagnostic]]:
    """Parse and validate a directory. Never raises for bad AOPs; returns diagnostics."""
    aops, diagnostics = load_directory(directory)
    diagnostics.extend(validate_aops(aops, registry or default_registry()))
    return aops, diagnostics


def check_source(
    markdown: str, registry: ToolRegistry | None = None
) -> tuple[AOP | None, list[Diagnostic]]:
    """Validate one draft AOP (the console's "validate a draft" box)."""
    try:
        aop = parse_aop(markdown, "<draft>")
    except AOPParseError as exc:
        return None, [_parse_error(exc, "<draft>")]
    return aop, validate_aop(aop, registry or default_registry())


# ---------------------------------------------------------------------- step prompts


def _bullets(items: Sequence[str], empty: str) -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


def _transition_line(t: Transition, has_conditions: bool) -> str:
    where = target_label(t.target)
    if t.condition is not None:
        return f"If {t.condition}: {where}"
    if t.implicit:
        return f"When this step is done: {where}"
    return f"Otherwise: {where}" if has_conditions else where


def render_step_prompt(aop: AOP, step: Step, registry: ToolRegistry, template: Prompt) -> str:
    tools = []
    for name in step.tool_refs:
        tool = registry.get(name)
        flags = ", ".join(
            flag
            for flag, on in (
                ("changes data", tool.side_effect),
                ("needs verification", tool.requires_verification),
            )
            if on
        )
        tools.append(f"{name}: {tool.description}" + (f" ({flags})" if flags else ""))

    has_conditions = any(t.condition is not None for t in step.transitions)
    transitions = [_transition_line(t, has_conditions) for t in step.transitions]
    terminals = {t.target for t in step.transitions if isinstance(t.target, Terminal)}
    if Terminal.ESCALATE in terminals:
        transitions.append("ESCALATE hands the conversation to a human teammate.")
    if Terminal.RESOLVE in terminals:
        transitions.append("RESOLVE ends the procedure; the customer's request is handled.")

    verification = (
        "This procedure needs a verified customer. Tools marked 'needs verification' stay "
        "blocked until verify_customer succeeds."
        if aop.requires_verification
        else "This procedure does not need identity verification."
    )
    return template.render(
        aop_name=aop.name,
        aop_ref=aop.ref,
        step_number=str(step.number),
        step_count=str(len(aop.steps)),
        step_text=step.text,
        policies=_bullets(aop.policies, "- (none)"),
        tools=_bullets(tools, "- (none: answer without tools)"),
        transitions=_bullets(transitions, "- (none)"),
        verification=verification,
    )


def compile_aop(aop: AOP, registry: ToolRegistry, template: Prompt) -> CompiledAOP:
    """Turn a *validated* AOP into its runtime form."""
    return CompiledAOP(
        id=aop.id,
        name=aop.name,
        version=aop.version,
        source_hash=aop.source_hash,
        triggers=aop.triggers,
        requires_verification=aop.requires_verification,
        policies=aop.policies,
        steps=[
            CompiledStep(
                number=step.number,
                text=step.text,
                allowed_tools=step.tool_refs,
                transitions=step.transitions,
                prompt=render_step_prompt(aop, step, registry, template),
            )
            for step in aop.steps
        ],
        step_prompt_version=template.version,
    )


def bundle_hash(aops: Sequence[CompiledAOP]) -> str:
    """One hash for the whole set: changes if any AOP or the step prompt template changes."""
    lines = sorted(f"{a.id}:{a.source_hash}:{a.step_prompt_version}" for a in aops)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def compile_aops(
    directory: Path | None = None,
    registry: ToolRegistry | None = None,
    prompts_dir: Path | None = None,
) -> AOPBundle:
    """Compile every AOP in `directory` (default: settings.aops_dir). Raises on any error."""
    registry = registry or default_registry()
    directory = directory or get_settings().aops_dir
    if not directory.is_dir():
        raise AOPCompileError([], f"AOP directory {directory} does not exist")
    aops, diagnostics = check_directory(directory, registry)
    if not aops and not diagnostics:
        raise AOPCompileError([], f"no AOP files (*.md) found in {directory}")
    if has_errors(diagnostics):
        raise AOPCompileError(diagnostics)
    template = load_prompt(STEP_PROMPT, prompts_dir)
    compiled = [compile_aop(aop, registry, template) for aop in aops]
    return AOPBundle(
        aops={a.id: a for a in compiled},
        diagnostics=diagnostics,
        bundle_hash=bundle_hash(compiled),
    )
