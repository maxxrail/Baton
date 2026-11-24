"""The simulator: personas hold full conversations with the real agent runtime, in-process.

For each (scenario, run):
1. take a free sandbox and reset it to the seed (`Sandbox.reset`), so runs are independent;
2. build a fresh `Agent` on that sandbox's store and database (real tools, guardrails, KB);
3. the persona speaks first; then agent turn, persona reply, ... until the persona writes
   `[DONE]`, the agent escalates (a human owns the conversation and the agent goes silent), or
   `max_turns` customer messages have been sent. A *resolve* does not end the run on its own:
   the persona decides whether it's done, so multi-intent scenarios can raise their next
   request (a resolved conversation reopens on a new message, as in production);
4. grade it: deterministic checks in code (`baton.sim.checks`), then `must` / `must_not` by
   the LLM judge.

Runs execute in a thread pool (`concurrency` workers, one sandbox each). The agent and model
clients are synchronous; the work is waiting on the model, so threads are enough.
"""

import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue

import structlog
from sqlalchemy import select

from baton.aop.models import AOPBundle
from baton.db import Trace
from baton.knowledge.base import KnowledgeBase
from baton.llm.client import LLMClient
from baton.runtime.agent import Agent
from baton.runtime.guardrails import GuardrailConfig
from baton.runtime.redact import redact
from baton.sim.checks import deterministic_checks
from baton.sim.judge import Criterion, Judge, judge_turns
from baton.sim.persona import PersonaPlayer
from baton.sim.pricing import trace_usage
from baton.sim.results import CheckResult, ConversationResult, StopReason, TurnRecord, Usage
from baton.sim.sandbox import Sandbox
from baton.sim.suite import CONTROL_TOOLS, Scenario
from baton.tools.northwind import NorthwindTools

log = structlog.get_logger()


@dataclass
class SimModels:
    """The models one run uses. Scripted runs also get per-turn hooks."""

    agent: LLMClient
    persona: LLMClient
    judge: LLMClient
    before_turn: Callable[[int], None] | None = None
    finish: Callable[[], str | None] | None = None


ModelsFor = Callable[[Scenario], SimModels]
Progress = Callable[[ConversationResult], None]


class Simulator:
    def __init__(
        self,
        bundle: AOPBundle,
        models_for: ModelsFor,
        *,
        knowledge: KnowledgeBase | None = None,
        prompts_dir: Path | None = None,
        config: GuardrailConfig | None = None,
        concurrency: int = 4,
        workdir: Path | None = None,
        now: datetime | None = None,
        progress: Progress | None = None,
    ) -> None:
        self.bundle = bundle
        self.models_for = models_for
        self.knowledge = knowledge
        self.prompts_dir = prompts_dir
        self.config = config
        self.concurrency = max(1, concurrency)
        self.workdir = workdir
        # Frozen for the whole suite: every reset seeds identical data, and "45 days ago"
        # means the same thing in the first run and the last.
        self.now = now or datetime.now(UTC).replace(microsecond=0)
        self.progress = progress

    # ------------------------------------------------------------------ suite

    def run(self, scenarios: list[Scenario], runs: int | None = None) -> list[ConversationResult]:
        """Every scenario `runs` times (default: each scenario's own `runs`), in parallel."""
        resolved = [sc.resolved() for sc in scenarios]
        jobs = [(sc, r) for sc in resolved for r in range(1, (runs or sc.runs) + 1)]
        if not jobs:
            return []
        workers = min(self.concurrency, len(jobs))
        # ignore_cleanup_errors: on Windows a file still held open must not turn finished
        # results into an exception.
        with tempfile.TemporaryDirectory(
            prefix="baton-sim-", dir=self.workdir, ignore_cleanup_errors=True
        ) as tmp:
            free: Queue[Sandbox] = Queue()
            sandboxes: list[Sandbox] = []

            def job(sc: Scenario, run: int) -> ConversationResult:
                box = free.get()
                try:
                    result = self.run_conversation(sc, run, box)
                finally:
                    free.put(box)
                if self.progress:
                    try:
                        self.progress(result)
                    except Exception:
                        log.warning("sim_progress_failed", scenario=sc.id, run=run)
                return result

            try:
                for i in range(workers):
                    sandboxes.append(Sandbox(Path(tmp) / f"worker-{i}", self.now))
                    free.put(sandboxes[-1])
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sim") as pool:
                    futures = [pool.submit(job, sc, r) for sc, r in jobs]
                    return [f.result() for f in futures]
            finally:
                for box in sandboxes:
                    box.close()

    # ------------------------------------------------------------------ one conversation

    def run_conversation(self, scenario: Scenario, run: int, box: Sandbox) -> ConversationResult:
        """Run and grade one conversation. `scenario` must already be `resolved()`.

        Never raises: a failure anywhere (reset, building the models, the judge) becomes a
        failed run with the reason, so one broken run can't throw away the rest of the suite.
        """
        started = time.perf_counter()
        try:
            return self._run_conversation(scenario, run, box, started)
        except Exception as exc:
            error = f"{type(exc).__name__}: {redact(str(exc))[:300]}"
            log.error("sim_run_crashed", scenario=scenario.id, run=run, error=error)
            return ConversationResult(
                scenario_id=scenario.id,
                run=run,
                passed=False,
                stop_reason="error",
                end_status="open",
                turns=[],
                checks=[
                    CheckResult(
                        kind="runtime",
                        criterion="no agent errors",
                        passed=False,
                        graded_by="code",
                        rationale=f"the run crashed: {error}",
                    )
                ],
                store_fingerprint="",
                error=error,
                duration_s=round(time.perf_counter() - started, 2),
            )

    def _run_conversation(
        self, scenario: Scenario, run: int, box: Sandbox, started: float
    ) -> ConversationResult:
        fingerprint = box.reset()
        models = self.models_for(scenario)
        tools = NorthwindTools(box.commerce(), knowledge=self.knowledge)
        agent = Agent(
            self.bundle,
            models.agent,
            tools,
            box.sessions,
            config=self.config,
            prompts_dir=self.prompts_dir,
        )
        persona = PersonaPlayer(models.persona, scenario.persona, self.prompts_dir)

        turns: list[TurnRecord] = []
        persona_usage = Usage()
        stop: StopReason = "max_turns"
        closing = ""
        error: str | None = None
        status = "open"
        try:
            conversation_id = agent.start_conversation("web")
            line = persona.first()
            persona_usage = persona_usage.add(line.usage)
            for index in range(1, scenario.max_turns + 1):
                if line.done:
                    stop, closing = "persona_done", line.closing
                    break
                if models.before_turn:
                    models.before_turn(index)
                reply = agent.handle_turn(conversation_id, line.text)
                status = reply.status
                turns.append(
                    TurnRecord(
                        index=index,
                        customer=line.text,
                        agent=reply.text,
                        status=reply.status,
                        replied=reply.replied,
                        trace_id=reply.trace_id,
                        trace=self._trace(box, reply.trace_id),
                    )
                )
                if reply.status == "escalated":
                    stop = "escalated"
                    break
                if index == scenario.max_turns:
                    break  # don't pay for a persona line nobody will read
                line = persona.reply_to(reply.text)
                persona_usage = persona_usage.add(line.usage)
        except Exception as exc:
            stop = "error"
            error = f"{type(exc).__name__}: {redact(str(exc))[:300]}"
            log.error("sim_turn_crashed", scenario=scenario.id, run=run, error=error)
        if models.finish:
            problem = models.finish()
            if problem and error is None:
                error = f"script: {problem}"

        checks = deterministic_checks(scenario.expect, turns, status, error)
        criteria = [Criterion("must", c) for c in scenario.expect.must]
        criteria += [Criterion("must_not", c) for c in scenario.expect.must_not]
        judge_usage = Usage()
        if stop == "error":
            checks += [
                CheckResult(
                    kind=c.kind,
                    criterion=c.text,
                    passed=False,
                    graded_by="judge",
                    rationale="not graded: the run crashed",
                )
                for c in criteria
            ]
        else:
            verdict = Judge(models.judge, self.prompts_dir).grade(
                criteria, judge_turns(turns), status
            )
            checks += verdict.checks
            judge_usage = verdict.usage

        agent_usage = Usage()
        tool_calls = tool_errors = 0
        for turn in turns:
            agent_usage = agent_usage.add(trace_usage(turn.trace))
            calls = turn.trace.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict) or call.get("source", "agent") != "agent":
                    continue
                if call.get("name") in CONTROL_TOOLS:
                    continue
                tool_calls += 1
                if not call.get("ok") and not call.get("blocked_by"):
                    tool_errors += 1

        return ConversationResult(
            scenario_id=scenario.id,
            run=run,
            passed=all(c.passed for c in checks),
            stop_reason=stop,
            end_status=status,
            turns=turns,
            checks=checks,
            persona_closing=closing,
            store_fingerprint=fingerprint,
            agent_usage=agent_usage,
            persona_usage=persona_usage,
            judge_usage=judge_usage,
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            turn_latencies_ms=[_latency(t.trace) for t in turns],
            error=error,
            duration_s=round(time.perf_counter() - started, 2),
        )

    @staticmethod
    def _trace(box: Sandbox, trace_id: str) -> dict[str, object]:
        with box.sessions() as session:
            payload = session.scalar(select(Trace.payload).where(Trace.id == trace_id))
        return dict(payload) if isinstance(payload, dict) else {}


def _latency(trace: dict[str, object]) -> float:
    value = trace.get("latency_ms")
    return float(value) if isinstance(value, int | float) else 0.0
