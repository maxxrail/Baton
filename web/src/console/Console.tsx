// The ops console: log in, watch the inbox live, open a conversation (messages, handoff,
// traces), take over and reply, browse/validate AOPs, and review simulation runs.
//
// Live updates come from /ws/ops: every event names a conversation, and the console simply
// re-reads the inbox (and the open conversation if it's the one that changed). Simple, and a
// missed event only delays a refresh.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  api,
  post,
  socketUrl,
  type Channel,
  type ConversationDetail,
  type InboxItem,
  type Status,
  type Trace,
} from "../lib/api";
import { AopBrowser } from "./AopBrowser";
import { ConversationView } from "./ConversationView";
import { Simulations } from "./Simulations";

type Auth = { state: "checking" } | { state: "out"; reason?: string } | { state: "in"; name: string };

function Login({ reason, onIn }: { reason?: string; onIn: (name: string) => void }) {
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [error, setError] = useState<string | null>(reason ?? null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await post("/ops/login", { name: name.trim(), secret });
      onIn(name.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  return (
    <form onSubmit={submit} className="mx-auto mt-24 w-80 space-y-3 rounded-lg border border-stone-200 bg-white p-6 shadow">
      <h1 className="text-lg font-semibold text-nw-800">Baton console</h1>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" className="w-full rounded border px-2 py-1.5 text-sm" aria-label="Name" />
      <input value={secret} onChange={(e) => setSecret(e.target.value)} type="password" placeholder="Console secret" className="w-full rounded border px-2 py-1.5 text-sm" aria-label="Secret" />
      {error && <p className="text-xs text-red-700">{error}</p>}
      <button type="submit" disabled={!name.trim() || !secret} className="w-full rounded bg-nw-700 py-1.5 text-sm text-white disabled:opacity-40">
        Log in
      </button>
    </form>
  );
}

const AGO = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
function ago(iso: string): string {
  const s = Math.round((Date.parse(iso) - Date.now()) / 1000);
  if (Math.abs(s) < 60) return AGO.format(s, "second");
  if (Math.abs(s) < 3600) return AGO.format(Math.round(s / 60), "minute");
  return AGO.format(Math.round(s / 3600), "hour");
}

const STATUS_DOT: Record<Status, string> = { open: "bg-emerald-500", escalated: "bg-amber-500", resolved: "bg-stone-400" };

function Inbox({ operator }: { operator: string }) {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [status, setStatus] = useState<Status | "">("");
  const [channel, setChannel] = useState<Channel | "">("");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [traces, setTraces] = useState<Trace[]>([]);
  const [activity, setActivity] = useState<Record<string, string>>({});
  const [live, setLive] = useState(false);
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selected;

  const loadInbox = useCallback(async () => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    if (channel) q.set("channel", channel);
    setItems(await api<InboxItem[]>(`/ops/conversations?${q}`));
  }, [status, channel]);

  const loadDetail = useCallback(async (id: string) => {
    const [d, t] = await Promise.all([
      api<ConversationDetail>(`/conversations/${id}`),
      api<Trace[]>(`/conversations/${id}/traces`),
    ]);
    if (selectedRef.current === id) {
      setDetail(d);
      setTraces(t);
    }
  }, []);

  useEffect(() => void loadInbox(), [loadInbox]);
  useEffect(() => {
    setDetail(null);
    if (selected) void loadDetail(selected);
  }, [selected, loadDetail]);

  // Live updates. Refreshes are coalesced so a burst of events costs one request.
  useEffect(() => {
    let closed = false;
    let timer: number | undefined;
    let refresh: number | undefined;
    const dirty = new Set<string>();
    const flush = () => {
      refresh = undefined;
      void loadInbox();
      if (selectedRef.current && dirty.has(selectedRef.current)) void loadDetail(selectedRef.current);
      dirty.clear();
    };
    const connect = () => {
      const ws = new WebSocket(socketUrl("/ws/ops"));
      ws.onopen = () => setLive(true);
      ws.onmessage = (e) => {
        const ev = JSON.parse(e.data as string) as { type: string; conversation_id: string; activity?: string; reason?: string };
        if (ev.type === "activity") {
          setActivity((a) => ({ ...a, [ev.conversation_id]: ev.activity ?? "" }));
          return;
        }
        if (ev.reason === "status") setActivity((a) => ({ ...a, [ev.conversation_id]: "" }));
        dirty.add(ev.conversation_id);
        refresh ??= window.setTimeout(flush, 150);
      };
      ws.onclose = () => {
        setLive(false);
        if (!closed) timer = window.setTimeout(connect, 2000);
      };
      return ws;
    };
    const ws = connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      window.clearTimeout(refresh);
      ws.close();
    };
  }, [loadInbox, loadDetail]);

  return (
    <div className="grid h-[calc(100vh-56px)] grid-cols-[340px_1fr]">
      <aside className="flex flex-col border-r border-stone-200 bg-white">
        <div className="flex gap-2 border-b border-stone-200 p-2 text-sm">
          <select value={status} onChange={(e) => setStatus(e.target.value as Status | "")} className="rounded border px-1 py-0.5" aria-label="Status filter">
            <option value="">All statuses</option>
            <option value="open">Open</option>
            <option value="escalated">Escalated</option>
            <option value="resolved">Resolved</option>
          </select>
          <select value={channel} onChange={(e) => setChannel(e.target.value as Channel | "")} className="rounded border px-1 py-0.5" aria-label="Channel filter">
            <option value="">All channels</option>
            <option value="web">Web</option>
            <option value="email">Email</option>
            <option value="sms">SMS</option>
          </select>
          <span className={`ml-auto self-center text-xs ${live ? "text-emerald-700" : "text-stone-400"}`}>{live ? "● live" : "○ offline"}</span>
        </div>
        <ul className="flex-1 overflow-y-auto">
          {items.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => setSelected(c.id)}
                className={`block w-full border-b border-stone-100 px-3 py-2 text-left ${c.id === selected ? "bg-nw-50" : "hover:bg-stone-50"}`}
              >
                <div className="flex items-center gap-2 text-sm">
                  <span className={`h-2 w-2 rounded-full ${STATUS_DOT[c.status]}`} aria-label={c.status} />
                  <span className="truncate font-medium">{c.contact ?? c.customer_ref ?? "Web visitor"}</span>
                  <span className="rounded bg-stone-100 px-1 text-[10px] uppercase">{c.channel}</span>
                  <span className="ml-auto shrink-0 text-[11px] text-stone-400">{ago(c.updated_at)}</span>
                </div>
                <p className="mt-0.5 truncate text-xs text-stone-600">
                  {activity[c.id] ? <em className="text-nw-700">AI: {activity[c.id]}</em> : c.last_message ?? "—"}
                </p>
                {(c.aop_ref || c.assignee) && (
                  <p className="font-mono text-[10px] text-stone-400">
                    {c.aop_ref}
                    {c.assignee ? ` · ${c.assignee}` : ""}
                  </p>
                )}
              </button>
            </li>
          ))}
          {!items.length && <li className="p-4 text-sm text-stone-500">No conversations yet. Open the demo store and say hi.</li>}
        </ul>
      </aside>
      <main className="overflow-hidden bg-paper p-4">
        {detail ? (
          <ConversationView detail={detail} traces={traces} onChanged={() => selected && void loadDetail(selected)} />
        ) : (
          <p className="text-sm text-stone-500">{selected ? "Loading…" : `Signed in as ${operator}. Pick a conversation.`}</p>
        )}
      </main>
    </div>
  );
}

const TABS = ["inbox", "aops", "simulations"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABELS: Record<Tab, string> = { inbox: "Inbox", aops: "AOPs", simulations: "Simulations" };

export function tabFromPath(pathname: string): Tab {
  const rest = pathname.replace(/^\/console\/?/, "").split("/")[0];
  return (TABS as readonly string[]).includes(rest) ? (rest as Tab) : "inbox";
}

export function Console() {
  const [auth, setAuth] = useState<Auth>({ state: "checking" });
  const [tab, setTabState] = useState<Tab>(tabFromPath(window.location.pathname));
  // Each tab has its own URL (/console, /console/aops, /console/simulations) so it can be linked.
  const setTab = (t: Tab) => {
    setTabState(t);
    window.history.replaceState(null, "", t === "inbox" ? "/console" : `/console/${t}`);
  };

  useEffect(() => {
    api<{ name: string }>("/ops/me").then(
      (me) => setAuth({ state: "in", name: me.name }),
      (e: unknown) =>
        setAuth({ state: "out", reason: e instanceof ApiError && e.status === 503 ? e.message : undefined }),
    );
  }, []);

  if (auth.state === "checking") return null;
  if (auth.state === "out") return <Login reason={auth.reason} onIn={(name) => setAuth({ state: "in", name })} />;

  const logout = async () => {
    await post("/ops/logout");
    setAuth({ state: "out" });
  };

  return (
    <div className="min-h-screen bg-paper text-stone-800">
      <header className="flex h-14 items-center gap-6 bg-nw-800 px-4 text-white">
        <span className="font-semibold">Baton console</span>
        {TABS.map((t) => (
          <button key={t} type="button" onClick={() => setTab(t)} className={`text-sm ${tab === t ? "underline underline-offset-4" : "text-nw-100"}`}>
            {TAB_LABELS[t]}
          </button>
        ))}
        <span className="ml-auto text-sm text-nw-100">{auth.name}</span>
        <button type="button" onClick={logout} className="text-sm text-nw-100 underline">
          Log out
        </button>
      </header>
      {tab === "inbox" && <Inbox operator={auth.name} />}
      {tab === "aops" && (
        <div className="p-4">
          <AopBrowser />
        </div>
      )}
      {tab === "simulations" && <Simulations />}
    </div>
  );
}
