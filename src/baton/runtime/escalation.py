"""Escalation: hand the conversation to a human with a note they can act on.

`build_handoff` asks the LLM for a structured summary through the `write_handoff` tool and
validates it. If the call fails, returns nothing usable, or the model refuses, a deterministic
summary built from the conversation state and the turn's tool log is used instead, so an
escalation never fails because the model did.

The summary is redacted (emails, phones, card numbers) before it's stored.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from baton.llm.client import LLMClient
from baton.llm.types import Msg, ToolSpec, Usage
from baton.prompts import load_prompt
from baton.runtime.redact import redact
from baton.runtime.state import ConversationState, HandoffSummary

HANDOFF_PROMPT = "handoff"
HANDOFF_TOOL = "write_handoff"
TRANSCRIPT_MESSAGES = 20


class _HandoffOut(BaseModel):
    customer: str = Field(min_length=1, max_length=200)
    intent: str = Field(min_length=1, max_length=500)
    tried: list[str] = Field(max_length=20)
    needed: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class HandoffResult:
    summary: HandoffSummary
    prompt_version: str
    usage: Usage
    latency_ms: float
    error: str | None = None


def _escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def fallback_summary(
    state: ConversationState, procedure: str | None, tried: Sequence[str], reason: str
) -> HandoffSummary:
    if state.verified:
        name = f"{state.customer_name} " if state.customer_name else ""
        customer = f"{name}({state.customer_ref}), verified"
    else:
        customer = "unverified"
    return HandoffSummary(
        customer=customer,
        intent=procedure or "unclear; read the transcript",
        tried=list(tried) or ["nothing yet"],
        needed=f"Take over the conversation. Escalated because: {reason}.",
        reason=reason,
        generated_by="fallback",
    )


def _tool_spec() -> ToolSpec:
    return ToolSpec(
        name=HANDOFF_TOOL,
        description="Write the handoff note for the human teammate.",
        input_schema={
            "type": "object",
            "properties": {
                "customer": {"type": "string"},
                "intent": {"type": "string"},
                "tried": {"type": "array", "items": {"type": "string"}},
                "needed": {"type": "string"},
            },
            "required": ["customer", "intent", "tried", "needed"],
            "additionalProperties": False,
        },
    )


def _redacted(summary: HandoffSummary) -> HandoffSummary:
    return summary.model_copy(
        update={
            "customer": redact(summary.customer),
            "intent": redact(summary.intent),
            "tried": [redact(t) for t in summary.tried],
            "needed": redact(summary.needed),
        }
    )


def build_handoff(
    llm: LLMClient,
    state: ConversationState,
    transcript: Sequence[tuple[str, str]],
    tried: Sequence[str],
    reason: str,
    procedure: str | None,
    prompts_dir: Path | None = None,
) -> HandoffResult:
    """`transcript` is (speaker, text) pairs; `tried` is a readable tool log."""
    prompt = load_prompt(HANDOFF_PROMPT, prompts_dir)
    fallback = fallback_summary(state, procedure, tried, reason)
    system = prompt.render(
        reason=reason,
        procedure=procedure or "none",
        tool_log="\n".join(f"- {t}" for t in tried) or "- (no tools called)",
        state=state.summary(),
    )
    lines = [f"{who}: {_escape(text)}" for who, text in transcript[-TRANSCRIPT_MESSAGES:]]
    user = Msg(role="user", content="<transcript>\n" + "\n".join(lines) + "\n</transcript>")

    start = time.perf_counter()
    try:
        response = llm.complete(system, [user], tools=[_tool_spec()], max_tokens=2048)
    except Exception as exc:  # any provider failure falls back; escalation must not fail
        elapsed = (time.perf_counter() - start) * 1000
        return HandoffResult(fallback, prompt.version, Usage(), elapsed, type(exc).__name__)
    elapsed = (time.perf_counter() - start) * 1000

    for call in response.tool_calls:
        if call.name != HANDOFF_TOOL:
            continue
        try:
            out = _HandoffOut.model_validate(call.input)
        except ValidationError:
            continue
        summary = HandoffSummary(**out.model_dump(), reason=reason, generated_by="llm")
        return HandoffResult(_redacted(summary), prompt.version, response.usage, elapsed)
    return HandoffResult(
        fallback, prompt.version, response.usage, elapsed, "no valid write_handoff call"
    )
