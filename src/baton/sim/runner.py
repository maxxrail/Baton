"""Wire a simulation run together: AOPs, help center, models, simulator, results on disk.

`run_suite` is what `baton-sim run` and the console's "rerun" call. Live runs use the real
model for the agent, the persona and the judge (`BATON_MODEL`, or `BATON_SIM_PERSONA_MODEL` /
`BATON_SIM_JUDGE_MODEL`); scripted runs need no key at all.
"""

import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine

from baton.aop.compiler import compile_aops
from baton.aop.models import AOPBundle
from baton.config import Settings
from baton.db import Base, make_engine, make_session_factory
from baton.knowledge.base import KnowledgeBase
from baton.knowledge.ingest import ingest
from baton.llm import AnthropicClient, LLMClient, get_embedder
from baton.prompts import load_prompt
from baton.runtime.factory import AgentUnavailableError
from baton.runtime.guardrails import GuardrailConfig
from baton.runtime.redact import redact
from baton.sim.judge import JUDGE_PROMPT
from baton.sim.metrics import summarize
from baton.sim.persona import PERSONA_PROMPT
from baton.sim.report import write_report
from baton.sim.results import ConversationResult, SimRun
from baton.sim.scripted import ScriptedAgentLLM, scripted_judge, scripted_persona
from baton.sim.simulator import ModelsFor, Progress, SimModels, Simulator
from baton.sim.suite import Scenario, SuiteError, load_suite, validate_suite
from baton.tools.registry import default_registry


@dataclass(frozen=True)
class RunOptions:
    suite: Path
    filter: str | None = None
    runs: int | None = None
    scripted: bool = False
    concurrency: int = 4
    out_dir: Path | None = None  # default: settings.sim_reports_dir
    aops_dir: Path | None = None  # default: settings.aops_dir (tests point it at a broken copy)


def select(scenarios: list[Scenario], needle: str | None) -> list[Scenario]:
    if not needle:
        return scenarios
    needles = [n.strip() for n in needle.split(",") if n.strip()]
    return [sc for sc in scenarios if any(sc.matches(n) for n in needles)]


def load_checked(suite: Path, bundle: AOPBundle, *, require_script: bool = False) -> list[Scenario]:
    scenarios = load_suite(suite)
    problems = validate_suite(scenarios, bundle, default_registry(), require_script=require_script)
    if problems:
        raise SuiteError(problems)
    return scenarios


def build_knowledge(settings: Settings, workdir: Path) -> tuple[KnowledgeBase, Engine]:
    """Index the help center into a private database for this run (fast with hashing)."""
    engine = make_engine(f"sqlite:///{(workdir / 'kb.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    embedder = get_embedder(settings)
    with sessions() as session:
        ingest(session, settings.kb_dir, embedder)
        session.commit()
    return KnowledgeBase(sessions, embedder, base_url=settings.baton_kb_base_url), engine


def live_models(settings: Settings) -> tuple[ModelsFor, dict[str, str]]:
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if settings.baton_llm == "fake" or not key:
        raise AgentUnavailableError(
            "a live simulation needs ANTHROPIC_API_KEY (or run the offline suite with --scripted)"
        )
    names = {
        "agent": settings.baton_model,
        "persona": settings.baton_sim_persona_model or settings.baton_model,
        "judge": settings.baton_sim_judge_model or settings.baton_model,
    }
    clients: dict[str, LLMClient] = {}
    for role, model in names.items():
        clients[role] = clients.get(model) or AnthropicClient(
            model=model, api_key=key, fallbacks=settings.baton_llm_fallbacks
        )
        clients.setdefault(model, clients[role])
    models = SimModels(agent=clients["agent"], persona=clients["persona"], judge=clients["judge"])
    return (lambda _sc: models), names


def scripted_models(scenario: Scenario) -> SimModels:
    agent = ScriptedAgentLLM(scenario)
    return SimModels(
        agent=agent,
        persona=scripted_persona(scenario),
        judge=scripted_judge(scenario),
        before_turn=agent.before_turn,
        finish=agent.finish,
    )


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def run_suite(
    settings: Settings,
    options: RunOptions,
    progress: Progress | None = None,
    *,
    now: datetime | None = None,
    models_for: ModelsFor | None = None,
) -> tuple[SimRun, Path]:
    """Run the (filtered) suite and write `results.json` + `report.html`. Returns both."""
    bundle = compile_aops(options.aops_dir or settings.aops_dir, prompts_dir=settings.prompts_dir)
    scenarios = select(
        load_checked(options.suite, bundle, require_script=options.scripted), options.filter
    )
    if not scenarios:
        raise SuiteError([f"no scenarios in {options.suite} match {options.filter!r}"])
    if models_for is not None:
        names = {"agent": "injected", "persona": "injected", "judge": "injected"}
    elif options.scripted:
        models_for, names = (
            scripted_models,
            dict.fromkeys(("agent", "persona", "judge"), "scripted"),
        )
    else:
        models_for, names = live_models(settings)

    started = time.perf_counter()
    created = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="baton-kb-", ignore_cleanup_errors=True) as tmp:
        knowledge, kb_engine = build_knowledge(settings, Path(tmp))
        sim = Simulator(
            bundle,
            models_for,
            knowledge=knowledge,
            prompts_dir=settings.prompts_dir,
            config=GuardrailConfig(refund_auto_limit_cents=settings.baton_refund_auto_limit_cents),
            concurrency=options.concurrency,
            now=now,
            progress=progress,
        )
        results: list[ConversationResult] = sim.run(scenarios, options.runs)
        kb_engine.dispose()

    summaries, totals = summarize(scenarios, results)
    run = SimRun(
        id=f"sim-{created.strftime('%Y%m%d-%H%M%S')}",
        created_at=created,
        mode="scripted" if options.scripted else "live",
        suite=options.suite.as_posix(),
        filter=options.filter,
        runs_per_scenario=options.runs,
        models=names,
        prompt_versions={
            JUDGE_PROMPT: load_prompt(JUDGE_PROMPT, settings.prompts_dir).version,
            PERSONA_PROMPT: load_prompt(PERSONA_PROMPT, settings.prompts_dir).version,
        },
        aop_bundle_hash=bundle.bundle_hash,
        git_sha=git_sha(),
        totals=totals,
        scenarios=summaries,
        conversations=results,
        duration_s=round(time.perf_counter() - started, 1),
    )
    return write_run(run, options.out_dir or settings.sim_reports_dir)


def redact_run(run: SimRun) -> SimRun:
    """A copy safe to write to disk or upload: customer and agent text, closing lines,
    rationales and errors go through `redact` (traces were redacted when they were stored).
    The judge saw the raw text; only the files are masked."""
    conversations = []
    for r in run.conversations:
        turns = [
            t.model_copy(update={"customer": redact(t.customer), "agent": redact(t.agent)})
            for t in r.turns
        ]
        checks = [c.model_copy(update={"rationale": redact(c.rationale)}) for c in r.checks]
        conversations.append(
            r.model_copy(
                update={
                    "turns": turns,
                    "checks": checks,
                    "persona_closing": redact(r.persona_closing),
                    "error": redact(r.error) if r.error else None,
                }
            )
        )
    return run.model_copy(update={"conversations": conversations})


def write_run(run: SimRun, reports_dir: Path) -> tuple[SimRun, Path]:
    """`<reports_dir>/<run id>/` with `results.json` (everything), `summary.json` (no
    transcripts) and `report.html`, all redacted. A clash gets a zero-padded suffix (`-02`)
    so ids still sort by time. Returns the run as written (its id may have changed)."""
    out = reports_dir / run.id
    n = 1
    while out.exists():
        n += 1
        out = reports_dir / f"{run.id}-{n:02d}"
    run = redact_run(run.model_copy(update={"id": out.name}))
    out.mkdir(parents=True)
    (out / "results.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
    summary = run.model_dump_json(indent=2, exclude={"conversations"})
    (out / "summary.json").write_text(summary, encoding="utf-8")
    write_report(run, out / "report.html")
    return run, out


def load_run(path: Path, *, summary: bool = False) -> SimRun:
    """A run from its directory or a JSON file. `summary=True` reads `summary.json` when it
    exists (no conversations), which is much smaller."""
    file = path
    if path.is_dir():
        file = path / "summary.json" if summary else path / "results.json"
        if not file.is_file():
            file = path / "results.json"
    return SimRun.model_validate_json(file.read_text(encoding="utf-8"))
