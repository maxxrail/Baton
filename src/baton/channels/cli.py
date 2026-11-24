"""`baton-chat`: talk to the agent in a terminal, against the local stack.

    uv run python -m scripts.dev      # terminal 1: mock commerce on :8300 (and the API)
    uv run baton-chat --trace          # terminal 2

The agent runs in this process with the real runtime, tools and guardrails; tools call the
mock store at MOCK_COMMERCE_URL and conversations are saved to DATABASE_URL. `--trace` prints
what each turn did: route, step movement, tool calls, help-center searches, the grounding
check, guardrail decisions, tokens and latency. Citations print as footnotes.

Needs a real model (ANTHROPIC_API_KEY). With BATON_LLM=fake it exits with a clear message.
"""

import argparse
import sys
from collections.abc import Iterable
from typing import TextIO, cast

import httpx
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.compiler import AOPCompileError, compile_aops
from baton.api.logging import configure_logging
from baton.channels.citations import render_for_channel
from baton.config import get_settings
from baton.db.models import KBChunk, Trace
from baton.runtime.agent import Agent
from baton.runtime.factory import AgentUnavailableError, build_agent, session_factory_for

EXIT_WORDS = {"exit", "quit", ":q"}
JsonDict = dict[str, object]


def _items(payload: JsonDict, key: str) -> list[JsonDict]:
    value = payload.get(key)
    return [cast(JsonDict, v) for v in value] if isinstance(value, list) else []


def render_trace(payload: JsonDict) -> str:
    """A compact, human-readable view of one turn's trace."""
    lines = []
    route = payload.get("route")
    if isinstance(route, dict):
        lines.append(f"  route    -> {route.get('aop_id')} ({route.get('rationale')})")
    step = f"{payload.get('step_before')} -> {payload.get('step_after')}"
    lines.append(
        f"  aop      {payload.get('aop_ref')}  step {step}  status {payload.get('status')}"
    )
    for tool in _items(payload, "tool_calls"):
        if tool.get("blocked_by"):
            outcome = f"BLOCKED by {tool['blocked_by']}"
        elif tool.get("ok"):
            result = tool.get("result")
            verified = result.get("verified") if isinstance(result, dict) else None
            outcome = "ok" if verified is None else f"ok (verified={verified})"
        else:
            outcome = f"error {tool.get('error_code')}"
        lines.append(f"  tool     {tool.get('name')}({tool.get('args')}) -> {outcome}")
    for search in _items(payload, "knowledge_searches"):
        covered = "covered" if search.get("covered") else "NOT covered"
        lines.append(f"  kb       {search.get('query')!r} -> {search.get('articles')} ({covered})")
    for check in _items(payload, "grounding"):
        lines.append(f"  grounded {check.get('action')}: {check.get('reason')}")
        removed = check.get("removed")
        for sentence in removed if isinstance(removed, list) else []:
            lines.append(f"    removed {str(sentence)[:120]!r}")
    guardrails = _items(payload, "guardrails")
    for g in guardrails:
        if g.get("action") != "allow":
            lines.append(f"  guard    {g.get('guardrail')}: {g.get('action')} - {g.get('reason')}")
    allowed = sum(1 for g in guardrails if g.get("action") == "allow")
    lines.append(
        f"  checks   {allowed} passed | tokens {payload.get('input_tokens')} in / "
        f"{payload.get('output_tokens')} out | {payload.get('latency_ms')} ms | "
        f"reply from {payload.get('reply_source')}"
    )
    drafts = payload.get("rejected_replies")
    for draft in drafts if isinstance(drafts, list) else []:
        lines.append(f"  rejected {str(draft)[:160]!r}")
    if payload.get("escalation_reason"):
        lines.append(f"  handoff  {payload['escalation_reason']}")
    if payload.get("error"):
        lines.append(f"  error    {payload['error']}")
    return "\n".join(lines)


def _trace(sessions: sessionmaker[Session], trace_id: str) -> JsonDict:
    with sessions() as session:
        row = session.scalars(select(Trace).where(Trace.id == trace_id)).one()
        return dict(row.payload)


def run_chat(
    agent: Agent,
    lines: Iterable[str],
    out: TextIO,
    *,
    show_trace: bool,
) -> str:
    """Drive one conversation from `lines`. Returns the conversation id."""
    cid = agent.start_conversation("cli")
    print(f"conversation {cid}. Type 'exit' to quit.", file=out)
    for raw in lines:
        text = raw.strip()
        if text.lower() in EXIT_WORDS:
            break
        if not text:
            continue
        reply = agent.handle_turn(cid, text)
        print(f"agent> {render_for_channel(reply.text, reply.citations, 'cli')}", file=out)
        if show_trace:
            print(render_trace(_trace(agent.sessions, reply.trace_id)), file=out)
        if reply.status != "open":
            print(f"[conversation {reply.status}]", file=out)
    return cid


def _stdin_lines() -> Iterable[str]:
    while True:
        try:
            yield input("you> ")
        except (EOFError, KeyboardInterrupt):
            return


def _preflight(sessions: sessionmaker[Session], store_url: str) -> str | None:
    """A readable reason the stack isn't ready, or None."""
    inspector = inspect(sessions.kw["bind"])
    has_state = inspector.has_table("conversations") and any(
        c["name"] == "state" for c in inspector.get_columns("conversations")
    )
    if not has_state or not inspector.has_table("kb_chunks"):
        return "the database isn't migrated: run `uv run alembic upgrade head`"
    try:
        httpx.get(f"{store_url}/healthz", timeout=2.0).raise_for_status()
    except httpx.HTTPError:
        return f"mock commerce isn't reachable at {store_url}: run `uv run python -m scripts.dev`"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baton-chat", description=__doc__.split("\n\n")[0])
    parser.add_argument("--trace", action="store_true", help="print each turn's trace")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("WARNING")  # keep the terminal for the conversation
    try:
        bundle = compile_aops(settings.aops_dir, prompts_dir=settings.prompts_dir)
        sessions = session_factory_for(settings)
        agent = build_agent(settings, bundle, sessions)
    except (AgentUnavailableError, AOPCompileError) as exc:
        print(f"baton-chat: {exc}", file=sys.stderr)
        return 2
    problem = _preflight(sessions, settings.mock_commerce_url)
    if problem:
        print(f"baton-chat: {problem}", file=sys.stderr)
        return 2
    with sessions() as session:
        if session.query(KBChunk.id).first() is None:
            print(
                "baton-chat: warning: the help center isn't indexed, so policy questions will "
                "get 'I'm not sure'. Run `uv run baton-kb ingest`.",
                file=sys.stderr,
            )
    print(f"Northwind Goods support | model {settings.baton_model}")
    run_chat(agent, _stdin_lines(), sys.stdout, show_trace=args.trace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
