"""Metrics (pass^k vs pass rate), the offline HTML report, and `compare` catching a deliberately
broken AOP."""

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baton.config import get_settings
from baton.sim.cli import main
from baton.sim.compare import compare, render_text
from baton.sim.metrics import markdown_table, percentile, summarize
from baton.sim.report import render_report
from baton.sim.results import CheckResult, ConversationResult, SimRun, Usage
from baton.sim.runner import RunOptions, load_run, run_suite
from tests.runtime.support import REPO
from tests.sim.support import scenario, turn

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
CI = REPO / "evals" / "sims" / "ci"


def _result(sid: str, run: int, passed: bool, **kw: object) -> ConversationResult:
    check = CheckResult(
        kind="must", criterion="explains policy", passed=passed, graded_by="judge", rationale="r"
    )
    data: dict[str, object] = {
        "scenario_id": sid,
        "run": run,
        "passed": passed,
        "stop_reason": "persona_done",
        "end_status": "resolved",
        "turns": [turn(1), turn(2)],
        "checks": [check],
        "store_fingerprint": "abc",
        "turn_latencies_ms": [1000.0, 3000.0],
        "agent_usage": Usage(input_tokens=1000, output_tokens=100, cost_usd=0.003),
    }
    data.update(kw)
    return ConversationResult.model_validate(data)


def test_pass_k_needs_every_run_to_pass() -> None:
    scenarios = [scenario(id="steady"), scenario(id="flaky")]
    results = [
        _result("steady", 1, True),
        _result("steady", 2, True),
        _result("steady", 3, True),
        _result("flaky", 1, True),
        _result("flaky", 2, False),
        _result("flaky", 3, True, end_status="escalated", tool_calls=4, tool_errors=1),
    ]
    summaries, totals = summarize(scenarios, results)
    steady, flaky = summaries
    assert (steady.pass_rate, steady.pass_k) == (1.0, True)
    assert (flaky.pass_rate, flaky.pass_k) == (0.6667, False)
    assert flaky.failed_criteria == {"must: explains policy": 1}
    assert flaky.escalation_rate == 0.3333
    assert flaky.tool_error_rate == 0.25
    assert totals.pass_rate == 0.8333  # 5 of 6 conversations...
    assert totals.pass_k_rate == 0.5  # ...but only 1 of 2 scenarios is reliable
    assert totals.mean_turns == 2.0
    assert totals.p50_latency_ms == 1000.0
    assert totals.p95_latency_ms == 3000.0
    assert totals.mean_cost_usd == 0.003


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 50) == 0.0
    assert percentile([5.0], 95) == 5.0
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0


@pytest.fixture(scope="module")
def ci_run(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("reports")
    return run_suite(get_settings(), RunOptions(suite=CI, scripted=True, out_dir=out), now=NOW)


def test_report_is_self_contained_and_shows_transcripts_and_rationales(ci_run) -> None:
    _run, out = ci_run
    html = (out / "report.html").read_text(encoding="utf-8")
    # Opens offline: no external scripts, styles, fonts or images.
    assert not re.search(r"<(script|link|img)[^>]+(src|href)=", html)
    assert "http://" not in html.replace("http://localhost", "")
    assert "<style>" in html
    # Transcripts, the agent's actions, judge and code rationales, linked traces.
    assert "I want to return the mugs from NW-10002" in html
    assert "create_refund" in html
    assert "blocked by refund_limit" in html
    assert "scripted verdict" in html
    assert 'href="#trace-ci_return_inside_window-r1-2"' in html
    assert 'id="trace-ci_return_inside_window-r1-2"' in html
    assert "pass^k" in html


def test_report_escapes_everything_it_renders(ci_run) -> None:
    run, _ = ci_run
    evil = run.conversations[0].model_copy(
        update={"turns": [turn(1, customer="<script>alert(1)</script>", agent="<b>x</b>")]}
    )
    html = render_report(run.model_copy(update={"conversations": [evil]}))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>x</b>" not in html


def test_results_json_round_trips(ci_run) -> None:
    run, out = ci_run
    loaded = load_run(out)
    assert loaded.id == run.id
    assert loaded.totals == run.totals
    assert len(loaded.conversations) == len(run.conversations)


def _break_return_item(aops: Path) -> None:
    """Remove the create_return tool from the return procedure: the step can no longer run it."""
    path = aops / "return_item.md"
    text = path.read_text(encoding="utf-8")
    broken = text.replace("Create the return with @create_return.", "Create the return.")
    assert broken != text
    path.write_text(broken, encoding="utf-8")


def test_compare_flags_a_regression_when_an_aop_is_broken(tmp_path: Path, ci_run) -> None:
    """Acceptance: break an AOP on purpose, re-run, and `compare` names the regression."""
    baseline, baseline_dir = ci_run
    aops = tmp_path / "aops"
    shutil.copytree(REPO / "aops", aops)
    _break_return_item(aops)
    broken, broken_dir = run_suite(
        get_settings(),
        RunOptions(suite=CI, scripted=True, out_dir=tmp_path / "reports", aops_dir=aops),
        now=NOW,
    )
    assert broken.aop_bundle_hash != baseline.aop_bundle_hash

    result = compare(baseline, broken)
    assert [d.scenario_id for d in result.regressions] == ["ci_return_inside_window"]
    diff = result.regressions[0]
    assert (diff.a_pass_k, diff.b_pass_k) == (True, False)
    assert "tools_called: create_return" in diff.newly_failing
    text = render_text(result)
    assert "REGRESSED  ci_return_inside_window: 100% -> 0%" in text
    assert "1 regression(s)" in text

    # The CLI exits 1 on a regression and 0 when nothing got worse.
    assert main(["compare", str(baseline_dir), str(broken_dir)]) == 1
    assert main(["compare", str(baseline_dir), str(baseline_dir)]) == 0
    assert main(["compare", str(broken_dir), str(baseline_dir)]) == 0  # an improvement


def test_compare_reports_added_and_removed_scenarios(ci_run) -> None:
    run, _ = ci_run
    fewer = run.model_copy(update={"scenarios": run.scenarios[1:]})
    result = compare(run, fewer)
    assert [d.status for d in result.diffs if d.status != "unchanged"] == ["removed"]
    assert compare(fewer, run).diffs[0].status == "added"


def test_markdown_table_has_a_row_per_procedure_and_a_total(
    ci_run, capsys: pytest.CaptureFixture[str]
) -> None:
    run, out = ci_run
    table = markdown_table(run.scenarios, run.totals)
    assert "| `return_item` | 2 | 2/2 | 100.0% |" in table
    assert table.splitlines()[-1].startswith("| **All** | **8** | **100.0%**")
    assert main(["table", str(out)]) == 0
    assert "| Procedure | Scenarios | pass^k |" in capsys.readouterr().out


def test_written_runs_are_redacted_and_same_second_runs_get_distinct_ids(tmp_path: Path) -> None:
    from baton.sim.runner import write_run

    base = _result("s", 1, True)
    leaky = base.model_copy(
        update={
            "turns": [turn(1, customer="I'm ava.thompson@example.com, call +1 206 555 0101")],
            "persona_closing": "bye from ava.thompson@example.com",
        }
    )
    summaries, totals = summarize([scenario(id="s")], [leaky])
    run = SimRun(
        id="sim-20261124-120000",
        created_at=NOW,
        mode="scripted",
        suite="x",
        totals=totals,
        scenarios=summaries,
        conversations=[leaky],
    )
    first, first_dir = write_run(run, tmp_path)
    second, second_dir = write_run(run, tmp_path)
    assert first.id == "sim-20261124-120000"
    assert second.id == second_dir.name == "sim-20261124-120000-02"
    for out in (first_dir, second_dir):
        for name in ("results.json", "report.html"):
            text = (out / name).read_text(encoding="utf-8")
            assert "ava.thompson@example.com" not in text
            assert "555 0101" not in text
