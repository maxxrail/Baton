import shutil
from pathlib import Path

import pytest

from baton.aop.compiler import (
    AOPCompileError,
    aop_files,
    bundle_hash,
    check_source,
    compile_aops,
)
from baton.aop.models import DiagnosticCode, Terminal

REPO = Path(__file__).resolve().parents[2]
AOPS = REPO / "aops"
PROMPTS = REPO / "prompts"
NORTHWIND = {
    "return_item",
    "where_is_my_order",
    "cancel_order",
    "change_address",
    "refund_request",
    "manage_subscription",
    "loyalty_points",
    "general_question",
}


@pytest.fixture
def aop_dir(tmp_path: Path) -> Path:
    """A writable copy of the Northwind AOPs."""
    target = tmp_path / "aops"
    shutil.copytree(AOPS, target)
    return target


def test_all_eight_northwind_aops_compile_with_zero_errors() -> None:
    bundle = compile_aops(AOPS, prompts_dir=PROMPTS)
    assert set(bundle.aops) == NORTHWIND
    assert bundle.diagnostics == []
    assert len(bundle.bundle_hash) == 64


def test_readme_is_not_an_aop() -> None:
    assert "README.md" not in {p.name for p in aop_files(AOPS)}


def test_step_prompt_contains_step_policies_tools_and_exits() -> None:
    aop = compile_aops(AOPS, prompts_dir=PROMPTS).get("return_item")
    prompt = aop.step(2).prompt
    assert "Return or exchange an item (return_item@2)" in prompt
    assert "step 2 of 6" in prompt
    assert aop.step(2).text in prompt
    assert "- Returns are accepted within 30 days of delivery." in prompt
    assert "- get_order: Get one order" in prompt
    assert "(needs verification)" in prompt
    assert "If the order is not delivered: RESOLVE" in prompt
    assert "When this step is done: step 3" in prompt
    assert "verify_customer" in prompt
    assert "$" not in prompt.replace("$100", "")  # every placeholder was filled


def test_side_effect_tools_are_flagged_in_step_prompts() -> None:
    aop = compile_aops(AOPS, prompts_dir=PROMPTS).get("cancel_order")
    assert "cancel_order: " in aop.step(4).prompt
    assert "(changes data, needs verification)" in aop.step(4).prompt


def test_allowed_tools_are_per_step() -> None:
    aop = compile_aops(AOPS, prompts_dir=PROMPTS).get("return_item")
    assert aop.step(1).allowed_tools == ["get_customer_orders"]
    assert aop.step(4).allowed_tools == []
    assert aop.step(5).allowed_tools == ["create_return"]


def test_tool_free_aop_prompt_says_so() -> None:
    aop = compile_aops(AOPS, prompts_dir=PROMPTS).get("general_question")
    assert "(none: answer without tools)" in aop.step(1).prompt
    assert "does not need identity verification" in aop.step(1).prompt


def test_compiled_aop_round_trips_through_json() -> None:
    bundle = compile_aops(AOPS, prompts_dir=PROMPTS)
    again = type(bundle).model_validate_json(bundle.model_dump_json())
    assert again == bundle
    assert again.get("return_item").step(2).transitions[0].target is Terminal.RESOLVE


def test_bundle_hash_is_stable_and_tracks_edits(aop_dir: Path) -> None:
    first = compile_aops(aop_dir, prompts_dir=PROMPTS).bundle_hash
    assert compile_aops(aop_dir, prompts_dir=PROMPTS).bundle_hash == first
    path = aop_dir / "cancel_order.md"
    path.write_text(path.read_text().replace("Cancel an order", "Cancel an order now"))
    assert compile_aops(aop_dir, prompts_dir=PROMPTS).bundle_hash != first


def test_bundle_hash_tracks_the_step_prompt_template(tmp_path: Path) -> None:
    bundle = compile_aops(AOPS, prompts_dir=PROMPTS)
    prompts = tmp_path / "prompts"
    shutil.copytree(PROMPTS, prompts)
    (prompts / "aop_step.md").write_text("Step $step_number: $step_text\n" + "$aop_name $aop_ref "
        "$step_count $policies $tools $transitions $verification\n")  # fmt: skip
    changed = compile_aops(AOPS, prompts_dir=prompts)
    assert changed.bundle_hash != bundle.bundle_hash
    assert changed.get("return_item").source_hash == bundle.get("return_item").source_hash
    assert bundle_hash(list(bundle.aops.values())) == bundle.bundle_hash


def test_compile_fails_with_every_error(aop_dir: Path) -> None:
    (aop_dir / "broken.md").write_text(
        "---\nid: broken\nname: B\nversion: 1\ntriggers: [x]\n---\n1. Use @nope. Go to step 4.\n"
    )
    with pytest.raises(AOPCompileError) as info:
        compile_aops(aop_dir, prompts_dir=PROMPTS)
    codes = {d.code for d in info.value.diagnostics}
    assert codes == {DiagnosticCode.UNKNOWN_TOOL, DiagnosticCode.BAD_TARGET}
    assert "broken:1" in str(info.value)


def test_warnings_do_not_stop_compilation(aop_dir: Path) -> None:
    words = " ".join(["word"] * 70)
    (aop_dir / "wordy.md").write_text(
        f"---\nid: wordy\nname: W\nversion: 1\ntriggers: [x]\n---\n1. {words}. RESOLVE.\n"
    )
    bundle = compile_aops(aop_dir, prompts_dir=PROMPTS)
    assert "wordy" in bundle.aops
    assert [d.code for d in bundle.diagnostics] == [DiagnosticCode.LONG_STEP]


def test_missing_or_empty_directory_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(AOPCompileError, match="does not exist"):
        compile_aops(tmp_path / "nope", prompts_dir=PROMPTS)
    with pytest.raises(AOPCompileError, match=r"no AOP files"):
        compile_aops(tmp_path, prompts_dir=PROMPTS)


def test_check_source_validates_a_draft() -> None:
    aop, diagnostics = check_source((AOPS / "return_item.md").read_text())
    assert aop is not None
    assert aop.id == "return_item"
    assert diagnostics == []

    aop, diagnostics = check_source("no frontmatter")
    assert aop is None
    assert [d.code for d in diagnostics] == [DiagnosticCode.PARSE_ERROR]
