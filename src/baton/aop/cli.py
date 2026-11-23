"""`baton-aop`: validate, inspect and version AOPs from the terminal.

    uv run baton-aop validate            # all AOPs; exit 1 on any error
    uv run baton-aop graph return_item   # Mermaid flowchart
    uv run baton-aop show return_item    # steps, transitions, tools (--prompts for step prompts)
    uv run baton-aop compile             # validate, then record new versions in the database

`--dir` points at another AOP directory (default: settings.aops_dir).
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy.exc import OperationalError, ProgrammingError

from baton.aop.compiler import (
    STEP_PROMPT,
    AOPCompileError,
    check_directory,
    compile_aop,
    compile_aops,
)
from baton.aop.mermaid import to_mermaid
from baton.aop.models import AOP, Diagnostic, target_label
from baton.aop.store import sync_versions
from baton.config import get_settings
from baton.db.session import session_scope
from baton.prompts import load_prompt
from baton.tools.registry import default_registry


def _print_diagnostics(diagnostics: list[Diagnostic]) -> None:
    for d in sorted(diagnostics, key=lambda d: (d.aop_id, d.step or 0, d.code.value)):
        print(d.format())


def _summary(n_aops: int, diagnostics: list[Diagnostic]) -> str:
    errors = sum(d.level == "error" for d in diagnostics)
    warnings = len(diagnostics) - errors
    return f"{n_aops} AOP(s) checked: {errors} error(s), {warnings} warning(s)"


def cmd_validate(directory: Path) -> int:
    aops, diagnostics = check_directory(directory)
    _print_diagnostics(diagnostics)
    print(_summary(len(aops), diagnostics))
    if not aops and not diagnostics:
        print(f"no AOP files found in {directory}", file=sys.stderr)
        return 1
    return 1 if any(d.level == "error" for d in diagnostics) else 0


def _find(directory: Path, aop_id: str) -> tuple[AOP, list[Diagnostic]] | None:
    aops, diagnostics = check_directory(directory)
    for aop in aops:
        if aop.id == aop_id:
            return aop, [d for d in diagnostics if d.aop_id == aop_id]
    known = ", ".join(sorted(a.id for a in aops)) or "(none)"
    print(f"unknown AOP {aop_id!r}. Known: {known}", file=sys.stderr)
    return None


def cmd_graph(directory: Path, aop_id: str) -> int:
    found = _find(directory, aop_id)
    if found is None:
        return 1
    print(to_mermaid(found[0]), end="")
    return 0


def cmd_show(directory: Path, aop_id: str, prompts: bool) -> int:
    found = _find(directory, aop_id)
    if found is None:
        return 1
    aop, diagnostics = found
    print(f"{aop.ref}  {aop.name}")
    print(f"source: {aop.source_path}  hash: {aop.source_hash[:12]}")
    print(f"requires_verification: {str(aop.requires_verification).lower()}")
    print("triggers:")
    for trigger in aop.triggers:
        print(f"  - {trigger}")
    print("policies:")
    for policy in aop.policies:
        print(f"  - {policy}")
    print("steps:")
    for step in aop.steps:
        print(f"  {step.number}. {step.text}")
        if step.tool_refs:
            print(f"     tools: {', '.join(step.tool_refs)}")
        for t in step.transitions:
            when = f"if {t.condition}" if t.condition else ("next" if t.implicit else "always")
            print(f"     -> {target_label(t.target)}  ({when})")
    if diagnostics:
        print("diagnostics:")
        _print_diagnostics(diagnostics)
    if prompts:
        if any(d.level == "error" for d in diagnostics):
            print("(step prompts not shown: fix the errors above first)")
            return 1
        compiled = compile_aop(aop, default_registry(), load_prompt(STEP_PROMPT))
        for compiled_step in compiled.steps:
            header = f"----- step {compiled_step.number} prompt -----"
            print(f"\n{header}\n{compiled_step.prompt}", end="")
    return 0


def cmd_compile(directory: Path) -> int:
    try:
        bundle = compile_aops(directory)
    except AOPCompileError as exc:
        _print_diagnostics(exc.diagnostics)
        print(f"compile failed: {exc}" if not exc.diagnostics else "compile failed")
        return 1
    _print_diagnostics(bundle.diagnostics)
    try:
        with session_scope() as session:
            result = sync_versions(session, bundle)
    except (OperationalError, ProgrammingError) as exc:
        print(
            f"database not ready ({exc.orig}). Run `uv run alembic upgrade head` first.",
            file=sys.stderr,
        )
        return 1
    for warning in result.warnings:
        print(f"warning {warning}")
    print(
        f"compiled {len(bundle.aops)} AOP(s), bundle {bundle.bundle_hash[:12]}: "
        f"{len(result.created)} new version(s)"
        + (f" ({', '.join(result.created)})" if result.created else "")
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baton-aop", description="Validate and inspect AOPs.")
    parser.add_argument("--dir", type=Path, default=None, help="AOP directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="check every AOP; exit 1 on any error")
    graph = sub.add_parser("graph", help="print an AOP as a Mermaid flowchart")
    graph.add_argument("aop_id")
    show = sub.add_parser("show", help="print an AOP's steps, transitions and tools")
    show.add_argument("aop_id")
    show.add_argument("--prompts", action="store_true", help="also print each step's prompt")
    sub.add_parser("compile", help="validate and record new AOP versions in the database")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; step text and Mermaid labels can contain "…" or "→".
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    directory: Path = args.dir or get_settings().aops_dir
    if not directory.is_dir():
        print(f"AOP directory {directory} does not exist", file=sys.stderr)
        return 2
    if args.command == "validate":
        return cmd_validate(directory)
    if args.command == "graph":
        return cmd_graph(directory, args.aop_id)
    if args.command == "show":
        return cmd_show(directory, args.aop_id, args.prompts)
    return cmd_compile(directory)


if __name__ == "__main__":
    raise SystemExit(main())
