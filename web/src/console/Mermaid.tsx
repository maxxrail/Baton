import { useEffect, useId, useRef, useState } from "react";

/** Renders a Mermaid graph. Mermaid is loaded on first use (it's large) and runs with
 * securityLevel "strict", which sanitizes labels, so a draft AOP can't inject markup. */
export function Mermaid({ code }: { code: string }) {
  const host = useRef<HTMLDivElement | null>(null);
  const id = `m${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "neutral" });
        const { svg } = await mermaid.render(`${id}-${Date.now()}`, code);
        if (!cancelled && host.current) {
          host.current.innerHTML = svg; // sanitized by mermaid (strict)
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, id]);

  return (
    <div>
      <div ref={host} className="overflow-auto rounded border border-stone-200 bg-white p-2" />
      {error && <pre className="mt-1 text-xs text-red-700">{error}</pre>}
    </div>
  );
}
