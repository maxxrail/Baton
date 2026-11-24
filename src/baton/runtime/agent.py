"""The agent loop: one customer message in, one guarded reply out.

`Agent.handle_turn(conversation_id, message)`:

1. Load the conversation state and store the customer's message (short transaction).
2. Conversation-level guardrails: an explicit request for a human, or too many turns without
   progress, escalates before the model runs.
3. Route to an AOP with the Part 01 router if none is active. Mid-procedure, the model calls
   `switch_procedure` when the customer changes topic and the runtime re-routes.
4. Tool-calling loop. The system prompt is the brand voice, the rules, the state summary and the
   current step's compiled prompt; only the tools this step allows are offered. Every store
   tool call passes `check_tool_call` (allow-list, identity gate, scope) and refunds also pass
   `check_refund`. At most `MAX_TOOL_CALLS` store calls run per turn. The model moves through
   the procedure with `advance_step(target)`, which is checked against the compiled graph.
5. The final text passes `check_reply`. A blocked reply gets one rewrite, then a safe fallback.
   A help-center answer (the turn searched the help center, the reply cites it, or the active
   procedure answers from it) then passes the grounding check (`baton.knowledge.grounding`):
   unsupported claims are removed, or the reply becomes an abstention that offers a teammate.
6. Save the reply, the new state and the turn's trace (second short transaction).

No database transaction is held open while the model or the store is being called.
"""

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.models import AOPBundle, CompiledAOP, CompiledStep, Terminal, target_label
from baton.aop.router import Router
from baton.aop.store import sync_versions
from baton.db.models import Conversation, Message, MessageRole
from baton.db.session import session_scope
from baton.knowledge.grounding import (
    HEDGE_NOTE,
    Evidence,
    GroundingChecker,
    citations,
    normalize_citations,
)
from baton.knowledge.retrieve import IndexedChunk, KnowledgeIndexError
from baton.llm.client import LLMClient
from baton.llm.types import (
    ContentBlock,
    LLMResponse,
    Msg,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
)
from baton.prompts import Prompt, load_prompt
from baton.runtime.escalation import build_handoff
from baton.runtime.guardrails import (
    KNOWLEDGE_TOOL,
    REFUND_TOOLS,
    Action,
    GuardrailConfig,
    GuardrailDecision,
    OrderBalance,
    ReplyContext,
    allowed_tools,
    check_progress,
    check_refund,
    check_reply,
    check_tool_call,
    check_verification_attempts,
    truncate,
    wants_human,
)
from baton.runtime.redact import redact
from baton.runtime.state import (
    Channel,
    ConversationState,
    Status,
    acquire_turn,
    credential_key,
    load_state,
    release_turn,
    save_state,
)
from baton.runtime.traces import (
    KnowledgeSearchTrace,
    LLMCallTrace,
    LLMPurpose,
    ToolCallTrace,
    TurnTrace,
    save_trace,
)
from baton.tools import schemas as s
from baton.tools.northwind import NorthwindTools, ToolContext, ToolOutcome

log = structlog.get_logger("baton.runtime")

MAX_TOOL_CALLS = 6  # store tool calls per turn (control tools don't count)
MAX_LLM_CALLS = 10  # model calls per turn, a backstop against advance/switch loops
HISTORY_MESSAGES = 30
AGENT_PROMPT, BRAND_PROMPT = "agent", "brand"
ADVANCE_TOOL, SWITCH_TOOL = "advance_step", "switch_procedure"

# Customer-facing messages the runtime sends itself (not model output).
ESCALATION_REPLY = (
    "Thanks for your patience. I'm passing this to a teammate who can help from here. "
    "They'll reply in this conversation and can see everything we've discussed."
)
HOLDING_REPLY = (
    "A teammate has this conversation and will reply here soon. Anything you add now will be "
    "waiting for them."
)
FALLBACK_REPLY = (
    "Sorry, I wasn't able to put together a proper answer to that. Could you tell me a bit more "
    "about what you need, or would you like me to bring in a teammate?"
)
ERROR_REPLY = "Sorry, something went wrong on our side. Please try again in a moment."
NO_PROCEDURE = (
    "## Current procedure\nNone yet. Greet the customer briefly and ask what they need help "
    "with. Don't answer questions about orders or accounts until a procedure is active."
)
DRAFT_KEPT_NOTE = (
    "[system] The text you wrote with those tool calls will be sent to the customer. Don't "
    "repeat it; reply only with anything still missing, or a brief closing."
)
REWRITE_INSTRUCTION = (
    "[system] Your reply was not sent: {reason}. Write a new reply to the customer that fixes "
    "this. Don't call tools."
)


ACCOUNT_EVIDENCE_CHARS = 3000  # per tool result shown to the grounding judge


class Citation(BaseModel):
    """A help-center article cited in a reply as `[kb:article_id]`; channels render it."""

    article_id: str
    title: str
    url: str


class AgentReply(BaseModel):
    text: str
    status: Status
    trace_id: str
    citations: list[Citation] = Field(default_factory=list)


@dataclass
class _Turn:
    """Scratch state for one turn."""

    state: ConversationState
    trace: TurnTrace
    llm: "_MeteredLLM"
    customer_message: str
    history: list[Msg]
    customer_text: str
    messages: list[Msg] = field(default_factory=list)
    store_calls: int = 0
    switched: bool = False
    progress: bool = False
    refund_succeeded: bool = False
    reported_refunds: bool = False
    refund_amounts: set[int] = field(default_factory=set)
    escalation: str | None = None
    forced_escalation: bool = False  # a guardrail escalated: the runtime writes the reply
    tried: list[str] = field(default_factory=list)
    # Grounding inputs: help-center passages returned this turn, other tool results, and
    # whether a procedure that answers from the help center was active at any point.
    kb_results: list[s.KnowledgeResult] = field(default_factory=list)
    account_evidence: list[Evidence] = field(default_factory=list)
    searched: bool = False
    knowledge_procedure: bool = False
    previous_kb_evidence: list[int] = field(default_factory=list)
    drafts: list[str] = field(default_factory=list)  # text sent along with tool calls
    completed: list[str] = field(default_factory=list)  # side effects the store confirmed
    procedures: dict[str, CompiledAOP] = field(default_factory=dict)  # active during the turn


class _MeteredLLM:
    """Wraps the LLM for one turn and records every call (tokens, latency) on the trace."""

    def __init__(self, inner: LLMClient, trace: TurnTrace) -> None:
        self.inner = inner
        self.trace = trace
        self.purpose: LLMPurpose = "agent"

    def complete(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 16000,
    ) -> LLMResponse:
        start = time.perf_counter()
        try:
            resp = self.inner.complete(system, messages, tools, temperature, max_tokens)
        except Exception as exc:
            ms = round((time.perf_counter() - start) * 1000, 1)
            self.trace.add_llm_call(
                LLMCallTrace(purpose=self.purpose, latency_ms=ms, error=type(exc).__name__)
            )
            raise
        self.trace.add_llm_call(
            LLMCallTrace(
                purpose=self.purpose,
                model=resp.model,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                stop_reason=resp.stop_reason,
            )
        )
        return resp


def _control_tools(aop: CompiledAOP, step: CompiledStep) -> list[ToolSpec]:
    exits = [target_label(t.target) for t in step.transitions]
    return [
        ToolSpec(
            name=ADVANCE_TOOL,
            description=(
                "Move the procedure forward when the current step is done. `target` is a step "
                f"number, RESOLVE or ESCALATE. Allowed from this step: {', '.join(exits)}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "e.g. '3', 'RESOLVE', 'ESCALATE'"},
                    "reason": {"type": "string", "description": "why, in a few words"},
                },
                "required": ["target", "reason"],
            },
        ),
        ToolSpec(
            name=SWITCH_TOOL,
            description=(
                f"The customer now wants something '{aop.name}' doesn't cover. The system "
                "re-routes their latest message to the right procedure."
            ),
            input_schema={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        ),
    ]


def _answers_from_help_center(aop: CompiledAOP) -> bool:
    """A procedure whose steps reference `@search_knowledge` answers from the help center, so
    every reply it produces is grounding-checked, even one written without searching."""
    return any(KNOWLEDGE_TOOL in step.allowed_tools for step in aop.steps)


def _done(call: ToolCall, out: BaseModel | None) -> str:
    """A plain statement of a side effect the store confirmed, for runtime-written replies."""
    order = str(call.input.get("order_number", "")).strip().upper()
    if call.name == "create_refund":
        cents = int(str(call.input.get("amount_cents", 0)))
        return f"issued a refund of {_dollars(cents)} on order {order}"
    if call.name == "create_return" and isinstance(out, s.ReturnAuthorization):
        return f"opened return {out.rma_number} for order {order}"
    phrases = {
        "cancel_order": f"cancelled order {order}",
        "change_address": f"updated the shipping address on order {order}",
        "pause_subscription": "paused your subscription",
        "resume_subscription": "resumed your subscription",
        "cancel_subscription": "cancelled your subscription",
    }
    return phrases.get(call.name, f"completed {call.name.replace('_', ' ')}")


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.0f}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


def _parse_target(raw: object) -> int | Terminal | None:
    text = str(raw).strip().upper().removeprefix("STEP").strip()
    if text in (Terminal.ESCALATE.value, Terminal.RESOLVE.value):
        return Terminal(text)
    return int(text) if text.isdigit() else None


def _result(call: ToolCall, payload: dict[str, object], *, is_error: bool) -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id=call.id, content=json.dumps(payload, default=str), is_error=is_error
    )


def _decision(guardrail: str, action: Action, reason: str) -> GuardrailDecision:
    return GuardrailDecision(guardrail=guardrail, action=action, reason=reason)


def _blocked(call: ToolCall, decision: GuardrailDecision) -> ToolResultBlock:
    error = {"code": "BLOCKED", "guardrail": decision.guardrail, "message": decision.reason}
    return _result(call, {"error": error}, is_error=True)


class Agent:
    def __init__(
        self,
        bundle: AOPBundle,
        llm: LLMClient,
        tools: NorthwindTools,
        session_factory: sessionmaker[Session],
        *,
        router: Router | None = None,
        config: GuardrailConfig | None = None,
        prompts_dir: Path | None = None,
        grounding: GroundingChecker | None = None,
        raise_errors: bool = False,
    ) -> None:
        """`raise_errors=True` (tests) re-raises unexpected exceptions after the error trace is
        written, instead of answering the customer with a generic apology.

        The help center comes from `tools.knowledge`; `grounding` defaults to a checker that
        uses the agent's own LLM."""
        self.bundle = bundle
        self.llm = llm
        self.tools = tools
        self.knowledge = tools.knowledge
        self.grounding = grounding or GroundingChecker(llm, prompts_dir)
        self.registry = tools.registry
        self.sessions = session_factory
        self.router = router or Router(bundle, llm, prompts_dir=prompts_dir)
        self.config = config or GuardrailConfig()
        self.prompts_dir = prompts_dir
        self.raise_errors = raise_errors
        self.agent_prompt: Prompt = load_prompt(AGENT_PROMPT, prompts_dir)
        self.brand_prompt: Prompt = load_prompt(BRAND_PROMPT, prompts_dir)
        self._versions_synced = False
        self._sync_lock = threading.Lock()

    # ------------------------------------------------------------------ conversations

    def start_conversation(self, channel: Channel, customer_hint: str | None = None) -> str:
        with session_scope(self.sessions) as session:
            convo = Conversation(channel=channel)
            session.add(convo)
            session.flush()
            state = ConversationState(conversation_id=convo.id, channel=channel)
            if customer_hint:
                state.slots["customer_hint"] = customer_hint.strip()[:254]
            save_state(session, state)
            return convo.id

    def _sync_versions_once(self) -> None:
        """Store every compiled AOP version so each trace's `aop_id@version` has a row."""
        with self._sync_lock:
            if self._versions_synced:
                return
            with session_scope(self.sessions) as session:
                sync_versions(session, self.bundle)
            self._versions_synced = True

    # ------------------------------------------------------------------ the turn

    def handle_turn(self, conversation_id: str, customer_message: str) -> AgentReply:
        start = time.perf_counter()
        customer_message = customer_message.strip()
        self._sync_versions_once()

        with session_scope(self.sessions) as session:
            acquire_turn(session, conversation_id)  # ConversationBusyError if one is running
            state = load_state(session, conversation_id)
            rows = self._message_rows(session, conversation_id)
            session.add(
                Message(
                    conversation_id=conversation_id,
                    role=MessageRole.CUSTOMER.value,
                    content=customer_message,
                )
            )

        try:
            return self._respond(state, rows, customer_message, start)
        except BaseException:
            # `_save` releases the lease with the reply. If we never got that far, release it
            # here so the conversation isn't stuck until the lease expires.
            with session_scope(self.sessions) as session:
                release_turn(session, conversation_id)
            raise

    def _respond(
        self,
        state: ConversationState,
        rows: list[tuple[str, str]],
        customer_message: str,
        start: float,
    ) -> AgentReply:
        conversation_id = state.conversation_id
        state.turn_count += 1
        trace = TurnTrace(
            conversation_id=conversation_id,
            turn=state.turn_count,
            channel=state.channel,
            step_before=state.current_step,
            prompt_versions={
                AGENT_PROMPT: self.agent_prompt.version,
                BRAND_PROMPT: self.brand_prompt.version,
            },
        )
        customer_text = "\n".join(
            [text for role, text in rows if role == MessageRole.CUSTOMER.value] + [customer_message]
        )
        turn = _Turn(
            state=state,
            trace=trace,
            llm=_MeteredLLM(self.llm, trace),
            customer_message=customer_message,
            history=self._llm_history(rows),
            customer_text=customer_text,
            previous_kb_evidence=list(state.kb_evidence),
        )
        self._note_aop(turn)
        was_escalated = state.status == "escalated"

        if was_escalated:
            text = HOLDING_REPLY
            trace.reply_source = "holding"
        else:
            state.status = "open"  # a resolved conversation reopens on a new message
            try:
                text = self._run(turn)
            except Exception as exc:
                trace.error = f"{type(exc).__name__}: {redact(str(exc))[:300]}"
                trace.reply_source = "fallback"
                text = ERROR_REPLY
                if self.raise_errors:
                    self._save(turn, text, start)
                    raise
            if turn.progress:
                state.turns_without_progress = 0
            else:
                state.turns_without_progress += 1

        trace.citations = citations(text)
        trace_id = self._save(turn, text, start)
        return AgentReply(
            text=text, status=state.status, trace_id=trace_id, citations=self._citations(turn, text)
        )

    def _run(self, turn: _Turn) -> str:
        state = turn.state
        if wants_human(turn.customer_message):
            self._record(turn, _decision("human_request", "escalate", "customer asked for a human"))
            return self._escalate(turn, "customer asked for a human")
        progress = check_progress(state, self.config)
        self._record(turn, progress)
        if progress.action == "escalate":
            return self._escalate(turn, progress.reason)

        if state.aop_id is None:
            self._route(turn)
        turn.messages = [*turn.history, Msg(role="user", content=turn.customer_message)]
        return self._loop(turn)

    def _route(self, turn: _Turn) -> bool:
        """Route the current message. Returns True if a (new) procedure became active."""
        turn.llm.purpose = "route"
        decision = self.router.route(turn.customer_message, turn.history, llm=turn.llm)
        turn.trace.route = decision
        turn.trace.prompt_versions["router"] = decision.prompt_version
        if decision.aop_id is None or decision.aop_id == turn.state.aop_id:
            return False
        aop = self.bundle.get(decision.aop_id)
        turn.state.start_procedure(aop.id, aop.version)
        self._note_aop(turn)
        return True

    def _loop(self, turn: _Turn) -> str:
        for _ in range(MAX_LLM_CALLS):
            aop, step = self._active(turn.state)
            if aop is not None:
                turn.procedures[aop.id] = aop
                turn.knowledge_procedure |= _answers_from_help_center(aop)
            specs: list[ToolSpec] = []
            if aop is not None and step is not None and turn.escalation is None:
                specs = [self.registry.get(n).to_llm_spec() for n in self._allowed(aop, step)]
                specs += _control_tools(aop, step)
            turn.llm.purpose = "agent"
            resp = turn.llm.complete(
                system=self._system(turn.state, step), messages=turn.messages, tools=specs or None
            )
            turn.messages.append(resp.as_assistant_msg())
            if resp.stop_reason == "refusal":
                return self._escalate(turn, "the model declined to answer")
            if not resp.tool_calls:
                final = resp.text.strip()
                if final and any(final in d for d in turn.drafts):
                    final = ""  # the model repeated text that is already being sent
                return self._finish(turn, "\n\n".join([*turn.drafts, final]).strip())
            results = [self._dispatch(turn, call) for call in resp.tool_calls]
            content: list[ContentBlock] = list(results)
            # Text written alongside tool calls is often the answer itself, sent together with
            # `advance_step(RESOLVE)`. It was written *before* the tool results, so it's kept
            # only if every call in that response succeeded; a draft next to a failed or
            # blocked call ("I've cancelled it" + a cancel that failed) is discarded.
            if resp.text.strip() and not any(r.is_error for r in results):
                turn.drafts.append(resp.text.strip())
                content.append(TextBlock(text=DRAFT_KEPT_NOTE))
            turn.messages.append(Msg(role="user", content=content))
            if turn.forced_escalation and turn.escalation:
                return self._escalate(turn, turn.escalation)
        reason = f"no reply after {MAX_LLM_CALLS} model calls"
        self._record(turn, _decision("turn_budget", "escalate", reason))
        return self._escalate(turn, f"the agent couldn't finish the turn in {MAX_LLM_CALLS} calls")

    # ------------------------------------------------------------------ tool dispatch

    def _dispatch(self, turn: _Turn, call: ToolCall) -> ToolResultBlock:
        if call.name == ADVANCE_TOOL:
            return self._advance(turn, call)
        if call.name == SWITCH_TOOL:
            return self._switch(turn, call)
        if turn.escalation is not None:
            decision = GuardrailDecision(
                guardrail="escalated",
                action="block",
                reason="the conversation is being handed to a teammate",
                tool=call.name,
            )
            return self._blocked_call(turn, call, decision)

        state = turn.state
        aop, step = self._active(state)
        allowed = self._allowed(aop, step) if aop and step else []
        decision = check_tool_call(
            call.name, call.input, state, allowed, self.registry, self.config
        )
        if decision.action == "allow" and turn.store_calls >= MAX_TOOL_CALLS:
            decision = GuardrailDecision(
                guardrail="turn_budget",
                action="block",
                reason=f"max {MAX_TOOL_CALLS} tool calls per turn; reply to the customer now",
                tool=call.name,
            )
        if decision.action != "allow":
            return self._blocked_call(turn, call, decision)
        self._record(turn, decision)

        turn.store_calls += 1
        ctx = ToolContext(customer_id=state.customer_ref if state.verified else None)
        if call.name == "create_refund":
            refund = self._refund_guardrail(turn, call, ctx)
            if refund is not None and refund.action != "allow":
                return self._blocked_call(turn, call, refund)

        start = time.perf_counter()
        outcome = self.tools.execute(call.name, call.input, ctx)
        latency = round((time.perf_counter() - start) * 1000, 1)
        self._observe(turn, call, outcome)
        turn.trace.tool_calls.append(
            ToolCallTrace(
                name=call.name,
                args=call.input,
                ok=outcome.ok,
                result=outcome.output.model_dump(mode="json") if outcome.output else None,
                error_code=outcome.error.code if outcome.error else None,
                latency_ms=latency,
            )
        )
        if outcome.output is not None:
            return _result(call, outcome.output.model_dump(mode="json"), is_error=False)
        assert outcome.error is not None
        error: dict[str, object] = {
            "code": outcome.error.code,
            "message": outcome.error.customer_safe_message,
        }
        if outcome.validation_errors:
            error["details"] = outcome.validation_errors
        return _result(call, {"error": error}, is_error=True)

    def _blocked_call(
        self, turn: _Turn, call: ToolCall, decision: GuardrailDecision
    ) -> ToolResultBlock:
        self._record(turn, decision)
        turn.trace.tool_calls.append(
            ToolCallTrace(name=call.name, args=call.input, ok=False, blocked_by=decision.guardrail)
        )
        turn.tried.append(f"{call.name}: blocked by {decision.guardrail}")
        if decision.action == "escalate":
            turn.escalation = decision.reason
            turn.forced_escalation = True
        log.info(
            "guardrail_blocked",
            conversation_id=turn.state.conversation_id,
            guardrail=decision.guardrail,
            tool=call.name,
            action=decision.action,
        )
        return _blocked(call, decision)

    def _refund_guardrail(
        self, turn: _Turn, call: ToolCall, ctx: ToolContext
    ) -> GuardrailDecision | None:
        """Check the refund against the limits and the order's live balance. None means the
        arguments are invalid, which the tool call itself reports as INVALID_INPUT."""
        try:
            args = s.CreateRefundIn.model_validate(call.input)
        except ValidationError:
            return None
        lookup_args: dict[str, object] = {"order_number": args.order_number}
        start = time.perf_counter()
        lookup = self.tools.execute("get_order", lookup_args, ctx)
        turn.trace.tool_calls.append(
            ToolCallTrace(
                name="get_order",
                args=lookup_args,
                ok=lookup.ok,
                error_code=lookup.error.code if lookup.error else None,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                source="guardrail",
            )
        )
        balance = None
        if isinstance(lookup.output, s.Order):
            balance = OrderBalance(lookup.output.refundable_cents, lookup.output.refunded_cents)
        decision = check_refund(args.amount_cents, balance, turn.state, self.config)
        if decision.action == "allow":
            self._record(turn, decision)
        return decision

    def _observe(self, turn: _Turn, call: ToolCall, outcome: ToolOutcome) -> None:
        """Update state from a tool result. Identity changes only happen here, in code."""
        state, out = turn.state, outcome.output
        turn.tried.append(f"{call.name}: {outcome.error.code if outcome.error else 'ok'}")
        if isinstance(out, s.CustomerLookupOut):
            state.lookup_candidates.update({c.id: c.first_name for c in out.customers})
        elif isinstance(out, s.VerifyCustomerOut):
            args = s.VerifyCustomerIn.model_validate(call.input)
            if out.verified:
                state.verified, state.customer_ref = True, args.customer_id
                state.customer_name = state.lookup_candidates.get(args.customer_id)
                turn.progress = True
            else:
                key = credential_key(args.order_number, args.zip)
                if key not in state.failed_verifications:
                    state.failed_verifications.append(key)
                attempts = check_verification_attempts(state, self.config)
                self._record(turn, attempts)
                if attempts.action == "escalate":
                    turn.escalation, turn.forced_escalation = attempts.reason, True
        elif isinstance(out, s.Order):
            state.remember_order(out.order_number, has_refunds=bool(out.refunds))
            state.slots["order_number"] = out.order_number
            if call.name == "get_order" and out.refunds:
                turn.reported_refunds = True
                turn.refund_amounts.update(r.amount_cents for r in out.refunds)
        elif isinstance(out, s.OrderListOut):
            for summary in out.orders:
                state.remember_order(summary.order_number)
        elif isinstance(out, s.ReturnAuthorization):
            state.slots["rma_number"] = out.rma_number
        elif isinstance(out, s.Subscription):
            state.slots["subscription_id"] = out.id

        if isinstance(out, s.SearchKnowledgeOut):
            turn.searched = True
            turn.kb_results.extend(out.results)
            state.kb_evidence = [r.chunk_id for r in out.results]
            turn.trace.knowledge_searches.append(
                KnowledgeSearchTrace(
                    query=str(call.input.get("query", ""))[:300],
                    covered=out.covered,
                    articles=[r.article_id for r in out.results],
                    chunk_ids=[r.chunk_id for r in out.results],
                )
            )
        elif out is not None:  # account data the reply may legitimately state
            data = json.dumps(out.model_dump(mode="json"), default=str)
            turn.account_evidence.append(
                Evidence(source="account", title=call.name, text=data[:ACCOUNT_EVIDENCE_CHARS])
            )

        tool = self.registry.get(call.name)
        if outcome.ok and tool.side_effect:
            turn.progress = True
            turn.completed.append(_done(call, out))
        if outcome.ok and call.name in REFUND_TOOLS:
            if call.name == "create_refund":
                amount = s.CreateRefundIn.model_validate(call.input).amount_cents
                state.auto_refunded_cents += amount
                turn.refund_amounts.add(amount)
                turn.refund_succeeded = True
            elif isinstance(out, s.Order) and out.refunds:
                # Cancelling refunds whatever was charged. That's the cancel policy, not a
                # discretionary refund, so it isn't counted against the auto-refund limit;
                # cancel_order is only allowed on the procedure's confirmation step.
                turn.refund_succeeded = True
                turn.refund_amounts.update(r.amount_cents for r in out.refunds)

    # ------------------------------------------------------------------ control tools

    def _advance(self, turn: _Turn, call: ToolCall) -> ToolResultBlock:
        state = turn.state
        aop, step = self._active(state)
        target = _parse_target(call.input.get("target", ""))
        if aop is None or step is None:
            decision = GuardrailDecision(
                guardrail="step_transition", action="block", reason="no procedure is active"
            )
            return self._blocked_call(turn, call, decision)
        exits = {t.target for t in step.transitions}
        if target is None or target not in exits:
            allowed = ", ".join(target_label(t) for t in exits)
            decision = GuardrailDecision(
                guardrail="step_transition",
                action="block",
                reason=(
                    f"{aop.ref} step {step.number} -> {call.input.get('target')!r} is not a "
                    f"transition in the procedure; allowed: {allowed}"
                ),
            )
            log.warning(
                "invalid_step_transition",
                conversation_id=state.conversation_id,
                aop=aop.ref,
                step=step.number,
                target=redact(str(call.input.get("target")))[:40],
            )
            return self._blocked_call(turn, call, decision)

        reason = f"{aop.ref} step {step.number} -> {target_label(target)}"
        self._record(turn, _decision("step_transition", "allow", reason))
        # Only forward moves count as progress, so bouncing between steps can't keep a
        # stuck conversation away from the no-progress escalation.
        if isinstance(target, Terminal) or target > step.number:
            turn.progress = True
        turn.trace.tool_calls.append(ToolCallTrace(name=call.name, args=call.input, ok=True))
        if target == Terminal.RESOLVE:
            state.end_procedure()
            state.status = "resolved"
            message = "Procedure resolved. Reply to the customer to wrap up."
        elif target == Terminal.ESCALATE:
            turn.escalation = str(call.input.get("reason") or f"{aop.name} needs a teammate")
            message = (
                "Escalation recorded; a teammate will take over. Tell the customer, briefly, "
                "that a teammate will follow up here and why."
            )
        else:
            assert isinstance(target, int)
            state.current_step = target
            message = f"Now on step {target}. Its instructions are in the system prompt."
        return _result(call, {"ok": True, "message": message}, is_error=False)

    def _switch(self, turn: _Turn, call: ToolCall) -> ToolResultBlock:
        if turn.switched:
            decision = GuardrailDecision(
                guardrail="switch_limit", action="block", reason="already re-routed this turn"
            )
            return self._blocked_call(turn, call, decision)
        turn.switched = True
        before = turn.state.aop_ref
        changed = self._route(turn)
        turn.trace.tool_calls.append(ToolCallTrace(name=call.name, args=call.input, ok=True))
        if changed:
            message = f"Switched from {before} to {turn.state.aop_ref}; now on step 1."
        else:
            message = "No other procedure fits; continue with the current one."
        return _result(call, {"ok": True, "message": message}, is_error=False)

    # ------------------------------------------------------------------ reply + escalation

    def _finish(self, turn: _Turn, text: str) -> str:
        text = normalize_citations(text)
        ctx = ReplyContext(
            channel=turn.state.channel,
            refund_succeeded_this_turn=turn.refund_succeeded,
            owned_orders=set(turn.state.owned_orders) if turn.state.verified else set(),
            reported_refunds_this_turn=turn.reported_refunds,
            customer_text=turn.customer_text,
            refund_amounts_cents=turn.refund_amounts,
        )
        check = check_reply(text, ctx, self.config)
        turn.trace.guardrails.extend(check.decisions)
        if not text.strip():
            check.blocked_reason = "the reply was empty"
        if check.blocked_reason is None:
            final = check.text
        else:
            turn.trace.rejected_replies.append(text)
            final = self._rewrite(turn, ctx, check.blocked_reason)
        if turn.trace.reply_source != "fallback" and self._needs_grounding(turn, final):
            final = self._ground(turn, ctx, final)
        if turn.escalation is not None:  # the procedure said ESCALATE; the model wrote the reply
            self._handoff(turn, turn.escalation)
        return final

    # ------------------------------------------------------------------ grounding

    @staticmethod
    def _needs_grounding(turn: _Turn, text: str) -> bool:
        return turn.searched or turn.knowledge_procedure or bool(citations(text))

    def _ground(self, turn: _Turn, ctx: ReplyContext, text: str) -> str:
        """Check the reply's claims against the evidence; see `baton.knowledge.grounding`."""
        turn.llm.purpose = "grounding"
        result = self.grounding.check(
            text, self._evidence(turn), turn.customer_message, llm=turn.llm
        )
        outcome = result.outcome
        turn.trace.grounding.append(outcome)
        turn.trace.prompt_versions["grounding"] = outcome.prompt_version
        action: Action = "allow" if outcome.action == "pass" else "rewrite"
        self._record(turn, _decision("grounding", action, f"{outcome.action}: {outcome.reason}"))
        if result.text == text:
            return text
        if outcome.action != "pass":
            turn.trace.rejected_replies.append(text)
        new_text = result.text
        if outcome.action == "abstained":
            turn.trace.reply_source = "abstention"
            # "I'm not sure" must not hide what really happened this turn: a teammate is
            # taking over, or the store confirmed an action. Both are runtime-verified facts.
            if turn.escalation is not None:
                new_text = ESCALATION_REPLY
            elif turn.completed:
                new_text = f"I've {'; and '.join(turn.completed)}. {HEDGE_NOTE}"
        # Never truncate away the offer of a teammate on a short channel: cut the body instead.
        limit = self.config.max_reply_chars.get(ctx.channel, 1200)
        if len(new_text) > limit and new_text.endswith(HEDGE_NOTE):
            body = new_text[: -len(HEDGE_NOTE)].rstrip()
            new_text = f"{truncate(body, max(limit - len(HEDGE_NOTE) - 2, 1))}\n\n{HEDGE_NOTE}"
        # The edited text goes through the deterministic reply checks again (length, PII).
        check = check_reply(new_text, ctx, self.config)
        if check.blocked_reason is not None:  # edits only remove sentences or add fixed text,
            turn.trace.reply_source = "fallback"  # so this is a safety net, not a code path
            return FALLBACK_REPLY
        return check.text

    def _evidence(self, turn: _Turn) -> list[Evidence]:
        """Everything a help-center answer may rely on this turn."""
        evidence: list[Evidence] = []
        seen: set[int] = set()
        for r in turn.kb_results:
            if r.chunk_id not in seen:
                seen.add(r.chunk_id)
                evidence.append(
                    Evidence(f"kb:{r.article_id}", r.title, f"{r.section}\n{r.text}", r.updated_at)
                )
        # Passages from the previous turn's search, so a follow-up answer stays checkable.
        for chunk_id in turn.previous_kb_evidence:
            chunk = self._kb_chunk(chunk_id) if chunk_id not in seen else None
            if chunk is not None:
                seen.add(chunk_id)
                text = f"{chunk.section}\n{chunk.text}"
                evidence.append(
                    Evidence(f"kb:{chunk.article_id}", chunk.title, text, chunk.updated_at)
                )
        evidence.extend(turn.account_evidence)
        evidence.append(Evidence("state", "Conversation state", turn.state.summary()))
        for aop in turn.procedures.values():
            if aop.policies:
                evidence.append(Evidence("procedure", aop.name, "\n".join(aop.policies)))
        return evidence

    def _kb_chunk(self, chunk_id: int) -> IndexedChunk | None:
        if self.knowledge is None:
            return None
        try:
            return self.knowledge.chunk(chunk_id)
        except KnowledgeIndexError:
            return None

    def _citations(self, turn: _Turn, text: str) -> list[Citation]:
        """Citations for articles the agent actually retrieved (this turn or the previous
        search). Grounding already strips any other tag; this doesn't rely on that."""
        if self.knowledge is None:
            return []
        retrieved = {r.article_id for r in turn.kb_results}
        for chunk_id in turn.previous_kb_evidence:
            chunk = self._kb_chunk(chunk_id)
            if chunk is not None:
                retrieved.add(chunk.article_id)
        out = []
        for article_id in citations(text):
            try:
                title = self.knowledge.title(article_id) if article_id in retrieved else None
            except KnowledgeIndexError:
                title = None
            if title is not None:
                out.append(
                    Citation(article_id=article_id, title=title, url=self.knowledge.url(article_id))
                )
        return out

    def _rewrite(self, turn: _Turn, ctx: ReplyContext, reason: str) -> str:
        turn.messages.append(Msg(role="user", content=REWRITE_INSTRUCTION.format(reason=reason)))
        turn.llm.purpose = "rewrite"
        _, step = self._active(turn.state)
        resp = turn.llm.complete(self._system(turn.state, step), turn.messages, tools=None)
        check = check_reply(resp.text, ctx, self.config)
        turn.trace.guardrails.extend(check.decisions)
        if check.blocked_reason is not None:
            turn.trace.rejected_replies.append(resp.text)
        if check.blocked_reason is None and resp.text.strip():
            turn.trace.reply_source = "rewrite"
            return check.text
        turn.trace.reply_source = "fallback"
        return FALLBACK_REPLY

    def _escalate(self, turn: _Turn, reason: str) -> str:
        """A guardrail-driven escalation: the runtime writes the customer's reply."""
        turn.trace.reply_source = "escalation"
        self._handoff(turn, reason)
        return ESCALATION_REPLY

    def _handoff(self, turn: _Turn, reason: str) -> None:
        state = turn.state
        aop = self.bundle.aops.get(state.aop_id) if state.aop_id else None
        state.status = "escalated"
        state.escalation_reason = reason
        turn.trace.escalation_reason = reason
        transcript = [
            ("customer" if m.role == "user" else "agent", m.content)
            for m in turn.history
            if isinstance(m.content, str)
        ] + [("customer", turn.customer_message)]
        turn.llm.purpose = "handoff"
        result = build_handoff(
            turn.llm,
            state,
            transcript,
            turn.tried,
            reason,
            aop.name if aop else None,
            self.prompts_dir,
        )
        state.handoff = result.summary
        turn.trace.prompt_versions["handoff"] = result.prompt_version

    # ------------------------------------------------------------------ helpers

    def _active(self, state: ConversationState) -> tuple[CompiledAOP | None, CompiledStep | None]:
        if state.aop_id is None or state.current_step is None:
            return None, None
        aop = self.bundle.aops.get(state.aop_id)
        if aop is None:  # the AOP was removed since this conversation started
            state.end_procedure()
            return None, None
        return aop, aop.step(state.current_step)

    def _allowed(self, aop: CompiledAOP, step: CompiledStep) -> list[str]:
        return allowed_tools(aop, step, self.registry)

    def _system(self, state: ConversationState, step: CompiledStep | None) -> str:
        return self.agent_prompt.render(
            brand=self.brand_prompt.text.strip(),
            refund_limit=_dollars(self.config.refund_auto_limit_cents),
            state=state.summary(),
            procedure=step.prompt if step else NO_PROCEDURE,
        )

    def _record(self, turn: _Turn, decision: GuardrailDecision) -> None:
        turn.trace.guardrails.append(decision)

    def _note_aop(self, turn: _Turn) -> None:
        aop = self.bundle.aops.get(turn.state.aop_id) if turn.state.aop_id else None
        if aop is not None:
            turn.trace.aop_ref = aop.ref
            turn.trace.aop_source_hash = aop.source_hash
            turn.trace.step_prompt_version = aop.step_prompt_version

    @staticmethod
    def _message_rows(session: Session, conversation_id: str) -> list[tuple[str, str]]:
        rows = session.execute(
            select(Message.role, Message.content)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id)
        ).all()
        return [(role, content) for role, content in rows]

    @staticmethod
    def _llm_history(rows: Sequence[tuple[str, str]]) -> list[Msg]:
        """Earlier messages as alternating user/assistant text (tool calls from earlier turns
        aren't replayed; the state summary carries what matters)."""
        msgs: list[Msg] = []
        for role, text in rows[-HISTORY_MESSAGES:]:
            if role == MessageRole.SYSTEM.value:
                continue
            llm_role = "user" if role == MessageRole.CUSTOMER.value else "assistant"
            if msgs and msgs[-1].role == llm_role and isinstance(msgs[-1].content, str):
                msgs[-1] = Msg(role=llm_role, content=f"{msgs[-1].content}\n\n{text}")
            elif msgs or llm_role == "user":
                msgs.append(Msg(role=llm_role, content=text))
        return msgs

    def _save(self, turn: _Turn, text: str, start: float) -> str:
        state, trace = turn.state, turn.trace
        trace.step_after = state.current_step
        trace.status = state.status
        trace.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        with session_scope(self.sessions) as session:
            reply = Message(
                conversation_id=state.conversation_id, role=MessageRole.AGENT.value, content=text
            )
            session.add(reply)
            if trace.escalation_reason and state.handoff:  # escalated during this turn
                h = state.handoff
                note = (
                    f"Handoff: {h.customer}. Wants: {h.intent}. Tried: {'; '.join(h.tried)}. "
                    f"Needed: {h.needed}"
                )
                session.add(
                    Message(
                        conversation_id=state.conversation_id,
                        role=MessageRole.SYSTEM.value,
                        content=note,
                    )
                )
            session.flush()
            save_state(session, state)
            trace_id = save_trace(session, trace, reply.id)
            release_turn(session, state.conversation_id)
        log.info(
            "agent_turn",
            conversation_id=state.conversation_id,
            turn=trace.turn,
            aop=trace.aop_ref,
            step=f"{trace.step_before}->{trace.step_after}",
            status=state.status,
            tools=[t.name for t in trace.tool_calls],
            blocked=[g.guardrail for g in trace.guardrails if g.action != "allow"],
            input_tokens=trace.input_tokens,
            output_tokens=trace.output_tokens,
            latency_ms=trace.latency_ms,
            message_chars=len(turn.customer_message),
        )
        return trace_id
