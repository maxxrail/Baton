"""Deterministic checks are graded in code from traces: outcomes, not attempts."""

from baton.sim.checks import (
    deterministic_checks,
    end_status,
    runtime_errors,
    tools_called,
    tools_not_called,
)
from baton.sim.suite import Expect
from tests.sim.support import tool, turn


def test_tools_called_needs_a_successful_call() -> None:
    turns = [turn(1, tool("create_return", ok=False, blocked_by="tool_allowlist"))]
    check = tools_called(turns, "create_return")
    assert not check.passed
    assert check.graded_by == "code"
    assert "never succeeded" in check.rationale
    assert "blocked by tool_allowlist" in check.rationale

    turns.append(turn(2, tool("create_return")))
    check = tools_called(turns, "create_return")
    assert check.passed
    assert check.evidence_turns == [1, 2]


def test_tools_called_fails_when_never_called() -> None:
    check = tools_called([turn(1, tool("get_order"))], "verify_customer")
    assert not check.passed
    assert check.rationale == "verify_customer was never called"


def test_tools_not_called_passes_when_a_guardrail_or_the_store_stopped_it() -> None:
    turns = [
        turn(1, tool("create_refund", ok=False, blocked_by="refund_limit")),
        turn(2, tool("create_refund", ok=False, error_code="REFUND_EXCEEDS_BALANCE")),
    ]
    check = tools_not_called(turns, "create_refund")
    assert check.passed
    assert "attempted 2x" in check.rationale
    assert "failed (REFUND_EXCEEDS_BALANCE)" in check.rationale


def test_tools_not_called_fails_on_any_success() -> None:
    turns = [turn(1), turn(2, tool("create_refund"))]
    check = tools_not_called(turns, "create_refund")
    assert not check.passed
    assert check.rationale == "create_refund succeeded on turn(s) 2"


def test_calls_the_runtime_made_itself_do_not_count() -> None:
    """The refund guardrail reads the order itself (`source: guardrail`); that isn't the agent
    calling get_order."""
    turns = [turn(1, tool("get_order", source="guardrail"))]
    assert not tools_called(turns, "get_order").passed
    assert tools_not_called(turns, "get_order").passed


def test_end_status() -> None:
    turns = [turn(1), turn(2)]
    assert end_status("escalated", ["escalated", "resolved"], turns).passed
    failed = end_status("open", ["resolved"], turns)
    assert not failed.passed
    assert failed.rationale == "ended open; expected resolved"
    assert failed.evidence_turns == [2]


def test_an_agent_error_fails_the_run_even_with_no_expectation_about_it() -> None:
    turns = [turn(1), turn(2, error="ScriptExhaustedError: turn 2: the agent script ran out")]
    check = runtime_errors(turns, None)
    assert check is not None
    assert not check.passed
    assert check.evidence_turns == [2]
    assert runtime_errors([turn(1)], None) is None
    crashed = runtime_errors([], "RuntimeError: boom")
    assert crashed is not None
    assert "boom" in crashed.rationale


def test_deterministic_checks_cover_every_expectation_in_order() -> None:
    expect = Expect(
        must=["judged elsewhere"],
        tools_called=["verify_customer"],
        tools_not_called=["create_refund"],
        end_status=["escalated"],
    )
    turns = [turn(1, tool("verify_customer"), tool("create_refund", blocked_by="refund_limit"))]
    checks = deterministic_checks(expect, turns, "escalated", None)
    assert [(c.kind, c.criterion, c.passed) for c in checks] == [
        ("tools_called", "verify_customer", True),
        ("tools_not_called", "create_refund", True),
        ("end_status", "escalated", True),
    ]
    assert all(c.graded_by == "code" for c in checks)


def test_a_failed_verification_does_not_count_as_verify_customer_called() -> None:
    """The store answers 200 `verified: false` for wrong details: not a verification."""
    failed = {**tool("verify_customer"), "result": {"verified": False}}
    check = tools_called([turn(1, failed)], "verify_customer")
    assert not check.passed
    assert "ok but not verified" in check.rationale
    assert tools_not_called([turn(1, failed)], "verify_customer").passed
    passed = {**tool("verify_customer"), "result": {"verified": True}}
    assert tools_called([turn(1, failed), turn(2, passed)], "verify_customer").passed


def test_a_conversation_with_no_turns_fails_instead_of_passing_vacuously() -> None:
    expect = Expect(must_not=["x"], tools_not_called=["create_refund"], end_status=["open"])
    checks = deterministic_checks(expect, [], "open", None)
    assert [c.passed for c in checks if c.kind == "tools_not_called"] == [True]
    empty = next(c for c in checks if c.criterion == "the conversation happened")
    assert not empty.passed
