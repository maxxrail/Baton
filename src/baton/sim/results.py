"""What a simulation run produces. `results.json` is a `SimRun` dumped as JSON."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

CheckKind = Literal["must", "must_not", "tools_called", "tools_not_called", "end_status", "runtime"]
StopReason = Literal["persona_done", "escalated", "max_turns", "error"]


class TurnRecord(BaseModel):
    """One exchange: the customer's message, the agent's reply and that turn's trace."""

    index: int = Field(description="1-based turn number, as the judge cites it")
    customer: str
    agent: str
    status: str
    replied: bool = True
    trace_id: str = ""
    trace: dict[str, object] = Field(default_factory=dict)


class CheckResult(BaseModel):
    kind: CheckKind
    criterion: str
    passed: bool
    graded_by: Literal["code", "judge"]
    rationale: str
    evidence_turns: list[int] = Field(default_factory=list)
    # For judge checks: what the judge reported (did the behaviour happen?). Code turns that
    # into `passed` (must: occurred; must_not: didn't).
    occurred: bool | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class ConversationResult(BaseModel):
    scenario_id: str
    run: int
    passed: bool
    stop_reason: StopReason
    end_status: str
    turns: list[TurnRecord]
    checks: list[CheckResult]
    persona_closing: str = Field(default="", description="the persona's last words, not sent")
    store_fingerprint: str = Field(description="hash of the store's state when the run started")
    agent_usage: Usage = Field(default_factory=Usage)
    persona_usage: Usage = Field(default_factory=Usage)
    judge_usage: Usage = Field(default_factory=Usage)
    tool_calls: int = 0
    tool_errors: int = 0
    turn_latencies_ms: list[float] = Field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0

    @property
    def escalated(self) -> bool:
        return self.end_status == "escalated"


class ScenarioSummary(BaseModel):
    scenario_id: str
    aop: str
    tags: list[str]
    description: str | None = None
    runs: int
    passed_runs: int
    pass_rate: float
    pass_k: bool = Field(description="every run passed (pass^k)")
    mean_turns: float
    tool_error_rate: float
    escalation_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    mean_tokens: float
    mean_cost_usd: float
    failed_criteria: dict[str, int] = Field(
        default_factory=dict, description="criterion -> number of runs it failed in"
    )


class Totals(BaseModel):
    scenarios: int
    conversations: int
    pass_rate: float = Field(description="passed conversations / conversations")
    pass_k_rate: float = Field(description="scenarios where every run passed / scenarios")
    mean_turns: float
    tool_error_rate: float
    escalation_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    mean_tokens: float = Field(description="agent tokens per conversation")
    mean_cost_usd: float = Field(description="agent cost per conversation")
    sim_cost_usd: float = Field(description="everything: agent + persona + judge")


class SimRun(BaseModel):
    id: str
    created_at: datetime
    mode: Literal["live", "scripted"]
    suite: str
    filter: str | None = None
    runs_per_scenario: int | None = None
    models: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    aop_bundle_hash: str = ""
    git_sha: str | None = None
    totals: Totals
    scenarios: list[ScenarioSummary]
    # Empty in `summary.json`, which the console lists without loading every transcript.
    conversations: list[ConversationResult] = Field(default_factory=list)
    duration_s: float = 0.0
