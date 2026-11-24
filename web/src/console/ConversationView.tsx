import { useState } from "react";
import { post, type ConversationDetail, type Trace } from "../lib/api";
import { TracePanel } from "./TracePanel";

const ROLE_STYLE: Record<string, string> = {
  customer: "bg-white border-stone-200",
  agent: "bg-nw-50 border-nw-100",
  human_agent: "bg-sky-50 border-sky-200",
  system: "bg-transparent border-dashed border-stone-300 italic text-stone-500",
};
const ROLE_LABEL: Record<string, string> = {
  customer: "Customer",
  agent: "AI agent",
  human_agent: "Teammate",
  system: "System",
};

export function ConversationView({
  detail,
  traces,
  onChanged,
}: {
  detail: ConversationDetail;
  traces: Trace[];
  onChanged: () => void;
}) {
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [tab, setTab] = useState<"messages" | "traces">("messages");
  const { state } = detail;
  const humanOwned = detail.status === "escalated";

  const act = async (path: string, body?: unknown) => {
    setBusy(true);
    setNotice(null);
    try {
      const out = await post<{ delivered?: boolean; error?: string | null }>(
        `/ops/conversations/${detail.id}/${path}`,
        body,
      );
      if (out && out.delivered === false) setNotice(`Saved, but not delivered: ${out.error}`);
      onChanged();
      return true;
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (reply.trim() && (await act("reply", { text: reply.trim() }))) setReply("");
  };

  return (
    <div className="flex h-full flex-col">
      <header className="space-y-1 border-b border-stone-200 pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold">{detail.contact ?? state.customer_ref ?? "Anonymous visitor"}</h2>
          <span className="rounded bg-stone-200 px-1.5 text-xs">{detail.channel}</span>
          <span className="rounded bg-stone-200 px-1.5 text-xs">{detail.status}</span>
          {state.verified && <span className="rounded bg-emerald-100 px-1.5 text-xs text-emerald-800">verified {state.customer_ref}</span>}
          {state.assignee && <span className="text-xs text-stone-500">owned by {state.assignee}</span>}
        </div>
        {detail.subject && <p className="text-sm text-stone-600">Subject: {detail.subject}</p>}
        {state.aop_id && (
          <p className="font-mono text-xs text-nw-700">
            {state.aop_id}@{state.aop_version} · step {state.current_step}
          </p>
        )}
        <div className="flex flex-wrap gap-2 pt-1">
          {!humanOwned && (
            <button type="button" disabled={busy} onClick={() => act("takeover")} className="rounded bg-nw-700 px-3 py-1 text-sm text-white disabled:opacity-40">
              Take over
            </button>
          )}
          {humanOwned && (
            <>
              <button type="button" disabled={busy} onClick={() => act("handback", { action: "return_to_agent" })} className="rounded border border-nw-700 px-3 py-1 text-sm text-nw-700 disabled:opacity-40">
                Return to AI
              </button>
              <button type="button" disabled={busy} onClick={() => act("handback", { action: "resolve" })} className="rounded border border-stone-400 px-3 py-1 text-sm disabled:opacity-40">
                Resolve
              </button>
            </>
          )}
        </div>
      </header>

      {state.handoff && (
        <section className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm" aria-label="Handoff summary">
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-amber-900">
            Handoff {state.handoff.generated_by === "fallback" ? "(fallback summary)" : ""}
          </h3>
          <dl className="grid grid-cols-[90px_1fr] gap-x-2 gap-y-0.5">
            <dt className="text-amber-800">Customer</dt>
            <dd>{state.handoff.customer}</dd>
            <dt className="text-amber-800">Wants</dt>
            <dd>{state.handoff.intent}</dd>
            <dt className="text-amber-800">Tried</dt>
            <dd>{state.handoff.tried.join("; ")}</dd>
            <dt className="text-amber-800">Needed</dt>
            <dd>{state.handoff.needed}</dd>
            <dt className="text-amber-800">Why</dt>
            <dd>{state.handoff.reason}</dd>
          </dl>
        </section>
      )}
      {!state.handoff && state.escalation_reason && (
        <p className="mt-3 rounded bg-amber-50 px-3 py-2 text-sm text-amber-900">{state.escalation_reason}</p>
      )}

      <div className="mt-3 flex gap-3 border-b border-stone-200 text-sm">
        {(["messages", "traces"] as const).map((t) => (
          <button key={t} type="button" onClick={() => setTab(t)} className={`pb-1 ${tab === t ? "border-b-2 border-nw-700 font-medium" : "text-stone-500"}`}>
            {t === "messages" ? `Messages (${detail.messages.length})` : `Traces (${traces.length})`}
          </button>
        ))}
      </div>

      <div className="mt-3 flex-1 overflow-y-auto pr-1">
        {tab === "messages" ? (
          <ol className="space-y-2">
            {detail.messages.map((m) => (
              <li key={m.id} className={`rounded border px-3 py-2 text-sm ${ROLE_STYLE[m.role]}`}>
                <div className="mb-0.5 text-[11px] font-semibold text-stone-500">
                  {ROLE_LABEL[m.role]}
                  {typeof m.meta?.operator === "string" ? ` · ${m.meta.operator}` : ""}
                  <span className="ml-2 font-normal">{new Date(m.created_at).toLocaleTimeString()}</span>
                </div>
                <p className="whitespace-pre-wrap">{m.content}</p>
              </li>
            ))}
          </ol>
        ) : (
          <TracePanel traces={traces} />
        )}
      </div>

      {notice && <p className="mt-2 rounded bg-amber-50 px-3 py-1.5 text-xs text-amber-900">{notice}</p>}
      <form onSubmit={send} className="mt-2 flex gap-2 border-t border-stone-200 pt-2">
        <textarea
          value={reply}
          onChange={(e) => setReply(e.target.value)}
          disabled={!humanOwned || busy}
          rows={2}
          placeholder={humanOwned ? `Reply as a teammate via ${detail.channel}…` : "Take over to reply"}
          className="flex-1 rounded border border-stone-300 px-2 py-1 text-sm disabled:bg-stone-100"
          aria-label="Reply"
        />
        <button type="submit" disabled={!humanOwned || busy || !reply.trim()} className="self-end rounded bg-nw-700 px-3 py-1.5 text-sm text-white disabled:opacity-40">
          Send
        </button>
      </form>
    </div>
  );
}
