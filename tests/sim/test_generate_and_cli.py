"""Persona generation writes reviewable drafts; the CLI's commands and exit codes."""

from pathlib import Path

import pytest

from baton.aop.models import AOPBundle
from baton.llm import FakeLLM, LLMResponse, ToolCall
from baton.sim.cli import main
from baton.sim.generate import GENERATE_TOOL, generate, write_drafts
from baton.sim.suite import load_suite, validate_suite
from baton.tools.registry import ToolRegistry
from tests.runtime.support import REPO

GOOD = {
    "id": "return_item_happy_mug",
    "tags": ["happy"],
    "persona": {
        "name": "Ava",
        "fixture": "ava_inside_window",
        "goal": "return the mugs",
        "knows": {"email": "<seeded>", "order_number": "<seeded>", "zip": "<seeded>"},
    },
    "expect": {"must": ["creates a return"], "tools_called": ["create_return"]},
    "max_turns": 10,
}


def _proposer(*drafts: object) -> FakeLLM:
    return FakeLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="g", name=GENERATE_TOOL, input={"scenarios": list(drafts)})
                ],
                model="fake",
            )
        ]
    )


def test_valid_drafts_are_kept_and_bad_ones_dropped_with_a_reason(
    bundle: AOPBundle, registry: ToolRegistry, tmp_path: Path
) -> None:
    llm = _proposer(
        GOOD,
        {**GOOD, "id": "return_item_bad_tool", "expect": {"tools_called": ["refund_everything"]}},
        {**GOOD, "id": "return_item_bad_tag", "tags": ["spicy"]},
        {**GOOD, "id": "return_item_ghost", "persona": {**GOOD["persona"], "fixture": "ghost"}},
        GOOD,  # duplicate id
        "not even an object",
    )
    drafts = generate(llm, bundle, registry, "return_item", n=6, prompts_dir=REPO / "prompts")
    assert [sc.id for sc in drafts.scenarios] == ["return_item_happy_mug"]
    reasons = "\n".join(drafts.rejected)
    assert "unknown tool 'refund_everything'" in reasons
    assert "unknown tags ['spicy']" in reasons
    assert "unknown fixture 'ghost'" in reasons
    assert "duplicate id" in reasons
    assert "not an object" in reasons

    # The prompt names the procedure, fixtures and tools, and asks for n scenarios.
    system, messages, _ = llm.calls[0]
    assert "exactly 6 scenarios" in system
    context = str(messages[0].content)
    assert '<procedure id="return_item"' in context
    assert "ava_outside_window" in context
    assert "- create_return:" in context

    path = write_drafts(drafts, "return_item", tmp_path / "generated", model="fake")
    assert path.read_text(encoding="utf-8").startswith("# DRAFT scenarios for return_item")
    loaded = load_suite(path)
    assert validate_suite(loaded, bundle, registry) == []
    assert loaded[0].persona.knows["zip"] == "<seeded>"  # placeholders stay unresolved


def test_no_tool_call_gives_no_drafts(bundle: AOPBundle, registry: ToolRegistry) -> None:
    drafts = generate(
        FakeLLM([LLMResponse(text="Here are some ideas...")]), bundle, registry, "return_item"
    )
    assert drafts.scenarios == []
    assert drafts.rejected == ["the model proposed no scenarios"]


def test_cli_validate_passes_on_the_repo_suites(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate"]) == 0
    out = capsys.readouterr().out
    assert "scenarios valid" in out
    assert "adversarial" in out


def test_cli_validate_fails_on_a_broken_suite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bad.yaml").write_text(
        "id: x\naop: nope\npersona: {name: p, goal: g}\nexpect: {must: [a]}\n", encoding="utf-8"
    )
    assert main(["validate", "--suite", str(tmp_path)]) == 1
    assert "unknown aop 'nope'" in capsys.readouterr().out


def test_cli_run_scripted_with_the_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "run",
            "--suite",
            str(REPO / "evals" / "sims" / "ci"),
            "--scripted",
            "--gate",
            "--filter",
            "ci_return_inside_window,ci_human_request",
            "--out",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "2 scenarios, 2 conversations: pass^k 100.0%" in out
    assert "gate passed" in out
    assert len(list(tmp_path.glob("sim-*/report.html"))) == 1


def test_live_commands_need_a_key(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Tests force BATON_LLM=fake and a blank key, so every live command refuses with exit 2."""
    assert main(["run", "--filter", "kb_return_window", "--out", str(tmp_path)]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert main(["calibrate"]) == 2
    assert main(["generate", "--aop", "return_item"]) == 2


def test_scripted_mode_refuses_scenarios_without_scripts(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = main(["run", "--scripted", "--filter", "kb_return_window", "--out", str(tmp_path)])
    assert code == 2
    assert "no script" in capsys.readouterr().err
