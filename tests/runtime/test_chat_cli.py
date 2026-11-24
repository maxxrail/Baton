"""`baton-chat`: the fake-LLM message, the conversation driver and the trace view."""

import io
from pathlib import Path

import pytest

from baton.channels.cli import main, render_trace, run_chat
from baton.runtime.agent import Agent
from tests.runtime.support import ScriptedLLM, call, say


def test_fake_llm_exits_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert "BATON_LLM=fake" in err


def test_run_chat_with_trace(agent: Agent, llm: ScriptedLLM) -> None:
    llm.route_to = "cancel_order"
    llm.push(
        call(("cancel_order", {"order_number": "NW-10003"})),  # not allowed on step 1
        say("Which order would you like to cancel?"),
    )
    out = io.StringIO()
    run_chat(agent, ["cancel my order", "", "exit", "never sent"], out, show_trace=True)
    text = out.getvalue()
    assert "agent> Which order would you like to cancel?" in text
    assert "route    -> cancel_order" in text
    assert "cancel_order({'order_number': 'NW-10003'}) -> BLOCKED by tool_allowlist" in text
    assert "guard    tool_allowlist: block" in text
    assert "tokens 360 in / 90 out" in text
    assert len(llm.agent_calls) == 2  # route call excluded; "never sent" after exit wasn't sent


def test_render_trace_shows_escalation() -> None:
    view = render_trace(
        {
            "aop_ref": "refund_request@2",
            "step_before": 6,
            "step_after": 6,
            "status": "escalated",
            "tool_calls": [{"name": "create_refund", "args": {}, "ok": False, "blocked_by": None}],
            "guardrails": [
                {"guardrail": "refund_limit", "action": "escalate", "reason": "over limit"}
            ],
            "escalation_reason": "over limit",
            "input_tokens": 1,
            "output_tokens": 2,
            "latency_ms": 3.0,
            "reply_source": "escalation",
        }
    )
    assert "error None" in view
    assert "refund_limit: escalate - over limit" in view
    assert "handoff  over limit" in view


def test_render_trace_shows_searches_and_grounding() -> None:
    view = render_trace(
        {
            "aop_ref": "general_question@2",
            "tool_calls": [],
            "guardrails": [{"guardrail": "grounding", "action": "rewrite", "reason": "trimmed"}],
            "knowledge_searches": [
                {"query": "price match", "covered": False, "articles": ["promo-codes"]}
            ],
            "grounding": [
                {
                    "action": "abstained",
                    "reason": "1 claim(s), none supported by the evidence",
                    "removed": ["We price match within 14 days."],
                }
            ],
            "reply_source": "abstention",
        }
    )
    assert "kb       'price match' -> ['promo-codes'] (NOT covered)" in view
    assert "grounded abstained: 1 claim(s), none supported" in view
    assert "removed 'We price match within 14 days.'" in view
    assert "reply from abstention" in view
