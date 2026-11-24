"""`report.html`: one self-contained file (inline CSS and a few lines of JS, no network) that
opens offline from disk.

Sections: the headline metrics, a table of scenarios (pass rate, pass^k, turns, escalations,
latency, cost, what failed), and a drill-down per scenario with every run's checks (rationale,
evidence turns linked to the transcript), the full transcript with the agent's tool calls, and
each turn's full trace (linked from the transcript). Everything rendered is escaped.
"""

import json
from html import escape
from pathlib import Path

from baton.sim.judge import action_lines
from baton.sim.results import ConversationResult, ScenarioSummary, SimRun

CSS = Path(__file__).with_name("report.css").read_text(encoding="utf-8")

JS = """
document.getElementById('only-failing').addEventListener('change', function (e) {
  document.querySelectorAll('[data-pass]').forEach(function (el) {
    el.style.display = e.target.checked && el.dataset.pass === '1' ? 'none' : '';
  });
});
"""


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _ok(passed: bool, yes: str = "pass", no: str = "fail") -> str:
    return f'<span class="pill {"pass" if passed else "fail"}">{yes if passed else no}</span>'


def _key(r: ConversationResult) -> str:
    return f"{r.scenario_id}-r{r.run}"


def _stats(run: SimRun) -> str:
    t = run.totals
    items = [
        ("pass^k (scenarios)", _pct(t.pass_k_rate)),
        ("pass rate (runs)", _pct(t.pass_rate)),
        ("scenarios / conversations", f"{t.scenarios} / {t.conversations}"),
        ("mean turns", f"{t.mean_turns:.1f}"),
        ("escalation rate", _pct(t.escalation_rate)),
        ("tool error rate", _pct(t.tool_error_rate)),
        (
            "p50 / p95 turn latency",
            f"{t.p50_latency_ms / 1000:.1f}s / {t.p95_latency_ms / 1000:.1f}s",
        ),
        ("agent tokens / conversation", f"{t.mean_tokens:,.0f}"),
        ("agent cost / conversation", f"${t.mean_cost_usd:.4f}"),
        ("whole run cost", f"${t.sim_cost_usd:.2f}"),
    ]
    cells = "".join(
        f'<div class="stat"><span class="muted">{escape(k)}</span><b>{escape(v)}</b></div>'
        for k, v in items
    )
    return f'<div class="grid">{cells}</div>'


def _scenario_row(s: ScenarioSummary) -> str:
    failed = "<br>".join(
        f"{escape(name)} <span class='muted'>({n}/{s.runs})</span>"
        for name, n in s.failed_criteria.items()
    )
    tags = "".join(f'<span class="tag">{escape(t)}</span>' for t in s.tags)
    return (
        f'<tr data-pass="{int(s.pass_k)}"><td><a href="#sc-{escape(s.scenario_id)}">'
        f"{escape(s.scenario_id)}</a><br>{tags}</td><td>{escape(s.aop)}</td>"
        f"<td>{s.passed_runs}/{s.runs}</td><td>{_ok(s.pass_k)}</td><td>{s.mean_turns:.1f}</td>"
        f"<td>{_pct(s.escalation_rate)}</td><td>{s.p50_latency_ms / 1000:.1f}s / "
        f"{s.p95_latency_ms / 1000:.1f}s</td><td>${s.mean_cost_usd:.4f}</td><td>{failed}</td></tr>"
    )


def _checks(r: ConversationResult) -> str:
    rows = []
    for c in r.checks:
        turns = ", ".join(f'<a href="#t-{escape(_key(r))}-{n}">{n}</a>' for n in c.evidence_turns)
        rows.append(
            f"<tr><td>{_ok(c.passed)}</td><td>{escape(c.kind)}</td><td>{escape(c.criterion)}</td>"
            f"<td>{escape(c.graded_by)}</td><td>{escape(c.rationale)}</td><td>{turns}</td></tr>"
        )
    head = (
        "<tr><th></th><th>kind</th><th>criterion</th><th>graded by</th><th>rationale</th>"
        "<th>turns</th></tr>"
    )
    return f'<div class="wrap"><table>{head}{"".join(rows)}</table></div>'


def _transcript(r: ConversationResult) -> str:
    parts = []
    for t in r.turns:
        anchor = f"t-{_key(r)}-{t.index}"
        trace_anchor = f"trace-{_key(r)}-{t.index}"
        actions = action_lines(t.trace)
        acts = (
            '<ul class="actions">' + "".join(f"<li>{escape(a)}</li>" for a in actions) + "</ul>"
            if actions
            else ""
        )
        aop = t.trace.get("aop_ref") or "-"
        step = f"{aop} step {t.trace.get('step_before')}→{t.trace.get('step_after')}"
        agent = (
            escape(t.agent) if t.replied else "<i>(silent: a teammate owns the conversation)</i>"
        )
        parts.append(
            f'<div class="turn c" id="{escape(anchor)}">'
            f'<div class="who">turn {t.index} · customer</div>{escape(t.customer)}</div>'
            f'<div class="turn a"><div class="who">agent · {escape(step)} · '
            f"status {escape(t.status)} · "
            f'<a href="#{escape(trace_anchor)}">trace {escape(t.trace_id[:8])}</a></div>{acts}'
            f'<div style="white-space:pre-wrap">{agent}</div></div>'
        )
    if r.persona_closing:
        parts.append(
            '<div class="turn c"><div class="who">customer (closing, not sent)</div>'
            f"{escape(r.persona_closing)}</div>"
        )
    return "".join(parts)


def _traces(r: ConversationResult) -> str:
    out = []
    for t in r.turns:
        body = escape(json.dumps(t.trace, indent=2, sort_keys=True, default=str))
        out.append(
            f'<details id="trace-{escape(_key(r))}-{t.index}"><summary>trace · turn {t.index} · '
            f"{escape(t.trace_id)}</summary><pre>{body}</pre></details>"
        )
    return "".join(out)


def _run_block(r: ConversationResult) -> str:
    meta = (
        f"stopped: {escape(r.stop_reason)} · ended {escape(r.end_status)} · {len(r.turns)} turns · "
        f"{r.tool_calls} tool calls ({r.tool_errors} errors) · "
        f"agent ${r.agent_usage.cost_usd:.4f} · "
        f"store start {escape(r.store_fingerprint)} · {r.duration_s:.1f}s"
    )
    error = f'<p class="fail">{escape(r.error)}</p>' if r.error else ""
    return (
        f'<details data-pass="{int(r.passed)}"{"" if r.passed else " open"}>'
        f"<summary>run {r.run} {_ok(r.passed)}</summary><p class='muted'>{meta}</p>{error}"
        f"<h3>Checks</h3>{_checks(r)}<h3>Transcript</h3>{_transcript(r)}"
        f"<h3>Traces</h3>{_traces(r)}</details>"
    )


def render_report(run: SimRun) -> str:
    by_scenario: dict[str, list[ConversationResult]] = {}
    for r in run.conversations:
        by_scenario.setdefault(r.scenario_id, []).append(r)
    head = (
        "<tr><th>scenario</th><th>aop</th><th>passed</th><th>pass^k</th><th>turns</th>"
        "<th>escalated</th><th>p50 / p95</th><th>cost</th><th>failed checks</th></tr>"
    )
    rows = "".join(_scenario_row(s) for s in run.scenarios)
    drill = []
    for s in run.scenarios:
        runs = "".join(
            _run_block(r) for r in sorted(by_scenario.get(s.scenario_id, []), key=lambda r: r.run)
        )
        desc = f"<p class='muted'>{escape(s.description)}</p>" if s.description else ""
        drill.append(
            f'<section id="sc-{escape(s.scenario_id)}" data-pass="{int(s.pass_k)}">'
            f"<h2>{escape(s.scenario_id)} {_ok(s.pass_k, 'pass^k', 'fails pass^k')}</h2>"
            f"{desc}{runs}</section>"
        )
    models = ", ".join(f"{k}: {v}" for k, v in run.models.items())
    prompts = ", ".join(run.prompt_versions.values())
    meta = (
        f"{escape(run.created_at.strftime('%Y-%m-%d %H:%M UTC'))} · {escape(run.mode)} · suite "
        f"{escape(run.suite)}{' · filter ' + escape(run.filter) if run.filter else ''} · "
        f"{escape(models)} · {escape(prompts)} · AOPs {escape(run.aop_bundle_hash[:12])}"
        f"{' · git ' + escape(run.git_sha) if run.git_sha else ''} · {run.duration_s:.0f}s"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Baton simulations {escape(run.id)}</title><style>{CSS}</style></head>
<body><main>
<h1>Baton simulations · {escape(run.id)}</h1><p class="muted">{meta}</p>
{_stats(run)}
<p><label><input type="checkbox" id="only-failing"> show failing only</label></p>
<h2>Scenarios</h2><div class="wrap"><table>{head}{rows}</table></div>
{"".join(drill)}
</main><script>{JS}</script></body></html>
"""


def write_report(run: SimRun, path: Path) -> None:
    path.write_text(render_report(run), encoding="utf-8")
