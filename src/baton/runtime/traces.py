"""Turn traces: one record per agent turn, written to the `traces` table.

A trace answers "why did the agent say that?": which procedure and step it was on (and the
exact compiled version), which prompt versions built the system prompt, what the router
decided, every model call (tokens, latency, stop reason), every tool call (args, result, error,
which guardrail blocked it), every guardrail decision, and the outcome.

Payloads are redacted before they're stored: emails, phones and card numbers are masked by
`redact_value`, and fields that hold verification secrets or addresses (`zip`, `line1`) are
replaced outright.
"""

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from baton.aop.router import RouteDecision
from baton.db.models import Trace
from baton.knowledge.grounding import GroundingOutcome
from baton.runtime.guardrails import GuardrailDecision
from baton.runtime.redact import redact_value

SECRET_KEYS = frozenset({"zip", "line1"})

LLMPurpose = Literal["route", "agent", "rewrite", "handoff", "grounding"]


class KnowledgeSearchTrace(BaseModel):
    """One `search_knowledge` call: what was asked and which passages came back, in order."""

    query: str
    covered: bool
    articles: list[str]
    chunk_ids: list[int]


class LLMCallTrace(BaseModel):
    purpose: LLMPurpose
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    stop_reason: str = ""
    error: str | None = None


class ToolCallTrace(BaseModel):
    name: str
    args: dict[str, object]
    ok: bool
    result: dict[str, object] | None = None
    error_code: str | None = None
    blocked_by: str | None = Field(default=None, description="guardrail that stopped the call")
    source: Literal["agent", "guardrail"] = Field(
        default="agent", description="'guardrail' for reads the runtime made to check a call"
    )
    latency_ms: float = 0.0


class TurnTrace(BaseModel):
    conversation_id: str
    turn: int
    channel: str
    aop_ref: str | None = None
    aop_source_hash: str | None = None
    step_prompt_version: str | None = None
    step_before: int | None = None
    step_after: int | None = None
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    route: RouteDecision | None = None
    llm_calls: list[LLMCallTrace] = Field(default_factory=list)
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    guardrails: list[GuardrailDecision] = Field(default_factory=list)
    knowledge_searches: list[KnowledgeSearchTrace] = Field(default_factory=list)
    grounding: list[GroundingOutcome] = Field(
        default_factory=list, description="every grounding check run on a reply this turn"
    )
    citations: list[str] = Field(default_factory=list, description="article ids in the reply")
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    status: str = "open"
    escalation_reason: str | None = None
    reply_source: Literal["llm", "rewrite", "fallback", "escalation", "holding", "abstention"] = (
        "llm"
    )
    rejected_replies: list[str] = Field(
        default_factory=list, description="drafts an output guardrail blocked, for debugging"
    )
    error: str | None = None

    def add_llm_call(self, call: LLMCallTrace) -> None:
        self.llm_calls.append(call)
        self.input_tokens += call.input_tokens
        self.output_tokens += call.output_tokens


def _mask_secrets(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if k in SECRET_KEYS and v else _mask_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_secrets(v) for v in value]
    return value


def trace_payload(trace: TurnTrace) -> dict[str, object]:
    payload = redact_value(_mask_secrets(trace.model_dump(mode="json")))
    assert isinstance(payload, dict)
    return payload


def save_trace(session: Session, trace: TurnTrace, message_id: int | None) -> str:
    row = Trace(
        conversation_id=trace.conversation_id, message_id=message_id, payload=trace_payload(trace)
    )
    session.add(row)
    session.flush()
    return row.id
