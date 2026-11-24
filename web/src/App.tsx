import { Console } from "./console/Console";
import { TranscriptPage, Widget } from "./widget/Widget";

/** Two routes: /widget (customer chat) and /console (ops). No router library needed. */
export function App() {
  const { pathname, search } = window.location;
  const params = new URLSearchParams(search);
  if (pathname.startsWith("/console")) return <Console />;
  if (pathname.startsWith("/widget")) {
    const transcript = params.get("c");
    if (transcript) return <TranscriptPage id={transcript} token={params.get("t")} />;
    return <Widget embedded={params.get("embed") === "1"} />;
  }
  return (
    <main className="mx-auto max-w-lg space-y-3 p-8 text-stone-800">
      <h1 className="text-2xl font-semibold text-nw-800">Baton</h1>
      <ul className="list-disc pl-5">
        <li>
          <a className="underline" href="/demo-store.html">Demo store</a> with the chat widget embedded
        </li>
        <li>
          <a className="underline" href="/widget">Widget</a> on its own
        </li>
        <li>
          <a className="underline" href="/console">Ops console</a> (inbox, traces, AOPs)
        </li>
      </ul>
    </main>
  );
}
