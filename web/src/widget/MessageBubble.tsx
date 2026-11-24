import type { ChatMessage } from "../lib/api";
import { Markdown } from "../lib/markdown";

const LABEL: Record<string, string> = {
  agent: "Northwind assistant",
  human_agent: "Northwind teammate",
};

/** One chat bubble. Customer text is shown exactly as typed (never parsed as markdown);
 * agent and teammate replies render the server's markdown, citations included. */
export function MessageBubble({ message }: { message: ChatMessage }) {
  const mine = message.role === "customer";
  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        data-role={message.role}
        className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm leading-relaxed ${
          mine ? "bg-nw-700 text-white rounded-br-sm" : "bg-white text-stone-800 shadow-sm rounded-bl-sm"
        }`}
      >
        {!mine && (
          <div className="mb-0.5 text-[11px] font-semibold text-nw-700">{LABEL[message.role] ?? ""}</div>
        )}
        {mine ? (
          <p className="whitespace-pre-wrap break-words">{message.body}</p>
        ) : (
          <div className="markdown space-y-2 break-words">
            <Markdown text={message.body} />
          </div>
        )}
      </div>
    </div>
  );
}

const ACTIVITY: Record<string, string> = {
  routing: "Reading your message…",
  thinking: "Typing…",
  checking: "Double-checking the answer…",
  handoff: "Bringing in a teammate…",
  "tool:lookup_customer": "Finding your account…",
  "tool:verify_customer": "Verifying your details…",
  "tool:get_order": "Looking up your order…",
  "tool:list_orders": "Looking up your orders…",
  "tool:search_knowledge": "Searching the help center…",
};

export function activityLabel(activity: string): string {
  if (ACTIVITY[activity]) return ACTIVITY[activity];
  if (activity.startsWith("tool:")) return "Working on it…";
  return "Typing…";
}
