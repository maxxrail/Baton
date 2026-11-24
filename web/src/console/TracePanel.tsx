// Per-turn traces: which procedure and step ran, every model call, every tool call (args,
// results, which guardrail blocked it), every guardrail decision, grounding, latency, tokens.
// This is the "why did the agent say that?" view.

import { useState } from "react";
import type { GuardrailDecision, ToolCallTrace, Trace } from "../lib/api";

const ACTION_STYLE: Record<GuardrailDecision["action"], string> = {
  allow: "bg-stone-100 text-stone-600",
  block: "bg-red-100 text-red-800",
  rewrite: "bg-violet-100 text-violet-800",
  escalate: "bg-amber-100 text-amber-900",
};

function Json({ value }: { value: unknown }) {
  return (
    <pre className="max-h-48 overflow-auto rounded bg-stone-900 p-2 text-[11px] leading-snug text-stone-100">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function ToolCall({ call }: { call: ToolCallTrace }) {
  const state = call.blocked_by ? "blocked" : call.ok ? "ok" : "error";
  const badge =
    state === "blocked"
      ? "bg-red-100 text-red-800"
      : state === "ok"
        ? "bg-emerald-100 text-emerald-800"
        : "bg-orange-100 text-orange-800";
  return (
    <li className="rounded border border-stone-200 bg-white p-2" data-testid="tool-call">
      <details>
        <summary className="flex cursor-pointer flex-wrap items-center gap-2 text-xs">
          <code className="font-semibold">{call.name}</code>
          <span className={`rounded px-1.5 py-0.5 ${badge}`}>
            {state === "blocked" ? `blocked by ${call.blocked_by}` : state === "error" ? call.error_code : "ok"}
          </span>
          {call.source === "guardrail" && (
            <span className="rounded bg-sky-100 px-1.5 py-0.5 text-sky-800">guardrail check</span>
          )}
          <span className="ml-auto text-stone-400">{call.latency_ms} ms</span>
        </summary>
        <div className="mt-2 space-y-1">
          <div className="text-[11px] font-semibold text-stone-500">args</div>
          <Json value={call.args} />
          {call.result && (
            <>
              <div className="text-[11px] font-semibold text-stone-500">result</div>
              <Json value={call.result} />
            </>
          )}
        </div>
      </details>
    </li>
  );
}

export function TraceTurn({ trace }: { trace: Trace }) {
  const t = trace.payload;
  const [showAllowed, setShowAllowed] = useState(false);
  const flagged = t.guardrails.filter((g) => g.action !== "allow");
  const allowed = t.guardrails.length - flagged.length;
  const guardrails = showAllowed ? t.guardrails : flagged;
  return (
    <article className="space-y-2 rounded-lg border border-stone-200 bg-stone-50 p-3" data-testid="trace-turn">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="font-semibold text-stone-800">Turn {t.turn}</span>
        <span className="font-mono text-nw-700">{t.aop_ref ?? "no procedure"}</span>
        <span>
          step {t.step_before ?? "–"} → {t.step_after ?? "–"}
        </span>
        <span className="rounded bg-stone-200 px-1.5">{t.status}</span>
        <span className="rounded bg-stone-200 px-1.5">reply: {t.reply_source}</span>
        <span className="ml-auto text-stone-500">
          {Math.round(t.latency_ms)} ms · {t.input_tokens} in / {t.output_tokens} out
        </span>
      </header>

      {t.escalation_reason && (
        <p className="rounded bg-amber-50 px-2 py-1 text-xs text-amber-900">Escalated: {t.escalation_reason}</p>
      )}
      {t.error && <p className="rounded bg-red-50 px-2 py-1 text-xs text-red-800">Error: {t.error}</p>}

      {t.route && (
        <p className="text-xs text-stone-600">
          Routed to <code>{t.route.aop_id ?? "none"}</code>
          {t.route.rationale ? ` (${t.route.rationale})` : ""}
        </p>
      )}

      <section>
        <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-stone-500">
          Tool calls ({t.tool_calls.length})
        </h4>
        {t.tool_calls.length ? (
          <ul className="space-y-1">
            {t.tool_calls.map((c, i) => (
              <ToolCall key={i} call={c} />
            ))}
          </ul>
        ) : (
          <p className="text-xs text-stone-400">none</p>
        )}
      </section>

      <section>
        <h4 className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-stone-500">
          Guardrails
          {allowed > 0 && (
            <button type="button" onClick={() => setShowAllowed((s) => !s)} className="normal-case text-nw-700 underline">
              {showAllowed ? "hide" : "show"} {allowed} allowed
            </button>
          )}
        </h4>
        {guardrails.length ? (
          <ul className="space-y-1">
            {guardrails.map((g, i) => (
              <li key={i} className="flex gap-2 text-xs" data-testid="guardrail">
                <span className={`shrink-0 rounded px-1.5 py-0.5 font-medium ${ACTION_STYLE[g.action]}`}>
                  {g.guardrail}: {g.action}
                </span>
                <span className="text-stone-700">{g.reason}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-stone-400">nothing blocked</p>
        )}
      </section>

      {!!t.knowledge_searches?.length && (
        <section className="text-xs">
          <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-stone-500">Help center</h4>
          {t.knowledge_searches.map((k, i) => (
            <p key={i}>
              “{k.query}” → {k.articles.join(", ") || "nothing"} {k.covered ? "" : "(not covered)"}
            </p>
          ))}
          {t.grounding?.map((g, i) => (
            <p key={i}>
              grounding: <strong>{g.action}</strong> {g.reason}
            </p>
          ))}
        </section>
      )}

      {t.rejected_replies.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-stone-600">{t.rejected_replies.length} rejected draft(s)</summary>
          <ul className="mt-1 space-y-1">
            {t.rejected_replies.map((r, i) => (
              <li key={i} className="rounded bg-white p-2 line-through decoration-red-400">
                {r}
              </li>
            ))}
          </ul>
        </details>
      )}

      <details className="text-xs text-stone-600">
        <summary className="cursor-pointer">
          {t.llm_calls.length} model call(s) · prompts {Object.values(t.prompt_versions).join(", ")}
        </summary>
        <table className="mt-1 w-full text-left">
          <thead className="text-stone-400">
            <tr>
              <th>purpose</th>
              <th>model</th>
              <th>tokens</th>
              <th>ms</th>
              <th>stop</th>
            </tr>
          </thead>
          <tbody>
            {t.llm_calls.map((c, i) => (
              <tr key={i}>
                <td>{c.purpose}</td>
                <td>{c.model}</td>
                <td>
                  {c.input_tokens}/{c.output_tokens}
                </td>
                <td>{Math.round(c.latency_ms)}</td>
                <td>{c.error ?? c.stop_reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </article>
  );
}

export function TracePanel({ traces }: { traces: Trace[] }) {
  if (!traces.length) return <p className="text-sm text-stone-500">No agent turns yet.</p>;
  return (
    <div className="space-y-3">
      {traces.map((t) => (
        <TraceTurn key={t.id} trace={t} />
      ))}
    </div>
  );
}
