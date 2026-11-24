// Simulation runs: the list of runs (newest first), one run's scenarios with what failed, a
// link to its self-contained HTML report, and "rerun" for a scenario (real model, one at a
// time; the API refuses with a reason if no key is configured).

import { useCallback, useEffect, useState } from "react";
import { api, post, type RerunState, type SimRunDetail, type SimRunSummary } from "../lib/api";

const pct = (x: number) => `${(x * 100).toFixed(1)}%`;
const secs = (ms: number) => `${(ms / 1000).toFixed(1)}s`;

function PassK({ ok }: { ok: boolean }) {
  return (
    <span className={`rounded px-1.5 text-xs font-medium ${ok ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-800"}`}>
      {ok ? "pass^k" : "fails"}
    </span>
  );
}

export function RunList({
  runs,
  selected,
  onSelect,
}: {
  runs: SimRunSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  if (!runs.length)
    return (
      <p className="p-3 text-sm text-stone-500">
        No runs yet. Run <code>uv run baton-sim run</code> (or <code>--suite evals/sims/ci --scripted</code> offline).
      </p>
    );
  return (
    <ul className="divide-y divide-stone-100" data-testid="sim-runs">
      {runs.map((r) => (
        <li key={r.id}>
          <button
            type="button"
            onClick={() => onSelect(r.id)}
            className={`w-full px-3 py-2 text-left text-sm ${selected === r.id ? "bg-nw-50" : "hover:bg-stone-50"}`}
          >
            <div className="flex items-center justify-between">
              <span className="font-mono text-xs">{r.id}</span>
              <span className="text-xs text-stone-500">{r.mode}</span>
            </div>
            <div className="text-xs text-stone-600">
              pass^k {pct(r.totals.pass_k_rate)} · pass {pct(r.totals.pass_rate)} · {r.totals.conversations} convos
              {r.filter ? ` · ${r.filter}` : ""}
            </div>
          </button>
        </li>
      ))}
    </ul>
  );
}

export function RunDetail({
  run,
  rerun,
  onRerun,
}: {
  run: SimRunDetail;
  rerun: RerunState;
  onRerun: (scenarioId: string) => void;
}) {
  const t = run.totals;
  const busy = rerun.state === "running";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="font-mono text-sm font-semibold">{run.id}</h2>
        <span className="text-xs text-stone-500">
          {run.mode} · {Object.entries(run.models).map(([k, v]) => `${k}: ${v}`).join(", ")}
          {run.git_sha ? ` · git ${run.git_sha}` : ""}
        </span>
        <a href={`/ops/sims/${run.id}/report`} target="_blank" rel="noreferrer" className="ml-auto text-sm text-nw-700 underline">
          Open report
        </a>
      </div>
      <dl className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4" data-testid="sim-totals">
        {[
          ["pass^k", pct(t.pass_k_rate)],
          ["pass rate", pct(t.pass_rate)],
          ["escalated", pct(t.escalation_rate)],
          ["p50 / p95", `${secs(t.p50_latency_ms)} / ${secs(t.p95_latency_ms)}`],
          ["mean turns", t.mean_turns.toFixed(1)],
          ["tool errors", pct(t.tool_error_rate)],
          ["cost / convo", `$${t.mean_cost_usd.toFixed(4)}`],
          ["run cost", `$${t.sim_cost_usd.toFixed(2)}`],
        ].map(([k, v]) => (
          <div key={k} className="rounded border border-stone-200 bg-white p-2">
            <dt className="text-xs text-stone-500">{k}</dt>
            <dd className="font-semibold">{v}</dd>
          </div>
        ))}
      </dl>
      {rerun.state !== "idle" && (
        <p className="text-xs text-stone-600" data-testid="rerun-state">
          Rerun of {rerun.scenario_id}: {rerun.state}
          {rerun.run_id ? ` → ${rerun.run_id}` : ""}
          {rerun.error ? ` (${rerun.error})` : ""}
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full bg-white text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-stone-500">
              <th className="p-2">scenario</th>
              <th className="p-2">passed</th>
              <th className="p-2" />
              <th className="p-2">turns</th>
              <th className="p-2">failed checks</th>
              <th className="p-2" />
            </tr>
          </thead>
          <tbody>
            {run.scenarios.map((s) => (
              <tr key={s.scenario_id} className="border-b align-top" data-testid="sim-scenario">
                <td className="p-2">
                  <div className="font-mono text-xs">{s.scenario_id}</div>
                  <div className="text-xs text-stone-500">
                    {s.aop} · {s.tags.join(", ")}
                  </div>
                </td>
                <td className="p-2">
                  {s.passed_runs}/{s.runs}
                </td>
                <td className="p-2">
                  <PassK ok={s.pass_k} />
                </td>
                <td className="p-2">{s.mean_turns.toFixed(1)}</td>
                <td className="p-2 text-xs">
                  {Object.entries(s.failed_criteria).map(([name, n]) => (
                    <div key={name}>
                      {name} <span className="text-stone-500">({n}/{s.runs})</span>
                    </div>
                  ))}
                </td>
                <td className="p-2">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onRerun(s.scenario_id)}
                    className="rounded border px-2 py-0.5 text-xs disabled:opacity-40"
                  >
                    Rerun
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function Simulations() {
  const [runs, setRuns] = useState<SimRunSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SimRunDetail | null>(null);
  const [rerun, setRerun] = useState<RerunState>({ state: "idle", scenario_id: null, started_by: null, started_at: null, run_id: null, error: null });
  const [error, setError] = useState<string | null>(null);

  const loadRuns = useCallback(async () => {
    const list = await api<SimRunSummary[]>("/ops/sims");
    setRuns(list);
    setSelected((s) => s ?? list[0]?.id ?? null);
  }, []);

  useEffect(() => {
    loadRuns().catch((e: Error) => setError(e.message));
    api<RerunState>("/ops/sims/rerun").then(setRerun, () => undefined);
  }, [loadRuns]);

  useEffect(() => {
    if (!selected) return;
    api<SimRunDetail>(`/ops/sims/${selected}`).then(setDetail, (e: Error) => setError(e.message));
  }, [selected]);

  // Poll while a rerun is going; when it finishes, refresh the list and open the new run.
  useEffect(() => {
    if (rerun.state !== "running") return;
    const timer = window.setInterval(async () => {
      try {
        const state = await api<RerunState>("/ops/sims/rerun");
        setRerun(state);
        if (state.state !== "running") {
          await loadRuns();
          if (state.run_id) setSelected(state.run_id);
        }
      } catch (e) {
        // Keep polling: a dropped request shouldn't strand the page on "running".
        setError(e instanceof Error ? e.message : String(e));
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [rerun.state, loadRuns]);

  const start = async (scenarioId: string) => {
    setError(null);
    try {
      setRerun(await post<RerunState>("/ops/sims/rerun", { scenario_id: scenarioId, runs: 3 }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="grid h-[calc(100vh-56px)] grid-cols-[300px_1fr]">
      <aside className="overflow-y-auto border-r border-stone-200 bg-white">
        <RunList runs={runs} selected={selected} onSelect={setSelected} />
      </aside>
      <main className="overflow-y-auto p-4">
        {error && <p className="mb-2 text-sm text-red-700">{error}</p>}
        {detail ? <RunDetail run={detail} rerun={rerun} onRerun={start} /> : <p className="text-sm text-stone-500">Select a run.</p>}
      </main>
    </div>
  );
}
