// The customer chat widget: a floating button that opens a chat panel.
//
// It creates a web conversation on first open (the id is remembered in localStorage, so a
// reload continues the chat), loads the transcript, and talks over /ws/chat/{id}: the server
// pushes typing activity, agent and teammate replies, and status changes. If the socket isn't
// open, a message goes over the HTTP fallback (POST /conversations/{id}/messages).
//
// Inside an iframe (`?embed=1`, see public/widget.js) it tells the host page when it opens or
// closes, so the iframe only covers the page while the panel is open.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  api,
  post,
  socketUrl,
  type ChatEvent,
  type ChatMessage,
  type ChatReply,
  type Status,
  type Transcript,
} from "../lib/api";
import { MessageBubble, activityLabel } from "./MessageBubble";

const STORAGE_KEY = "baton.conversation";
const HUMAN_REQUEST = "I'd like to talk to a person, please.";
const GREETING: ChatMessage = {
  id: "greeting",
  role: "agent",
  body: "Hi! I'm the Northwind Goods assistant. I can help with orders, returns, refunds, subscriptions and loyalty points.",
  citations: [],
};

function remembered(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function remember(id: string | null) {
  try {
    if (id) window.localStorage.setItem(STORAGE_KEY, id);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* private mode: the chat just won't survive a reload */
  }
}

/** Merge a pushed message, skipping one we already show (same id). */
export function addMessage(list: ChatMessage[], msg: ChatMessage): ChatMessage[] {
  if (msg.id !== null && list.some((m) => m.id === msg.id)) return list;
  return [...list, msg];
}

export function Widget({ embedded = false }: { embedded?: boolean }) {
  const [open, setOpen] = useState(false);
  const [cid, setCid] = useState<string | null>(remembered);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<Status>("open");
  const [activity, setActivity] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const socket = useRef<WebSocket | null>(null);
  const bottom = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (embedded) window.parent.postMessage({ type: "baton:resize", open }, "*");
  }, [open, embedded]);

  useEffect(() => bottom.current?.scrollIntoView({ block: "end" }), [messages, activity]);

  // Create or resume the conversation when the panel first opens.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        let id = cid;
        if (id) {
          try {
            const t = await api<Transcript>(`/conversations/${id}/messages`);
            if (cancelled) return;
            setMessages(t.messages);
            setStatus(t.status);
          } catch (err) {
            if (!(err instanceof ApiError && err.status === 404)) throw err;
            id = null; // the server forgot it (fresh database): start over
          }
        }
        if (!id) {
          const created = await post<{ id: string }>("/conversations", { channel: "web" });
          id = created.id;
          remember(id);
          if (!cancelled) {
            setCid(id);
            setMessages([]);
          }
        }
      } catch (err) {
        if (!cancelled) setNotice(err instanceof Error ? err.message : "Chat is unavailable.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, cid]);

  // The live socket, reconnecting with backoff while the panel is open.
  useEffect(() => {
    if (!open || !cid) return;
    let closed = false;
    let retry = 0;
    let timer: number | undefined;
    const connect = () => {
      const ws = new WebSocket(socketUrl(`/ws/chat/${cid}`));
      socket.current = ws;
      ws.onopen = () => {
        retry = 0;
      };
      ws.onmessage = (e) => {
        const event = JSON.parse(e.data as string) as ChatEvent;
        if (event.type === "typing") setActivity(event.activity || null);
        else if (event.type === "message") {
          setActivity(null);
          setMessages((list) => addMessage(list, event.message));
        } else if (event.type === "status") {
          setStatus(event.status);
          if (event.note) setNotice(event.note);
        } else if (event.type === "error") {
          setActivity(null);
          setNotice(event.message);
        }
      };
      ws.onclose = () => {
        if (socket.current === ws) socket.current = null;
        if (!closed) timer = window.setTimeout(connect, Math.min(1000 * 2 ** retry++, 15000));
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      socket.current?.close();
      socket.current = null;
    };
  }, [open, cid]);

  const send = useCallback(
    async (text: string, kind: "message" | "request_human" = "message") => {
      if (!cid || !text.trim()) return;
      setNotice(null);
      setMessages((list) => [
        ...list,
        { id: `local-${Date.now()}`, role: "customer", body: text, citations: [] },
      ]);
      const ws = socket.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        setActivity("routing");
        ws.send(JSON.stringify(kind === "message" ? { type: "message", text } : { type: kind }));
        return;
      }
      // HTTP fallback: the reply comes back in the response.
      setActivity("thinking");
      try {
        const reply = await post<ChatReply>(`/conversations/${cid}/messages`, { text });
        setStatus(reply.status);
        if (reply.replied) {
          setMessages((list) =>
            addMessage(list, {
              id: reply.message_id,
              role: "agent",
              body: reply.body,
              citations: reply.citations,
            }),
          );
        }
      } catch (err) {
        setNotice(err instanceof Error ? err.message : "Message not sent.");
      } finally {
        setActivity(null);
      }
    },
    [cid],
  );

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text || activity) return;
    setDraft("");
    void send(text);
  };

  const shown = messages.length ? messages : [GREETING];

  return (
    <div className={`${embedded ? "fixed" : "fixed"} bottom-4 right-4 z-50 flex flex-col items-end gap-3`}>
      {open && (
        <section
          aria-label="Northwind Goods chat"
          className="flex h-[min(560px,calc(100vh-6rem))] w-[min(370px,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-stone-200 bg-paper shadow-2xl"
        >
          <header className="flex items-center justify-between bg-nw-800 px-4 py-3 text-white">
            <div>
              <div className="text-sm font-semibold">Northwind Goods</div>
              <div className="text-xs text-nw-100">
                {status === "escalated" ? "A teammate will reply here" : "Support · usually instant"}
              </div>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded px-2 py-1 text-lg leading-none hover:bg-nw-700"
              aria-label="Close chat"
            >
              ×
            </button>
          </header>

          <div className="flex-1 space-y-3 overflow-y-auto px-3 py-4" aria-live="polite">
            {shown.map((m, i) => (
              <MessageBubble key={m.id ?? `m${i}`} message={m} />
            ))}
            {activity && (
              <div className="flex items-center gap-2 text-xs text-stone-500" role="status">
                <span className="inline-flex gap-0.5">
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-stone-400" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-stone-400 [animation-delay:120ms]" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-stone-400 [animation-delay:240ms]" />
                </span>
                {activityLabel(activity)}
              </div>
            )}
            <div ref={bottom} />
          </div>

          {notice && <div className="border-t border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">{notice}</div>}

          <form onSubmit={submit} className="border-t border-stone-200 bg-white p-2">
            <div className="flex gap-2">
              <input
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={status === "escalated" ? "Write to our teammate…" : "Ask about an order, return, refund…"}
                maxLength={4000}
                className="flex-1 rounded-lg border border-stone-300 px-3 py-2 text-sm focus:border-nw-600 focus:outline-none"
                aria-label="Message"
              />
              <button
                type="submit"
                disabled={!draft.trim() || !!activity}
                className="rounded-lg bg-nw-700 px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
              >
                Send
              </button>
            </div>
            {status !== "escalated" && (
              <button
                type="button"
                onClick={() => void send(HUMAN_REQUEST, "request_human")}
                disabled={!!activity || !cid}
                className="mt-1.5 text-xs text-nw-700 underline disabled:opacity-40"
              >
                Talk to a human
              </button>
            )}
          </form>
        </section>
      )}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex h-14 w-14 items-center justify-center rounded-full bg-nw-700 text-white shadow-xl hover:bg-nw-800"
        aria-label={open ? "Close chat" : "Chat with Northwind Goods"}
      >
        <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
          {open ? (
            <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
          ) : (
            <path d="M4 5h16v11H8l-4 4V5z" strokeLinejoin="round" />
          )}
        </svg>
      </button>
    </div>
  );
}

/** `/widget?c=<id>&t=<token>`: the full transcript, read-only. SMS replies that don't fit
 * link here with a signed, expiring token (baton.channels.links). */
export function TranscriptPage({ id, token }: { id: string; token: string | null }) {
  const [t, setT] = useState<Transcript | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const query = token ? `?t=${encodeURIComponent(token)}` : "";
    api<Transcript>(`/conversations/${encodeURIComponent(id)}/messages${query}`).then(setT, () =>
      setError("This link has expired or isn't valid."),
    );
  }, [id, token]);
  return (
    <main className="mx-auto max-w-xl space-y-3 p-4">
      <h1 className="text-lg font-semibold text-nw-800">Your Northwind Goods conversation</h1>
      {error && <p className="text-sm text-red-700">{error}</p>}
      {t?.messages.map((m) => <MessageBubble key={m.id} message={m} />)}
    </main>
  );
}
