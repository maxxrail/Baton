"""Deterministic guardrails. They run in code around the model, so no prompt can talk past them.

Two kinds:
- **Before a tool call** (`check_tool_call`, `check_refund`): may the model run this tool, now,
  with these arguments? A block becomes the tool's result ("verify the customer first"), so
  the model can recover; an escalation ends the turn and hands off to a human.
- **Before a reply is sent** (`check_reply`): the final text may not claim a refund that didn't
  happen, mention another customer's order, echo contact details the customer didn't give us,
  or exceed the channel's length cap. Claims and foreign orders block the reply (the agent
  gets one chance to rewrite it, then a safe fallback is sent); PII and length are rewritten
  in place.

Conversation-level checks (`wants_human`, `check_progress`, `check_verification_attempts`)
escalate stuck or abusive conversations.

Every function returns `GuardrailDecision`s, and the runtime writes every one to the trace.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from baton.aop.models import CompiledAOP, CompiledStep
from baton.runtime.redact import CARD_RE, EMAIL_RE, PHONE_RE, redact
from baton.runtime.state import Channel, ConversationState
from baton.tools.registry import ToolRegistry

Action = Literal["allow", "block", "rewrite", "escalate"]

# Tools usable at any step of any procedure: they only establish who the customer is.
IDENTITY_TOOLS = frozenset({"lookup_customer", "verify_customer"})
# The help-center search is public and read-only, so every procedure may use it (Part 03).
KNOWLEDGE_TOOL = "search_knowledge"
# Tools whose success means money went back to the customer this turn.
REFUND_TOOLS = frozenset({"create_refund", "cancel_order"})


class GuardrailDecision(BaseModel):
    guardrail: str
    action: Action
    reason: str
    tool: str | None = None


@dataclass(frozen=True)
class GuardrailConfig:
    refund_auto_limit_cents: int = 10_000
    max_failed_verifications: int = 3
    max_turns_without_progress: int = 12
    max_reply_chars: dict[str, int] = field(
        default_factory=lambda: {"web": 1200, "cli": 1200, "email": 4000, "sms": 480}
    )


def _allow(guardrail: str, reason: str, tool: str | None = None) -> GuardrailDecision:
    return GuardrailDecision(guardrail=guardrail, action="allow", reason=reason, tool=tool)


def _block(guardrail: str, reason: str, tool: str | None = None) -> GuardrailDecision:
    return GuardrailDecision(guardrail=guardrail, action="block", reason=reason, tool=tool)


def _escalate(guardrail: str, reason: str, tool: str | None = None) -> GuardrailDecision:
    return GuardrailDecision(guardrail=guardrail, action="escalate", reason=reason, tool=tool)


# ---------------------------------------------------------------------- before tool calls


def allowed_tools(
    aop: CompiledAOP | None, step: CompiledStep | None, registry: ToolRegistry
) -> list[str]:
    """Tools that may run right now: the current step's `@refs`, the identity tools, the
    read-only tools referenced anywhere in the active procedure (so the agent can look up an
    order the customer names at step 1 instead of forcing a step jump to read data), and the
    help-center search, which every procedure may use.

    Side-effecting tools are only ever allowed on the step that references them.
    """
    if aop is None or step is None:
        return []
    names = list(step.allowed_tools)
    for s in aop.steps:
        names.extend(t for t in s.allowed_tools if not registry.get(t).side_effect)
    if aop.requires_verification or any(registry.get(t).requires_verification for t in names):
        names.extend(sorted(IDENTITY_TOOLS))
    names.append(KNOWLEDGE_TOOL)
    return [n for n in dict.fromkeys(names) if n in registry]


def check_tool_call(
    name: str,
    args: dict[str, object],
    state: ConversationState,
    allowed: Iterable[str],
    registry: ToolRegistry,
    config: GuardrailConfig,
) -> GuardrailDecision:
    """Static checks before any store tool runs. The first failing check wins."""
    if name not in registry:
        return _block("unknown_tool", f"{name!r} is not a tool", name)
    allowed = list(allowed)
    if name not in allowed:
        where = f"{state.aop_ref} step {state.current_step}" if state.aop_id else "no procedure"
        return _block(
            "tool_allowlist",
            f"{name} is not allowed at {where}; allowed now: {', '.join(allowed) or 'none'}",
            name,
        )
    tool = registry.get(name)
    if tool.requires_verification and not state.verified:
        return _block(
            "identity_gate",
            f"{name} needs a verified customer; verify identity with verify_customer first",
            name,
        )
    if name == "verify_customer":
        if len(state.failed_verifications) >= config.max_failed_verifications:
            return _escalate(
                "verification_attempts",
                f"{len(state.failed_verifications)} failed verification attempts",
                name,
            )
        if state.verified and str(args.get("customer_id", "")).strip() != state.customer_ref:
            return _block(
                "identity_switch",
                "this conversation is already verified for another account",
                name,
            )
    return _allow("pre_tool", "passed allow-list, identity and scope checks", name)


@dataclass(frozen=True)
class OrderBalance:
    """What the store says about the order right now, read just before the refund."""

    refundable_cents: int
    refunded_cents: int


def check_refund(
    amount_cents: int,
    order: OrderBalance | None,
    state: ConversationState,
    config: GuardrailConfig,
) -> GuardrailDecision:
    """Auto-approve a refund only if it is within the limit on its own, within the limit
    together with this conversation's earlier refunds, within the limit together with refunds
    already on the order (so the same order can't be split across conversations), and within
    the order's remaining balance. Over a limit -> a human decides.

    Fails closed: if the order couldn't be read (`order is None`), the refund is blocked.
    The amount-only checks run first, so a store outage never lets a big refund through.
    """
    limit = config.refund_auto_limit_cents
    tool = "create_refund"
    if amount_cents > limit:
        return _escalate(
            "refund_limit",
            f"refund of {amount_cents} cents is over the {limit}-cent auto-approval limit",
            tool,
        )
    total = state.auto_refunded_cents + amount_cents
    if total > limit:
        return _escalate(
            "refund_limit",
            f"refunds this conversation would total {total} cents, over the {limit}-cent limit",
            tool,
        )
    if order is None:
        return _block(
            "refund_unverifiable",
            "the order's balance couldn't be read, so the refund can't be checked; try again",
            tool,
        )
    on_order = order.refunded_cents + amount_cents
    if on_order > limit:
        return _escalate(
            "refund_limit",
            f"refunds on this order would total {on_order} cents, over the {limit}-cent limit",
            tool,
        )
    if amount_cents > order.refundable_cents:
        return _escalate(
            "refund_balance",
            f"refund of {amount_cents} cents exceeds the {order.refundable_cents} cents still "
            "refundable on the order",
            tool,
        )
    return _allow("refund_limit", f"{amount_cents} cents within limits and balance", tool)


# ---------------------------------------------------------------------- conversation level

_HUMAN = re.compile(
    r"\b(?:talk|speak|chat)\s+(?:to|with)\s+(?:a\s+|an\s+|your\s+|some\s*one\s+|a\s+real\s+)?"
    r"(?:human|person|someone|real\s+person|agent|representative|rep|manager|supervisor"
    r"|teammate)\b"
    r"|\b(?:human|live|real)\s+(?:agent|person|support|representative)\b"
    r"|\b(?:get|transfer|pass|put|connect|hand)\s+me\s+(?:to|through\s+to|over\s+to|with)?\s*"
    r"(?:a\s+|an\s+|the\s+|your\s+)?(?:human|person|agent|representative|teammate|manager)\b"
    r"|^\s*(?:human|agent|representative)\s*[.!?]*\s*$"
    # Spanish and French, the other languages in the simulation suite.
    r"|\bhablar\s+con\s+(?:una\s+persona|un\s+agente|alguien|un\s+humano|un\s+compañero)"
    r"|\bparler\s+(?:à|a|avec)\s+(?:une\s+personne|un\s+conseiller|quelqu'un|un\s+humain)",
    re.IGNORECASE,
)


def wants_human(message: str) -> bool:
    """An explicit request for a person. Deliberately literal: the cost of a false positive is
    a human reading a chat, the cost of a false negative is a customer trapped with a bot."""
    return bool(_HUMAN.search(message))


_CLOSING = re.compile(
    r"\b(?:thanks|thank\s+you|thx|ty|that'?s\s+all|that'?s\s+it|all\s+set|bye|goodbye"
    r"|nothing\s+else|gracias|eso\s+es\s+todo|adi[oó]s|merci|c'est\s+tout|au\s+revoir)\b",
    re.IGNORECASE,
)
CLOSING_MAX_WORDS = 25


def is_closing(message: str) -> bool:
    """A goodbye ("thanks, that's all"), not a new request: short, no question, and a closing
    phrase. Used to keep a resolved conversation resolved."""
    if "?" in message or len(message.split()) > CLOSING_MAX_WORDS:
        return False
    return bool(_CLOSING.search(message))


def check_progress(state: ConversationState, config: GuardrailConfig) -> GuardrailDecision:
    if state.turns_without_progress >= config.max_turns_without_progress:
        return _escalate(
            "no_progress", f"{state.turns_without_progress} turns without progress on the request"
        )
    return _allow("no_progress", f"{state.turns_without_progress} turns without progress")


def check_verification_attempts(
    state: ConversationState, config: GuardrailConfig
) -> GuardrailDecision:
    failed = len(state.failed_verifications)
    if failed >= config.max_failed_verifications:
        return _escalate("verification_attempts", f"{failed} failed verification attempts")
    return _allow("verification_attempts", f"{failed} failed verification attempts")


# ---------------------------------------------------------------------- before the reply

# Two kinds of refund language:
# - an *action* claim or promise by the agent ("I've refunded you", "we'll issue a refund"):
#   allowed only if a refund tool (create_refund, or cancel_order, which refunds) succeeded in
#   this turn;
# - a *status* statement ("your refund was issued", "it has been refunded"): also allowed when
#   an order looked up this turn shows refunds on record (answering "where's my refund?").
# Conditional wording from the procedures ("the refund is issued once the item arrives") is
# neither, so it passes.
_AMOUNT = r"(?:full\s+|partial\s+)?"
_DID = r"(?:issued|processed|sent|approved|initiated)"
# The agent says it did, or will, give money back.
_REFUND_ACTION = re.compile(
    r"\b(?:I(?:'ve|\s+have)?|we(?:'ve|\s+have)?)\s+(?:just\s+|now\s+|already\s+)?"
    rf"(?:refunded|credited|reimbursed|{_DID}\s+(?:you\s+)?(?:a|the|your)\s+{_AMOUNT}"
    r"(?:refund|credit))"
    r"|\b(?:I|we)(?:'ll|\s+will)\s+(?:go\s+ahead\s+and\s+)?(?:refund|credit|reimburse)\b"
    r"|\b(?:I|we)(?:'ll|\s+will)\s+(?:issue|process|send|approve)\s+(?:you\s+)?(?:a|the|your)\s+"
    rf"{_AMOUNT}(?:refund|credit)",
    re.IGNORECASE,
)
# A promise to the customer, stated as certain. Conditional wording passes ("you'll be
# refunded once we receive the item" is the return procedure's own step 6).
_REFUND_PROMISE = re.compile(
    r"\byou(?:'ll|\s+will)\s+be\s+(?:refunded|credited|reimbursed)\b"
    rf"|\byou(?:'ll|\s+will)\s+(?:get|receive|see)\s+(?:a|your|the)\s+{_AMOUNT}"
    r"(?:refund|credit)\s+(?:of|for)\b",
    re.IGNORECASE,
)
# A statement that a refund happened. Also allowed when an order looked up this turn shows
# refunds on record (answering "where's my refund?"). Negated and conditional wording passes.
_REFUND_STATUS = re.compile(
    r"\b(?:refund|credit)\s+(?:has\s+been|was|is\s+now|is\s+on\s+(?:its|the)\s+way"
    r"|is\s+(?:processed|complete|completed|approved|done|issued)\b)"
    r"|\b(?:refund|credit)\s+(?:issued|processed|approved|completed)\b"
    r"|\b(?:has|have)\s+been\s+(?:refunded|credited|reimbursed)",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"\b(?:no|nothing|none|not\s+(?:a|any|a\s+single)|never\s+(?:a|any))\s+$", re.IGNORECASE
)
_CONDITIONAL = re.compile(r"\b(?:when|once|after|as\s+soon\s+as|if|until)\b", re.IGNORECASE)
_DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?")
_SENTENCE_END = re.compile(r"[.!?\n]")


def _sentence(text: str, match: re.Match[str]) -> tuple[str, str]:
    """(the sentence containing the match, the part of it after the match)."""
    starts = [m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())]
    begin = starts[-1] if starts else 0
    stop = _SENTENCE_END.search(text, match.end())
    end = stop.start() if stop else len(text)
    return text[begin:end], text[match.end() : end]


def _first(
    pattern: re.Pattern[str], text: str, *, negatable: bool, conditional_ok: bool
) -> re.Match[str] | None:
    for match in pattern.finditer(text):
        before = text[max(0, match.start() - 20) : match.start()]
        if negatable and _NEGATED.search(before):
            continue  # "No refund has been issued yet" is care, not a claim
        if conditional_ok and _CONDITIONAL.search(_sentence(text, match)[1]):
            continue  # "...is issued once we receive the item"
        return match
    return None


def _cents(match: re.Match[str]) -> int:
    whole, frac = match.group(1).replace(",", ""), match.group(2) or "00"
    return int(whole) * 100 + int(frac)


# Order numbers in any spelling the model might use: NW-10042, NW 10042, nw10042, #10042.
ORDER_NUMBER_RE = re.compile(r"(?:\bNW[-\s]?|#)(\d{4,6})\b", re.IGNORECASE)


def order_numbers(text: str) -> set[str]:
    """Canonical `NW-<digits>` for every order-number-like token in the text."""
    return {f"NW-{digits}" for digits in ORDER_NUMBER_RE.findall(text)}


@dataclass
class ReplyContext:
    channel: Channel
    refund_succeeded_this_turn: bool
    owned_orders: set[str]
    reported_refunds_this_turn: bool
    customer_text: str  # everything the customer typed in this conversation
    # Refund amounts that are real: issued this turn, or on an order looked up this turn.
    refund_amounts_cents: set[int] = field(default_factory=set)


@dataclass
class ReplyCheck:
    text: str
    decisions: list[GuardrailDecision]
    blocked_reason: str | None  # set when the reply must not be sent as written


def _refund_problem(text: str, ctx: ReplyContext) -> tuple[str | None, bool]:
    """(why the refund language can't be sent, whether the reply mentions a refund at all)."""
    action = _first(_REFUND_ACTION, text, negatable=False, conditional_ok=False)
    promise = _first(_REFUND_PROMISE, text, negatable=False, conditional_ok=True)
    status = _first(_REFUND_STATUS, text, negatable=True, conditional_ok=True)
    did = action or promise
    if did and not ctx.refund_succeeded_this_turn:
        return (
            f"reply claims or promises a refund ({did.group(0)!r}) but no refund tool "
            "succeeded this turn",
            True,
        )
    if status and not (ctx.refund_succeeded_this_turn or ctx.reported_refunds_this_turn):
        return (
            f"reply says a refund happened ({status.group(0)!r}) but no refund succeeded or "
            "was found on an order this turn",
            True,
        )
    for claim in (did, status):
        if claim is None:
            continue
        amounts = {_cents(m) for m in _DOLLARS.finditer(_sentence(text, claim)[0])}
        wrong = amounts - ctx.refund_amounts_cents
        if wrong:
            shown = ", ".join(f"${c / 100:.2f}" for c in sorted(wrong))
            return f"reply states a refund amount ({shown}) that no refund matches", True
    return None, bool(did or status)


def _foreign_orders(text: str, ctx: ReplyContext) -> list[str]:
    allowed = {o.upper() for o in ctx.owned_orders} | order_numbers(ctx.customer_text)
    return sorted(order_numbers(text) - allowed)


def _mask_unknown_pii(text: str, customer_text: str) -> tuple[str, list[str]]:
    """Mask emails/phones the customer didn't give us themselves, and any card number."""
    known_digits = {re.sub(r"\D", "", m.group(0))[-10:] for m in PHONE_RE.finditer(customer_text)}
    known_emails = {m.group(0).lower() for m in EMAIL_RE.finditer(customer_text)}
    masked: list[str] = []

    def email(m: re.Match[str]) -> str:
        if m.group(0).lower() in known_emails:
            return m.group(0)
        masked.append("email")
        return redact(m.group(0))

    def phone(m: re.Match[str]) -> str:
        if re.sub(r"\D", "", m.group(0))[-10:] in known_digits:
            return m.group(0)
        masked.append("phone")
        return "[phone]"

    def card(m: re.Match[str]) -> str:
        masked.append("card")
        return redact(m.group(0))

    text = CARD_RE.sub(card, text)  # before phones: a card number contains phone-like runs
    return PHONE_RE.sub(phone, EMAIL_RE.sub(email, text)), masked


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    if sentence_end > limit // 2:
        cut = cut[: sentence_end + 1]
    # Never end inside a bracket: a half citation tag ("[kb:shipping-intern") would be shown
    # raw instead of rendered.
    cut = re.sub(r"\s*\[[^\]\n]*$", "", cut)
    return cut.rstrip() + "…"


def check_reply(text: str, ctx: ReplyContext, config: GuardrailConfig) -> ReplyCheck:
    decisions: list[GuardrailDecision] = []

    reason, mentions_refund = _refund_problem(text, ctx)
    if reason:
        decisions.append(_block("refund_claim", reason))
        return ReplyCheck(text, decisions, reason)
    decisions.append(
        _allow(
            "refund_claim",
            "refund backed by a tool result" if mentions_refund else "no refund claim",
        )
    )

    foreign = _foreign_orders(text, ctx)
    if foreign:
        reason = f"reply mentions order(s) not on this customer's account: {', '.join(foreign)}"
        decisions.append(_block("foreign_order", reason))
        return ReplyCheck(text, decisions, reason)
    decisions.append(_allow("foreign_order", "every order number belongs to this customer"))

    text, masked = _mask_unknown_pii(text, ctx.customer_text)
    if masked:
        decisions.append(
            GuardrailDecision(
                guardrail="pii",
                action="rewrite",
                reason=f"masked {len(masked)} contact detail(s) the customer didn't provide",
            )
        )
    else:
        decisions.append(_allow("pii", "no unprovided contact details"))

    limit = config.max_reply_chars.get(ctx.channel, 1200)
    if len(text) > limit:
        text = truncate(text, limit)
        decisions.append(
            GuardrailDecision(
                guardrail="length",
                action="rewrite",
                reason=f"truncated to the {ctx.channel} cap of {limit} chars",
            )
        )
    else:
        decisions.append(_allow("length", f"{len(text)} <= {limit} chars"))
    return ReplyCheck(text, decisions, None)
