"""The simulator end to end with scripted models: real runtime, tools, guardrails, mock store."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baton.aop.models import AOPBundle
from baton.config import get_settings
from baton.llm import FakeLLM, LLMResponse
from baton.sim.runner import RunOptions, run_suite, scripted_models
from baton.sim.sandbox import Sandbox
from baton.sim.simulator import SimModels, Simulator
from baton.sim.suite import Scenario, load_suite
from tests.runtime.support import REPO

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
CI = REPO / "evals" / "sims" / "ci"


def _ci(scenario_id: str) -> Scenario:
    return next(sc for sc in load_suite(CI) if sc.id == scenario_id).resolved()


@pytest.fixture
def box(tmp_path: Path) -> Iterator[Sandbox]:
    sandbox = Sandbox(tmp_path / "box", NOW)
    yield sandbox
    sandbox.close()


def _sim(bundle: AOPBundle, models_for=scripted_models, **kw: object) -> Simulator:
    return Simulator(bundle, models_for, prompts_dir=REPO / "prompts", now=NOW, **kw)


def test_the_ci_suite_passes_offline(tmp_path: Path) -> None:
    """What CI runs on every push: every scripted scenario passes, guardrail ones included."""
    run, out = run_suite(
        get_settings(),
        RunOptions(suite=CI, scripted=True, out_dir=tmp_path, concurrency=3),
        now=NOW,
    )
    failures = {
        r.scenario_id: [c.rationale for c in r.checks if not c.passed]
        for r in run.conversations
        if not r.passed
    }
    assert failures == {}
    assert run.totals.pass_k_rate == 1.0
    assert run.mode == "scripted"
    assert (out / "results.json").is_file()
    assert (out / "report.html").is_file()
    by_id = {r.scenario_id: r for r in run.conversations}
    # The guardrails did the work, not the script: the model *tried* each of these.
    refund = by_id["ci_refund_over_limit"]
    assert refund.end_status == "escalated"
    assert "blocked by refund_limit" in next(
        c.rationale for c in refund.checks if c.criterion == "create_refund"
    )
    skip = by_id["ci_skip_verification"]
    assert "blocked by identity_gate" in next(
        c.rationale for c in skip.checks if c.criterion == "get_order"
    )


def test_reset_gives_the_same_starting_state_for_a_side_effecting_scenario(
    bundle: AOPBundle, box: Sandbox
) -> None:
    """Acceptance: the same side-effecting scenario twice, same sandbox, same starting state."""
    sim = _sim(bundle)
    sc = _ci("ci_return_inside_window")
    first = sim.run_conversation(sc, 1, box)
    assert first.passed
    # The run changed the store: the RMA exists and the fingerprint moved.
    assert [r["rma_number"] for r in box.store_get("/orders/NW-10002")["returns"]] == [
        "RMA-10002-1"
    ]
    assert box.fingerprint() != first.store_fingerprint

    second = sim.run_conversation(sc, 2, box)
    assert second.store_fingerprint == first.store_fingerprint
    # Without the reset the second create_return would fail with RETURN_ALREADY_EXISTS.
    assert second.passed
    assert [r["rma_number"] for r in box.store_get("/orders/NW-10002")["returns"]] == [
        "RMA-10002-1"
    ]


def test_parallel_runs_are_independent_and_start_from_the_same_state(bundle: AOPBundle) -> None:
    sc = _ci("ci_return_inside_window")
    results = _sim(bundle, concurrency=3).run([sc], runs=4)
    assert [r.run for r in results] == [1, 2, 3, 4]
    assert all(r.passed for r in results)
    assert len({r.store_fingerprint for r in results}) == 1


def test_a_script_that_runs_out_fails_the_run_with_the_reason(
    bundle: AOPBundle, box: Sandbox
) -> None:
    sc = _ci("ci_return_inside_window")
    assert sc.script is not None
    short = sc.script.model_copy(
        update={"turns": [sc.script.turns[0], sc.script.turns[1].model_copy(update={"agent": []})]}
    )
    result = _sim(bundle).run_conversation(sc.model_copy(update={"script": short}), 1, box)
    assert not result.passed
    runtime = next(c for c in result.checks if c.kind == "runtime")
    assert "the agent script ran out" in runtime.rationale
    assert runtime.evidence_turns == [2]


def test_leftover_script_steps_are_reported(bundle: AOPBundle, box: Sandbox) -> None:
    sc = _ci("ci_kb_question")
    assert sc.script is not None
    turn = sc.script.turns[0]
    longer = turn.model_copy(update={"agent": [*turn.agent, turn.agent[-1]]})
    script = sc.script.model_copy(update={"turns": [longer]})
    result = _sim(bundle).run_conversation(sc.model_copy(update={"script": script}), 1, box)
    assert not result.passed
    assert result.error == "script: turn 1: 1 scripted step(s) unused"


def _talker(lines: list[str]):
    def models(sc: Scenario) -> SimModels:
        # The agent: "Could you tell me more?" forever (a general_question clarifying step).
        agent = FakeLLM(lambda s, m, t: LLMResponse(text="Could you tell me more?", model="x"))
        persona = FakeLLM([LLMResponse(text=line, model="x") for line in lines])
        judge = FakeLLM([])
        return SimModels(agent=agent, persona=persona, judge=judge)

    return models


def test_max_turns_stops_the_conversation_without_an_unused_persona_call(
    bundle: AOPBundle, box: Sandbox
) -> None:
    sc = Scenario.model_validate(
        {
            "id": "chatty",
            "aop": "general_question",
            "persona": {"name": "p", "goal": "g"},
            "max_turns": 3,
            "expect": {"end_status": ["open"]},
        }
    )
    # Exactly three lines: a fourth persona call would raise FakeLLMExhaustedError.
    result = _sim(bundle, models_for=_talker(["a", "b", "c"])).run_conversation(sc, 1, box)
    assert result.stop_reason == "max_turns"
    assert [t.customer for t in result.turns] == ["a", "b", "c"]
    assert result.passed


def test_a_goodbye_is_sent_as_the_last_message_then_the_run_ends(
    bundle: AOPBundle, box: Sandbox
) -> None:
    sc = Scenario.model_validate(
        {
            "id": "brief",
            "aop": "general_question",
            "persona": {"name": "p", "goal": "g"},
            "expect": {"end_status": ["open"]},
        }
    )
    result = _sim(bundle, models_for=_talker(["hello", "ok thanks bye [DONE]"])).run_conversation(
        sc, 1, box
    )
    assert result.stop_reason == "persona_done"
    # "ok thanks bye" reaches the agent (so it can close the request); no further persona call.
    assert [t.customer for t in result.turns] == ["hello", "ok thanks bye"]
    assert result.persona_closing == ""


def test_a_bare_done_ends_the_run_without_another_message(bundle: AOPBundle, box: Sandbox) -> None:
    sc = Scenario.model_validate(
        {
            "id": "brief",
            "aop": "general_question",
            "persona": {"name": "p", "goal": "g"},
            "expect": {"end_status": ["open"]},
        }
    )
    result = _sim(bundle, models_for=_talker(["hello", "[DONE]"])).run_conversation(sc, 1, box)
    assert [t.customer for t in result.turns] == ["hello"]


def test_escalation_ends_the_run_immediately(bundle: AOPBundle, box: Sandbox) -> None:
    result = _sim(bundle).run_conversation(_ci("ci_human_request"), 1, box)
    assert result.stop_reason == "escalated"
    assert result.end_status == "escalated"
    assert len(result.turns) == 1


def test_traces_and_usage_are_recorded_per_turn(bundle: AOPBundle, box: Sandbox) -> None:
    result = _sim(bundle).run_conversation(_ci("ci_return_inside_window"), 1, box)
    assert len(result.turns) == 3
    assert all(t.trace.get("conversation_id") for t in result.turns)
    assert result.turns[1].trace["aop_ref"] == "return_item@2"
    assert result.tool_calls == 4  # lookup, verify, get_order, create_return (no control tools)
    assert result.tool_errors == 0
    assert result.agent_usage.input_tokens > 0
    assert len(result.turn_latencies_ms) == 3


def test_a_crash_outside_the_turn_loop_fails_that_run_and_keeps_the_rest(
    bundle: AOPBundle,
) -> None:
    good = _ci("ci_human_request")
    bad = _ci("ci_kb_question")

    def models(sc: Scenario) -> SimModels:
        if sc.id == bad.id:
            raise RuntimeError("model config for ava.thompson@example.com is broken")
        return scripted_models(sc)

    results = _sim(bundle, models_for=models, concurrency=2).run([good, bad], runs=1)
    by_id = {r.scenario_id: r for r in results}
    assert by_id[good.id].passed
    crashed = by_id[bad.id]
    assert not crashed.passed
    assert crashed.stop_reason == "error"
    assert "RuntimeError" in crashed.checks[0].rationale
    assert "ava.thompson@example.com" not in crashed.checks[0].rationale  # redacted


def test_a_persona_that_leaves_immediately_fails_the_run(bundle: AOPBundle, box: Sandbox) -> None:
    sc = Scenario.model_validate(
        {
            "id": "silent",
            "aop": "refund_request",
            "persona": {"name": "p", "goal": "g"},
            "expect": {"must_not": ["issues a refund"], "end_status": ["open"]},
        }
    )
    result = _sim(bundle, models_for=_talker(["[DONE]"])).run_conversation(sc, 1, box)
    assert result.turns == []
    assert not result.passed


def test_a_progress_callback_that_raises_does_not_lose_results(bundle: AOPBundle) -> None:
    def progress(_r: object) -> None:
        raise ValueError("terminal closed")

    results = _sim(bundle, progress=progress).run([_ci("ci_human_request")], runs=2)
    assert [r.passed for r in results] == [True, True]
