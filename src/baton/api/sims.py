"""Simulation runs for the console (`/ops/sims/...`, operator-only).

- `GET /ops/sims` the runs in `SIM_REPORTS_DIR`, newest first (summaries, no transcripts)
- `GET /ops/sims/{run_id}` one run's totals and per-scenario summaries
- `GET /ops/sims/{run_id}/report` the run's self-contained `report.html`
- `POST /ops/sims/rerun` `{scenario_id, runs}` re-runs one scenario of the main suite with the
  real model, in the background (one rerun at a time); `GET /ops/sims/rerun` shows its state

Run ids are checked against a strict pattern before they touch the filesystem. The report is
served with a Content-Security-Policy that allows only its own inline style and script.
"""

import base64
import hashlib
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from starlette.datastructures import State

from baton.api.auth import Operator
from baton.config import Settings
from baton.runtime.factory import AgentUnavailableError
from baton.runtime.redact import redact
from baton.sim.report import JS as REPORT_JS
from baton.sim.results import ScenarioSummary, SimRun, Totals
from baton.sim.runner import RunOptions, live_models, load_run, run_suite
from baton.sim.suite import SuiteError, load_suite

log = structlog.get_logger()
router = APIRouter(prefix="/ops/sims", tags=["simulations"])

RUN_ID = re.compile(r"sim-\d{8}-\d{6}(-\d+)?")
MAX_RUNS_LISTED = 50
# Only the report's own inline script may run (pinned by hash), so an escaping slip in a
# transcript can't execute on the API origin with the operator's cookie.
_JS_HASH = base64.b64encode(hashlib.sha256(REPORT_JS.encode("utf-8")).digest()).decode()
REPORT_CSP = (
    f"default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{_JS_HASH}'; "
    "frame-ancestors 'self'"
)

SuiteRunner = Callable[[Settings, RunOptions], tuple[SimRun, Path]]


class RunSummary(BaseModel):
    id: str
    created_at: datetime
    mode: str
    suite: str
    filter: str | None
    models: dict[str, str]
    totals: Totals


class RunDetail(RunSummary):
    scenarios: list[ScenarioSummary]
    git_sha: str | None
    aop_bundle_hash: str


class RerunIn(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=100)
    runs: int = Field(default=3, ge=1, le=5)


class RerunState(BaseModel):
    state: Literal["idle", "running", "done", "failed"] = "idle"
    scenario_id: str | None = None
    started_by: str | None = None
    started_at: datetime | None = None
    run_id: str | None = None
    error: str | None = None


class _Reruns:
    """The one background rerun the console may have going."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current = RerunState()


def _reports_dir(request: Request) -> Path:
    settings: Settings = request.app.state.settings
    return settings.sim_reports_dir


def _run_dir(request: Request, run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="no such run")
    path = _reports_dir(request) / run_id
    if not (path / "results.json").is_file():
        raise HTTPException(status_code=404, detail="no such run")
    return path


def _summary(run: SimRun) -> RunSummary:
    return RunSummary.model_validate(run.model_dump(exclude={"conversations", "scenarios"}))


@router.get("")
def list_runs(request: Request, _operator: Operator) -> list[RunSummary]:
    root = _reports_dir(request)
    if not root.is_dir():
        return []
    dirs = sorted(
        (d for d in root.iterdir() if RUN_ID.fullmatch(d.name) and (d / "results.json").is_file()),
        key=lambda d: d.name,
        reverse=True,
    )
    out: list[RunSummary] = []
    for d in dirs[:MAX_RUNS_LISTED]:
        try:
            out.append(_summary(load_run(d, summary=True)))
        except ValueError:
            continue  # a half-written or foreign results.json: skip it, don't break the list
    return out


@router.get("/rerun")
def rerun_state(request: Request, _operator: Operator) -> RerunState:
    reruns: _Reruns = request.app.state.sim_reruns
    with reruns.lock:
        return reruns.current.model_copy()


@router.post("/rerun", status_code=202)
def rerun(body: RerunIn, request: Request, operator: Operator) -> RerunState:
    settings: Settings = request.app.state.settings
    suite = settings.sim_suite_dir
    try:
        ids = {sc.id for sc in load_suite(suite)}
    except SuiteError as exc:
        raise HTTPException(status_code=500, detail=f"the suite doesn't load: {exc}") from None
    if body.scenario_id not in ids:
        raise HTTPException(status_code=404, detail="unknown scenario")
    runner: SuiteRunner = request.app.state.sim_runner
    if runner is run_suite_live:
        try:
            live_models(settings)  # fail now with a clear 503, not later in the thread
        except AgentUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    reruns: _Reruns = request.app.state.sim_reruns
    with reruns.lock:
        if reruns.current.state == "running":
            raise HTTPException(status_code=409, detail="a rerun is already running")
        reruns.current = RerunState(
            state="running",
            scenario_id=body.scenario_id,
            started_by=operator,
            started_at=datetime.now(UTC),
        )
        started = reruns.current.model_copy()
    options = RunOptions(suite=suite, filter=body.scenario_id, runs=body.runs)

    def work() -> None:
        try:
            run, _ = runner(settings, options)
            update = {"state": "done", "run_id": run.id}
        except Exception as exc:
            reason = f"{type(exc).__name__}: {redact(str(exc))[:300]}"
            log.error("sim_rerun_failed", scenario=body.scenario_id, error=reason)
            update = {"state": "failed", "error": reason}
        with reruns.lock:
            reruns.current = reruns.current.model_copy(update=update)

    threading.Thread(target=work, name="sim-rerun", daemon=True).start()
    return started


@router.get("/{run_id}")
def get_run(run_id: str, request: Request, _operator: Operator) -> RunDetail:
    run = load_run(_run_dir(request, run_id), summary=True)
    return RunDetail.model_validate(run.model_dump(exclude={"conversations"}))


@router.get("/{run_id}/report", response_class=HTMLResponse)
def get_report(run_id: str, request: Request, _operator: Operator) -> HTMLResponse:
    path = _run_dir(request, run_id) / "report.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="this run has no report")
    return HTMLResponse(
        path.read_text(encoding="utf-8"),
        headers={"Content-Security-Policy": REPORT_CSP, "X-Content-Type-Options": "nosniff"},
    )


def run_suite_live(settings: Settings, options: RunOptions) -> tuple[SimRun, Path]:
    """The default rerun: the real models (the console never runs scripted suites)."""
    return run_suite(settings, options)


def install(state: State) -> None:
    """Set the rerun state and the default runner on `app.state` (tests swap the runner)."""
    state.sim_reruns = _Reruns()
    state.sim_runner = run_suite_live
