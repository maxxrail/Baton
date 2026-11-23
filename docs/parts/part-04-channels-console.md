# Part 04 — Channels + human handoff console

## Goal
Customers reach the same agent over **web chat, email, and SMS**; ops staff see every conversation live, inspect traces, and **take over** escalated ones.

## Scope
1. **Channel adapter interface** (`src/baton/channels/base.py`): normalize inbound → `(conversation_key, customer_hint, text)`, format outbound per channel (markdown for web, plain text + signature for email, ≤ 2 SMS segments with link to full answer for SMS), render `[kb:id]` citations per channel.
2. **Web chat**: WebSocket endpoint `/ws/chat/{conversation_id}` (and HTTP fallback). Typing indicator while the agent works; streams agent text if the LLM client supports streaming (add `stream()` to `LLMClient`; FakeLLM yields chunks).
3. **Email**: `POST /channels/email/inbound` accepting a generic JSON shape (from, subject, text, message_id, in_reply_to) — thread by `in_reply_to`/subject. Outbound via an `EmailSender` interface with a `ConsoleEmailSender` (logs) and an SMTP/Resend implementation via env. Strip quoted reply history before sending to the agent.
4. **SMS**: Twilio inbound webhook `POST /channels/sms/inbound` with **signature validation**, outbound via Twilio REST behind an `SmsSender` interface; `ConsoleSmsSender` for dev. Handle STOP/HELP keywords. (Reuse patterns from my frontdesk-agent project if useful.)
5. **Human handoff**: when escalated, the agent stops replying; a human in the console sees the handoff summary and replies as `human_agent` through the same channel; human can hand back to AI ("resolve" or "return to agent").
6. **Web app** (`web/`, React + Vite + TS + Tailwind):
   - `/widget`: embeddable chat widget (floating button + panel), Northwind branding, citations as links, "talk to a human" button. Also a `widget.js` embed snippet that mounts it on any page; `web/demo-store.html` fake store page with it embedded.
   - `/console`: inbox (filter by status/channel, live updates via WebSocket/SSE), conversation view with a **trace panel** (per turn: AOP@version + step, tool calls with args/results, guardrail decisions, latency/tokens), take-over + reply box, AOP browser (list, Mermaid graph render, validate-a-draft box hitting `/aops/validate`).
   - Simple shared-secret login for the console for now (real auth in Part 07).
7. **Dev**: `scripts/dev.py` also starts Vite. `scripts/simulate_inbound.py` sends fake email/SMS webhooks for demoing without Twilio.

## Acceptance criteria
- [ ] Same conversation logic regardless of channel (test: identical scripted FakeLLM run via web, email, SMS produces the same tool calls).
- [ ] Twilio signature validation tested (valid passes, tampered fails 403).
- [ ] Email threading test: reply with `in_reply_to` lands in the same conversation; quoted history stripped.
- [ ] Escalated conversation: agent doesn't respond to new customer messages; human reply is delivered via the original channel; hand-back works (tests).
- [ ] Console shows a live new conversation within 2s of the first widget message (manual check).
- [ ] Trace panel renders tool calls and guardrail blocks for a conversation from Part 02's adversarial cases.
- [ ] Vitest tests for widget message rendering + citation links; `npm run build` succeeds.
- [ ] Lint, types, tests clean.

## Verify
```
uv run pytest -q
cd web && npm install && npm test && npm run build && cd ..
uv run python -m scripts.dev
# open http://localhost:5173/demo-store.html → chat; open http://localhost:5173/console in another tab
uv run python -m scripts.simulate_inbound --channel sms --from "+15555550123" --text "where is my order"
```
