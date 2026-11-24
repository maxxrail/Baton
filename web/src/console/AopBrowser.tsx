import { useEffect, useState } from "react";
import { api, post, type AopDetail, type AopSummary, type Diagnostic } from "../lib/api";
import { Mermaid } from "./Mermaid";

interface ValidateOut {
  ok: boolean;
  diagnostics: Diagnostic[];
  mermaid: string | null;
}

const DRAFT_EXAMPLE = `---
id: gift_wrap
name: Gift wrap request
version: 1
triggers:
  - customer wants gift wrapping
requires_verification: false
policies:
  - Gift wrap can only be added at checkout.
---
1. Ask which order they want gift wrapped.
2. Explain that gift wrap can only be added at checkout. If they want a teammate, ESCALATE. Otherwise RESOLVE.
`;

export function AopBrowser() {
  const [aops, setAops] = useState<AopSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AopDetail | null>(null);
  const [draft, setDraft] = useState(DRAFT_EXAMPLE);
  const [result, setResult] = useState<ValidateOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ aops: AopSummary[] }>("/aops").then(
      (r) => {
        setAops(r.aops);
        setSelected((s) => s ?? r.aops[0]?.id ?? null);
      },
      (e: Error) => setError(e.message),
    );
  }, []);

  useEffect(() => {
    if (!selected) return;
    api<AopDetail>(`/aops/${selected}`).then(setDetail, (e: Error) => setError(e.message));
  }, [selected]);

  const validate = async () => {
    setResult(null);
    try {
      setResult(await post<ValidateOut>("/aops/validate", { markdown: draft }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[220px_1fr]">
      <nav className="space-y-1">
        {aops.map((a) => (
          <button
            key={a.id}
            type="button"
            onClick={() => setSelected(a.id)}
            className={`block w-full rounded px-2 py-1.5 text-left text-sm ${
              a.id === selected ? "bg-nw-100 font-medium text-nw-800" : "hover:bg-stone-100"
            }`}
          >
            {a.name}
            <span className="block font-mono text-[11px] text-stone-500">
              {a.ref} · {a.step_count} steps
            </span>
          </button>
        ))}
      </nav>

      <div className="space-y-6">
        {error && <p className="text-sm text-red-700">{error}</p>}
        {detail && (
          <section className="space-y-2">
            <h2 className="text-lg font-semibold">
              {detail.aop.name} <span className="font-mono text-sm text-stone-500">{detail.ref}</span>
            </h2>
            <Mermaid code={detail.mermaid} />
            <ol className="list-decimal space-y-1 pl-5 text-sm">
              {detail.aop.steps.map((s) => (
                <li key={s.number}>
                  {s.text}
                  {s.allowed_tools.length > 0 && (
                    <span className="ml-1 font-mono text-[11px] text-nw-700">[{s.allowed_tools.join(", ")}]</span>
                  )}
                </li>
              ))}
            </ol>
            {detail.aop.policies.length > 0 && (
              <ul className="list-disc pl-5 text-xs text-stone-600">
                {detail.aop.policies.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            )}
          </section>
        )}

        <section className="space-y-2">
          <h2 className="text-lg font-semibold">Validate a draft</h2>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={14}
            spellCheck={false}
            className="w-full rounded border border-stone-300 p-2 font-mono text-xs"
            aria-label="Draft AOP markdown"
          />
          <button type="button" onClick={validate} className="rounded bg-nw-700 px-3 py-1.5 text-sm text-white">
            Validate
          </button>
          {result && (
            <div className="space-y-2">
              <p className={`text-sm font-medium ${result.ok ? "text-emerald-700" : "text-red-700"}`}>
                {result.ok ? "Compiles." : "Doesn't compile."}
              </p>
              <ul className="space-y-1 text-xs">
                {result.diagnostics.map((d, i) => (
                  <li key={i} className={d.level === "error" ? "text-red-700" : "text-amber-700"}>
                    <code>{d.code}</code> {d.step ? `step ${d.step}: ` : ""}
                    {d.message}
                  </li>
                ))}
              </ul>
              {result.mermaid && <Mermaid code={result.mermaid} />}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
