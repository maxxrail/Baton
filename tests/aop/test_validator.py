"""Validator tests. Each file in fixtures/bad/ is named after the one code it must raise."""

from pathlib import Path

import pytest

from baton.aop.compiler import check_directory, load_directory
from baton.aop.models import DiagnosticCode
from baton.aop.parser import parse_aop, parse_aop_file
from baton.aop.validator import has_errors, validate_aop, validate_aops
from baton.tools.registry import default_registry

BAD = Path(__file__).parent / "fixtures" / "bad"
REPO_AOPS = Path(__file__).resolve().parents[2] / "aops"

# Every code has a fixture whose file name is the code in lower case.
FIXTURES = {code: BAD / f"{code.value.lower()}.md" for code in DiagnosticCode}


def test_every_code_has_a_fixture_and_no_fixture_is_orphaned() -> None:
    assert {p.name for p in BAD.glob("*.md")} == {p.name for p in FIXTURES.values()}


def _codes_for(code: DiagnosticCode) -> list[DiagnosticCode]:
    path = FIXTURES[code]
    if code is DiagnosticCode.PARSE_ERROR:
        # Parse errors surface when loading a directory; keep only this file's diagnostics.
        _, diagnostics = check_directory(BAD)
        return [d.code for d in diagnostics if d.source_path == str(path)]
    if code is DiagnosticCode.DUPLICATE_ID:
        # Duplicates need company: validate the fixture alongside the real Northwind AOPs.
        northwind, _ = load_directory(REPO_AOPS)
        return [
            d.code for d in validate_aops([*northwind, parse_aop_file(path)], default_registry())
        ]
    return [d.code for d in validate_aop(parse_aop_file(path), default_registry())]


@pytest.mark.parametrize("code", list(DiagnosticCode), ids=lambda c: c.value)
def test_bad_fixture_raises_exactly_its_code(code: DiagnosticCode) -> None:
    assert _codes_for(code) == [code]


def test_parse_errors_become_diagnostics_with_line_numbers() -> None:
    _, diagnostics = check_directory(BAD)
    parse = [d for d in diagnostics if d.code is DiagnosticCode.PARSE_ERROR]
    assert len(parse) == 1
    assert parse[0].aop_id == "parse_error"  # falls back to the file name
    assert "requires_verfication" in parse[0].message
    assert parse[0].message.startswith("line 1:")


def test_long_step_is_only_a_warning() -> None:
    diagnostics = validate_aop(
        parse_aop_file(FIXTURES[DiagnosticCode.LONG_STEP]), default_registry()
    )
    assert [d.level for d in diagnostics] == ["warning"]
    assert not has_errors(diagnostics)


def test_unknown_tool_lists_known_tools() -> None:
    aop = parse_aop_file(FIXTURES[DiagnosticCode.UNKNOWN_TOOL])
    [diag] = validate_aop(aop, default_registry())
    assert "@get_weather" in diag.message
    assert "get_order" in diag.message
    assert diag.step == 1


def test_side_effect_check_names_the_tool_and_step() -> None:
    aop = parse_aop_file(FIXTURES[DiagnosticCode.SIDE_EFFECT_WITHOUT_VERIFICATION])
    [diag] = validate_aop(aop, default_registry())
    assert diag.step == 2
    assert "@cancel_order" in diag.message


def test_read_only_tools_are_fine_without_verification() -> None:
    src = (
        "---\nid: lookup\nname: Lookup\nversion: 1\ntriggers: [x]\n---\n"
        "1. Find them with @lookup_customer and RESOLVE.\n"
    )
    assert validate_aop(parse_aop(src), default_registry()) == []


def test_cycle_report_names_every_step_in_the_loop() -> None:
    aop = parse_aop_file(FIXTURES[DiagnosticCode.CYCLE_WITHOUT_EXIT])
    [diag] = validate_aop(aop, default_registry())
    assert diag.step == 2
    assert "steps 2, 3" in diag.message


def test_self_loop_without_exit_is_a_cycle() -> None:
    src = "---\nid: loop\nname: L\nversion: 1\ntriggers: [x]\n---\n1. Ask again. Repeat step 1.\n"
    assert [d.code for d in validate_aop(parse_aop(src), default_registry())] == [
        DiagnosticCode.CYCLE_WITHOUT_EXIT
    ]


def test_loop_with_an_exit_is_fine() -> None:
    src = (
        "---\nid: loop\nname: L\nversion: 1\ntriggers: [x]\n---\n"
        "1. Ask for the order number.\n"
        "2. If it is not found, go back to step 1.\n"
        "3. Help them. RESOLVE.\n"
    )
    assert validate_aop(parse_aop(src), default_registry()) == []


def test_conditional_terminal_on_last_step_still_needs_a_default() -> None:
    src = "---\nid: x\nname: X\nversion: 1\ntriggers: [x]\n---\n1. If they are happy, RESOLVE.\n"
    [diag] = validate_aop(parse_aop(src), default_registry())
    assert diag.code is DiagnosticCode.NO_TERMINAL
    assert "Otherwise RESOLVE" in diag.message


def test_unreachable_hint_explains_sentence_scoped_conditions() -> None:
    src = (
        "---\nid: x\nname: X\nversion: 1\ntriggers: [x]\n---\n"
        "1. If it shipped, share tracking. RESOLVE.\n"
        "2. Explain it is still processing. RESOLVE.\n"
    )
    [diag] = validate_aop(parse_aop(src), default_registry())
    assert diag.code is DiagnosticCode.UNREACHABLE_STEP
    assert "Step 1 always goes to RESOLVE" in diag.message


def test_one_root_cause_is_reported_once() -> None:
    # Step 2 jumps nowhere valid: only BAD_TARGET, no knock-on cycle/terminal reports.
    src = (
        "---\nid: x\nname: X\nversion: 1\ntriggers: [x]\n---\n"
        "1. Ask.\n2. Go to step 7.\n3. RESOLVE.\n"
    )
    codes = [d.code for d in validate_aop(parse_aop(src), default_registry())]
    assert codes == [DiagnosticCode.BAD_TARGET, DiagnosticCode.UNREACHABLE_STEP]


def test_northwind_aops_have_no_diagnostics() -> None:
    aops, diagnostics = check_directory(REPO_AOPS)
    assert diagnostics == []
    assert {a.id for a in aops} == {
        "return_item",
        "where_is_my_order",
        "cancel_order",
        "change_address",
        "refund_request",
        "manage_subscription",
        "loyalty_points",
        "general_question",
    }


def test_northwind_side_effect_aops_all_require_verification() -> None:
    registry = default_registry()
    aops, _ = check_directory(REPO_AOPS)
    for aop in aops:
        if any(registry.get(t).side_effect for t in aop.tool_refs()):
            assert aop.requires_verification, aop.id


def test_missing_default_inside_a_loop_is_reported_once() -> None:
    src = "---\nid: x\nname: X\nversion: 1\ntriggers: [x]\n---\n1. Hi.\n2. If X, go to step 1.\n"
    codes = [d.code for d in validate_aop(parse_aop(src), default_registry())]
    assert codes == [DiagnosticCode.NO_TERMINAL]


def test_bad_target_inside_a_loop_is_reported_once() -> None:
    src = (
        "---\nid: x\nname: X\nversion: 1\ntriggers: [x]\n---\n"
        "1. Ask.\n2. If X, go to step 3. Otherwise go to step 9.\n3. Go to step 2.\n"
    )
    codes = [d.code for d in validate_aop(parse_aop(src), default_registry())]
    assert codes == [DiagnosticCode.BAD_TARGET]
