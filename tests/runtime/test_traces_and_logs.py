"""Every turn leaves a complete trace, and nothing in logs is raw PII."""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from baton.api.logging import configure_logging
from baton.db import AOPVersion
from baton.runtime.agent import Agent
from tests.runtime.support import ScriptedLLM, advance, call, say, traces_of

AVA = {"customer_id": "cus_0001", "order_number": "NW-10002", "zip": "98101"}


def _conversation(agent: Agent, llm: ScriptedLLM) -> str:
    """Three turns: an ordinary one, an escalation, and a message the agent stays silent on."""
    cid = agent.start_conversation("web")
    llm.route_to = "where_is_my_order"
    llm.push(
        call(("lookup_customer", {"email": "ava.thompson@example.com"}), ("verify_customer", AVA)),
        call(advance(2), ("get_order", {"order_number": "NW-10002"})),
        say("NW-10002 was delivered 10 days ago."),
    )
    agent.handle_turn(
        cid, "where is NW-10002? ava.thompson@example.com, zip 98101, call me at 206-555-0101"
    )
    agent.handle_turn(cid, "it never arrived, let me speak to a person")
    agent.handle_turn(cid, "hello? my email is ava.thompson@example.com")
    return cid


def test_every_turn_has_a_complete_trace(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    traces = traces_of(sessions, _conversation(agent, llm))
    assert [t["turn"] for t in traces] == [1, 2, 3]
    for trace in traces:
        assert trace["aop_ref"] == "where_is_my_order@1"
        assert trace["aop_source_hash"]
        assert str(trace["step_prompt_version"]).startswith("aop_step@")
        versions = trace["prompt_versions"]
        assert isinstance(versions, dict)
        assert versions["agent"].startswith("agent@")
        assert versions["brand"].startswith("brand@")
        assert isinstance(trace["tool_calls"], list)
        assert isinstance(trace["guardrails"], list)
        assert trace["guardrails"] or trace["reply_source"] == "human_owned"
        assert float(str(trace["latency_ms"])) > 0
        assert "input_tokens" in trace
        assert "output_tokens" in trace

    first, escalated, silent = traces
    assert first["route"]["aop_id"] == "where_is_my_order"
    assert first["prompt_versions"]["router"].startswith("router@")
    assert first["input_tokens"] == 120 * 4  # route + three agent calls, 120 each
    assert first["output_tokens"] == 30 * 4
    assert {c["purpose"] for c in first["llm_calls"]} == {"route", "agent"}
    assert [c["name"] for c in first["tool_calls"]][:2] == ["lookup_customer", "verify_customer"]
    assert first["tool_calls"][1]["result"] == {"verified": True}

    assert escalated["status"] == "escalated"
    assert escalated["escalation_reason"] == "customer asked for a human"
    assert escalated["prompt_versions"]["handoff"].startswith("handoff@")
    assert [c["purpose"] for c in escalated["llm_calls"]] == ["handoff"]
    assert silent["reply_source"] == "human_owned"
    assert silent["llm_calls"] == []


def test_trace_aop_version_has_a_stored_row(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    trace = traces_of(sessions, _conversation(agent, llm))[0]
    with sessions() as session:
        row = session.scalars(
            select(AOPVersion).where(
                AOPVersion.aop_id == "where_is_my_order",
                AOPVersion.source_hash == trace["aop_source_hash"],
                AOPVersion.step_prompt_version == trace["step_prompt_version"],
            )
        ).one()
    assert row.version == 1


def test_logs_contain_no_raw_emails_or_phones(
    agent: Agent, llm: ScriptedLLM, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG")
    _conversation(agent, llm)
    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert any(e["event"] == "agent_turn" for e in events)  # the logs did capture the turns
    assert "ava.thompson@example.com" not in out
    assert "206-555-0101" not in out
    assert "5550101" not in out
    assert "98101" not in out  # the verification zip: redact() can't spot it, so never logged
    turn = next(e for e in events if e["event"] == "agent_turn")
    assert turn["message_chars"] > 0


def test_trace_payloads_contain_no_raw_pii(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    blob = json.dumps(traces_of(sessions, _conversation(agent, llm)))
    assert "ava.thompson@example.com" not in blob
    assert "206-555-0101" not in blob
    assert "98101" not in blob  # verification zip and shipping address are masked
    assert "12 Harbor Ave" not in blob
