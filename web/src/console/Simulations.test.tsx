import { fireEvent, render, screen, within } from "@testing-library/react";
import type { RerunState, SimRunDetail } from "../lib/api";
import { tabFromPath } from "./Console";
import { RunDetail, RunList } from "./Simulations";

const totals = {
  scenarios: 2,
  conversations: 6,
  pass_rate: 0.8333,
  pass_k_rate: 0.5,
  mean_turns: 4.2,
  tool_error_rate: 0.02,
  escalation_rate: 0.33,
  p50_latency_ms: 4200,
  p95_latency_ms: 9100,
  mean_tokens: 21000,
  mean_cost_usd: 0.0512,
  sim_cost_usd: 0.61,
};

const run: SimRunDetail = {
  id: "sim-20261124-101500",
  created_at: "2026-11-24T10:15:00Z",
  mode: "live",
  suite: "evals/sims",
  filter: null,
  models: { agent: "claude-sonnet-5-5" },
  git_sha: "abc1234",
  aop_bundle_hash: "f00",
  totals,
  scenarios: [
    {
      scenario_id: "return_outside_window",
      aop: "return_item",
      tags: ["edge", "pressure"],
      description: null,
      runs: 3,
      passed_runs: 2,
      pass_rate: 0.6667,
      pass_k: false,
      mean_turns: 5,
      escalation_rate: 0.67,
      p50_latency_ms: 4000,
      p95_latency_ms: 8000,
      mean_cost_usd: 0.05,
      failed_criteria: { "must_not: promises a refund": 1 },
    },
    {
      scenario_id: "kb_return_window",
      aop: "general_question",
      tags: ["kb"],
      description: null,
      runs: 3,
      passed_runs: 3,
      pass_rate: 1,
      pass_k: true,
      mean_turns: 2,
      escalation_rate: 0,
      p50_latency_ms: 3000,
      p95_latency_ms: 5000,
      mean_cost_usd: 0.02,
      failed_criteria: {},
    },
  ],
};

const idle: RerunState = { state: "idle", scenario_id: null, started_by: null, started_at: null, run_id: null, error: null };

describe("Simulations", () => {
  it("shows totals, pass^k per scenario, what failed, and links the report", () => {
    render(<RunDetail run={run} rerun={idle} onRerun={() => undefined} />);
    expect(within(screen.getByTestId("sim-totals")).getByText("50.0%")).toBeTruthy();
    const [flaky, steady] = screen.getAllByTestId("sim-scenario");
    expect(within(flaky).getByText("fails")).toBeTruthy();
    expect(within(flaky).getByText("2/3")).toBeTruthy();
    expect(flaky.textContent).toContain("must_not: promises a refund");
    expect(within(steady).getByText("pass^k")).toBeTruthy();
    const link = screen.getByText("Open report") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/ops/sims/sim-20261124-101500/report");
    expect(link.getAttribute("rel")).toBe("noreferrer");
  });

  it("reruns a scenario, and disables rerun while one is going", () => {
    const onRerun = vi.fn();
    const { rerender } = render(<RunDetail run={run} rerun={idle} onRerun={onRerun} />);
    fireEvent.click(within(screen.getAllByTestId("sim-scenario")[0]).getByText("Rerun"));
    expect(onRerun).toHaveBeenCalledWith("return_outside_window");

    const running: RerunState = { ...idle, state: "running", scenario_id: "return_outside_window" };
    rerender(<RunDetail run={run} rerun={running} onRerun={onRerun} />);
    expect(screen.getByTestId("rerun-state").textContent).toContain("return_outside_window: running");
    for (const button of screen.getAllByText("Rerun")) expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("lists runs and explains how to make one when there are none", () => {
    const onSelect = vi.fn();
    const { rerender } = render(<RunList runs={[]} selected={null} onSelect={onSelect} />);
    expect(screen.getByText(/No runs yet/)).toBeTruthy();
    rerender(<RunList runs={[run]} selected={null} onSelect={onSelect} />);
    fireEvent.click(screen.getByText("sim-20261124-101500"));
    expect(onSelect).toHaveBeenCalledWith("sim-20261124-101500");
    expect(screen.getByTestId("sim-runs").textContent).toContain("pass^k 50.0%");
  });

  it("opens the right tab from the URL", () => {
    expect(tabFromPath("/console/simulations")).toBe("simulations");
    expect(tabFromPath("/console/aops")).toBe("aops");
    expect(tabFromPath("/console")).toBe("inbox");
    expect(tabFromPath("/console/nonsense")).toBe("inbox");
  });
});
