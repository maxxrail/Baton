// Typed access to the Baton API. Everything goes through Vite's proxy (same origin), so
// cookies (console login) and WebSockets work without CORS.

export type Role = "customer" | "agent" | "human_agent" | "system";
export type Status = "open" | "escalated" | "resolved";
export type Channel = "web" | "email" | "sms" | "cli";

export interface Citation {
  article_id: string;
  title: string;
  url: string;
}

/** A customer-visible message, `body` already formatted for the web (markdown). */
export interface ChatMessage {
  id: number | string | null;
  role: Role;
  body: string;
  citations: Citation[];
  created_at?: string;
}

export interface Transcript {
  id: string;
  status: Status;
  messages: ChatMessage[];
}

export interface ChatReply {
  text: string;
  body: string;
  status: Status;
  replied: boolean;
  message_id: number | null;
  citations: Citation[];
}

/** What the server pushes on /ws/chat/{id}. */
export type ChatEvent =
  | { type: "typing"; activity: string }
  | { type: "message"; message: ChatMessage }
  | { type: "status"; status: Status; note?: string }
  | { type: "error"; code: string; message: string };

// ---------------------------------------------------------------- console types

export interface InboxItem {
  id: string;
  channel: Channel;
  status: Status;
  contact: string | null;
  subject: string | null;
  customer_ref: string | null;
  aop_ref: string | null;
  assignee: string | null;
  escalation_reason: string | null;
  last_role: Role | null;
  last_message: string | null;
  message_count: number;
  updated_at: string;
}

export interface HandoffSummary {
  customer: string;
  intent: string;
  tried: string[];
  needed: string;
  reason: string;
  generated_by: "llm" | "fallback";
}

export interface ConversationState {
  status: Status;
  channel: Channel;
  verified: boolean;
  customer_ref: string | null;
  customer_name: string | null;
  aop_id: string | null;
  aop_version: number | null;
  current_step: number | null;
  escalation_reason: string | null;
  handoff: HandoffSummary | null;
  assignee: string | null;
  turn_count: number;
}

export interface StoredMessage {
  id: number;
  role: Role;
  content: string;
  meta: Record<string, unknown> | null;
  created_at: string;
}

export interface ConversationDetail {
  id: string;
  channel: Channel;
  status: Status;
  contact: string | null;
  subject: string | null;
  state: ConversationState;
  messages: StoredMessage[];
}

export interface GuardrailDecision {
  guardrail: string;
  action: "allow" | "block" | "rewrite" | "escalate";
  reason: string;
  tool?: string | null;
}

export interface ToolCallTrace {
  name: string;
  args: Record<string, unknown>;
  ok: boolean;
  result: Record<string, unknown> | null;
  error_code: string | null;
  blocked_by: string | null;
  source: "agent" | "guardrail";
  latency_ms: number;
}

export interface LLMCallTrace {
  purpose: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  stop_reason: string;
  error: string | null;
}

export interface TracePayload {
  turn: number;
  channel: string;
  aop_ref: string | null;
  step_before: number | null;
  step_after: number | null;
  prompt_versions: Record<string, string>;
  route: { aop_id: string | null; candidates?: string[]; rationale?: string } | null;
  llm_calls: LLMCallTrace[];
  tool_calls: ToolCallTrace[];
  guardrails: GuardrailDecision[];
  knowledge_searches?: { query: string; covered: boolean; articles: string[] }[];
  grounding?: { action: string; reason: string }[];
  citations?: string[];
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  status: Status;
  escalation_reason: string | null;
  reply_source: string;
  rejected_replies: string[];
  error: string | null;
}

export interface Trace {
  id: string;
  message_id: number | null;
  created_at: string;
  payload: TracePayload;
}

export interface Diagnostic {
  level: "error" | "warning";
  aop_id: string;
  step: number | null;
  code: string;
  message: string;
}

export interface AopSummary {
  id: string;
  name: string;
  version: number;
  ref: string;
  requires_verification: boolean;
  triggers: string[];
  step_count: number;
}

export interface AopDetail {
  ref: string;
  mermaid: string;
  aop: {
    id: string;
    name: string;
    policies: string[];
    steps: { number: number; text: string; allowed_tools: string[] }[];
  };
}

// ---------------------------------------------------------------- client

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  const resp = await fetch(path, { credentials: "same-origin", ...init, headers });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = (await resp.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export function socketUrl(path: string): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}${path}`;
}
