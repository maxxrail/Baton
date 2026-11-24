"""Console simulation endpoints: list runs, open a report, rerun a scenario."""

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton.api.app import create_app
from baton.config import Settings
from baton.sim.results import SimRun
from baton.sim.runner import RunOptions, run_suite
from tests.runtime.support import REPO

SECRET = "console-test-secret"
OPS = {"Authorization": f"Bearer {SECRET}"}
CI = REPO / "evals" / "sims" / "ci"
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        baton_console_secret=SECRET,
        sim_reports_dir=tmp_path / "reports",
        sim_suite_dir=CI,
    )


def _scripted(settings: Settings, options: RunOptions) -> tuple[SimRun, Path]:
    return run_suite(settings, replace(options, scripted=True), now=NOW)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = _settings(tmp_path)
    run_suite(
        settings,
        RunOptions(suite=CI, scripted=True, filter="ci_human_request,ci_kb_question"),
        now=NOW,
    )
    app = create_app(settings)
    app.state.sim_runner = _scripted
    return TestClient(app)


def test_runs_are_operator_only(client: TestClient) -> None:
    assert client.get("/ops/sims").status_code == 401
    assert client.post("/ops/sims/rerun", json={"scenario_id": "ci_kb_question"}).status_code == 401


def test_list_and_open_a_run(client: TestClient) -> None:
    runs = client.get("/ops/sims", headers=OPS).json()
    assert len(runs) == 1
    run = runs[0]
    assert run["mode"] == "scripted"
    assert run["totals"]["scenarios"] == 2
    assert "conversations" not in run

    detail = client.get(f"/ops/sims/{run['id']}", headers=OPS).json()
    assert {s["scenario_id"] for s in detail["scenarios"]} == {"ci_human_request", "ci_kb_question"}

    report = client.get(f"/ops/sims/{run['id']}/report", headers=OPS)
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in report.headers["content-security-policy"]
    assert "ci_kb_question" in report.text


@pytest.mark.parametrize("bad", ["sim-x", "..", "sim-20260101-000000", "results.json"])
def test_unknown_or_malformed_run_ids_are_404(client: TestClient, bad: str) -> None:
    assert client.get(f"/ops/sims/{bad}", headers=OPS).status_code == 404
    assert client.get(f"/ops/sims/{bad}/report", headers=OPS).status_code == 404


def _wait(client: TestClient, state: str) -> dict[str, object]:
    for _ in range(200):
        current = client.get("/ops/sims/rerun", headers=OPS).json()
        if current["state"] == state:
            return current
        time.sleep(0.05)
    raise AssertionError(f"rerun never reached {state}: {current}")


def test_rerun_a_scenario_in_the_background(client: TestClient) -> None:
    assert client.get("/ops/sims/rerun", headers=OPS).json()["state"] == "idle"
    resp = client.post(
        "/ops/sims/rerun", json={"scenario_id": "ci_return_inside_window", "runs": 2}, headers=OPS
    )
    assert resp.status_code == 202
    assert resp.json()["state"] == "running"
    assert resp.json()["started_by"] == "api"
    done = _wait(client, "done")
    runs = client.get("/ops/sims", headers=OPS).json()
    assert runs[0]["id"] == done["run_id"]
    assert runs[0]["filter"] == "ci_return_inside_window"
    assert runs[0]["totals"]["conversations"] == 2


def test_rerun_rejects_unknown_scenarios_bad_counts_and_a_second_rerun(
    client: TestClient,
) -> None:
    unknown = client.post("/ops/sims/rerun", json={"scenario_id": "nope"}, headers=OPS)
    assert unknown.status_code == 404
    too_many = client.post(
        "/ops/sims/rerun", json={"scenario_id": "ci_kb_question", "runs": 50}, headers=OPS
    )
    assert too_many.status_code == 422

    release = threading.Event()

    def slow(settings: Settings, options: RunOptions) -> tuple[SimRun, Path]:
        release.wait(5)
        return _scripted(settings, options)

    client.app.state.sim_runner = slow  # type: ignore[attr-defined]
    first = client.post("/ops/sims/rerun", json={"scenario_id": "ci_kb_question"}, headers=OPS)
    assert first.status_code == 202
    second = client.post("/ops/sims/rerun", json={"scenario_id": "ci_kb_question"}, headers=OPS)
    assert second.status_code == 409
    release.set()
    _wait(client, "done")


def test_a_failed_rerun_reports_why(client: TestClient) -> None:
    def broken(settings: Settings, options: RunOptions) -> tuple[SimRun, Path]:
        raise RuntimeError("the store exploded")

    client.app.state.sim_runner = broken  # type: ignore[attr-defined]
    client.post("/ops/sims/rerun", json={"scenario_id": "ci_kb_question"}, headers=OPS)
    failed = _wait(client, "failed")
    assert failed["error"] == "RuntimeError: the store exploded"


def test_live_rerun_without_a_key_is_503(tmp_path: Path) -> None:
    """The default runner uses the real model; tests have no key, so it refuses up front."""
    client = TestClient(create_app(_settings(tmp_path)))
    resp = client.post("/ops/sims/rerun", json={"scenario_id": "ci_kb_question"}, headers=OPS)
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]
    assert client.get("/ops/sims/rerun", headers=OPS).json()["state"] == "idle"


def test_report_csp_pins_the_reports_own_script(client: TestClient) -> None:
    import base64
    import hashlib

    from baton.sim.report import JS

    run_id = client.get("/ops/sims", headers=OPS).json()[0]["id"]
    csp = client.get(f"/ops/sims/{run_id}/report", headers=OPS).headers["content-security-policy"]
    digest = base64.b64encode(hashlib.sha256(JS.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp
    assert "unsafe-inline'; frame" not in csp.split("script-src")[1]
    assert client.get(f"/ops/sims/{run_id}%0A", headers=OPS).status_code == 404
