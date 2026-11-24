"""Unit tests for the deterministic guardrails (no LLM, no store)."""

from pathlib import Path

import pytest

from baton.aop.compiler import compile_aops
from baton.aop.models import AOPBundle
from baton.runtime.guardrails import (
    GuardrailConfig,
    OrderBalance,
    ReplyContext,
    allowed_tools,
    check_progress,
    check_refund,
    check_reply,
    check_tool_call,
    wants_human,
)
from baton.runtime.state import ConversationState
from baton.tools.registry import default_registry

REPO = Path(__file__).resolve().parents[2]
CONFIG = GuardrailConfig()
REGISTRY = default_registry()


@pytest.fixture(scope="module")
def bundle() -> AOPBundle:
    return compile_aops(REPO / "aops", prompts_dir=REPO / "prompts")


def _state(**kw: object) -> ConversationState:
    return ConversationState.model_validate({"conversation_id": "c1", "channel": "web", **kw})


def _ctx(**kw: object) -> ReplyContext:
    base: dict[str, object] = {
        "channel": "web",
        "refund_succeeded_this_turn": False,
        "owned_orders": {"NW-10002"},
        "reported_refunds_this_turn": False,
        "customer_text": "",
    }
    base.update(kw)
    return ReplyContext(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- allow-list


def test_allowed_tools_cancel_order_step_2_excludes_cancel(bundle: AOPBundle) -> None:
    aop = bundle.get("cancel_order")
    names = allowed_tools(aop, aop.step(2), REGISTRY)
    assert "get_order" in names
    assert "get_customer_orders" in names  # read-only, referenced at step 1
    assert {"lookup_customer", "verify_customer"} <= set(names)
    assert "cancel_order" not in names  # side effect only on step 4
    assert "cancel_order" in allowed_tools(aop, aop.step(4), REGISTRY)


def test_no_procedure_means_no_tools() -> None:
    assert allowed_tools(None, None, REGISTRY) == []


def test_general_question_offers_only_the_help_center(bundle: AOPBundle) -> None:
    aop = bundle.get("general_question")
    assert allowed_tools(aop, aop.step(1), REGISTRY) == ["search_knowledge"]


def test_every_step_of_every_procedure_can_search_the_help_center(bundle: AOPBundle) -> None:
    for aop in bundle.aops.values():
        for step in aop.steps:
            assert "search_knowledge" in allowed_tools(aop, step, REGISTRY), (aop.id, step.number)


def test_tool_not_on_step_is_blocked(bundle: AOPBundle) -> None:
    aop = bundle.get("return_item")
    state = _state(aop_id="return_item", aop_version=1, current_step=1, verified=True)
    decision = check_tool_call(
        "create_refund", {}, state, allowed_tools(aop, aop.step(1), REGISTRY), REGISTRY, CONFIG
    )
    assert decision.action == "block"
    assert decision.guardrail == "tool_allowlist"


def test_identity_gate_blocks_until_verified(bundle: AOPBundle) -> None:
    aop = bundle.get("where_is_my_order")
    allowed = allowed_tools(aop, aop.step(2), REGISTRY)
    unverified = _state(aop_id=aop.id, aop_version=1, current_step=2)
    blocked = check_tool_call("get_order", {}, unverified, allowed, REGISTRY, CONFIG)
    assert (blocked.action, blocked.guardrail) == ("block", "identity_gate")
    verified = unverified.model_copy(update={"verified": True, "customer_ref": "cus_0001"})
    assert check_tool_call("get_order", {}, verified, allowed, REGISTRY, CONFIG).action == "allow"


def test_unknown_tool_blocked() -> None:
    decision = check_tool_call("drop_tables", {}, _state(), ["drop_tables"], REGISTRY, CONFIG)
    assert decision.guardrail == "unknown_tool"


def test_cannot_switch_identity_after_verification(bundle: AOPBundle) -> None:
    aop = bundle.get("refund_request")
    state = _state(verified=True, customer_ref="cus_0001", aop_id=aop.id, current_step=1)
    allowed = allowed_tools(aop, aop.step(1), REGISTRY)
    args = {"customer_id": "cus_0002", "order_number": "NW-10004", "zip": "78701"}
    decision = check_tool_call("verify_customer", args, state, allowed, REGISTRY, CONFIG)
    assert decision.guardrail == "identity_switch"


def test_fourth_verification_attempt_escalates(bundle: AOPBundle) -> None:
    aop = bundle.get("refund_request")
    state = _state(failed_verifications=["a", "b", "c"], aop_id=aop.id, current_step=1)
    allowed = allowed_tools(aop, aop.step(1), REGISTRY)
    decision = check_tool_call("verify_customer", {}, state, allowed, REGISTRY, CONFIG)
    assert decision.action == "escalate"


# ---------------------------------------------------------------- refunds


@pytest.mark.parametrize(
    ("amount", "refundable", "on_order", "already", "action", "guardrail"),
    [
        (10_000, 20_000, 0, 0, "allow", "refund_limit"),
        (10_001, 20_000, 0, 0, "escalate", "refund_limit"),
        (25_000, 30_000, 0, 0, "escalate", "refund_limit"),
        (6_000, 20_000, 0, 5_000, "escalate", "refund_limit"),  # conversation total
        (6_000, 20_000, 5_000, 0, "escalate", "refund_limit"),  # order total (other chats)
        (5_000, 4_000, 0, 0, "escalate", "refund_balance"),
    ],
)
def test_refund_limits(
    amount: int, refundable: int, on_order: int, already: int, action: str, guardrail: str
) -> None:
    order = OrderBalance(refundable_cents=refundable, refunded_cents=on_order)
    decision = check_refund(amount, order, _state(auto_refunded_cents=already), CONFIG)
    assert (decision.action, decision.guardrail) == (action, guardrail)


def test_refund_limit_is_configurable() -> None:
    order = OrderBalance(refundable_cents=10_000, refunded_cents=0)
    config = GuardrailConfig(refund_auto_limit_cents=2_500)
    assert check_refund(5_000, order, _state(), config).action == "escalate"


def test_refund_fails_closed_when_the_order_cant_be_read() -> None:
    small = check_refund(1_000, None, _state(), CONFIG)
    assert (small.action, small.guardrail) == ("block", "refund_unverifiable")
    # A big refund still escalates on the amount alone; the outage doesn't matter.
    assert check_refund(500_000, None, _state(), CONFIG).action == "escalate"


# ---------------------------------------------------------------- conversation level


@pytest.mark.parametrize(
    "message",
    [
        "I want to talk to a human",
        "can I speak with a real person please",
        "Let me talk to your manager",
        "get me a representative",
        "AGENT",
        "is there live support?",
    ],
)
def test_wants_human(message: str) -> None:
    assert wants_human(message)


@pytest.mark.parametrize(
    "message",
    ["where is my order", "the agent earlier said it shipped", "I'm a human being, obviously"],
)
def test_does_not_want_human(message: str) -> None:
    assert not wants_human(message)


def test_no_progress_escalates_at_limit() -> None:
    assert check_progress(_state(turns_without_progress=11), CONFIG).action == "allow"
    assert check_progress(_state(turns_without_progress=12), CONFIG).action == "escalate"


# ---------------------------------------------------------------- replies


@pytest.mark.parametrize(
    "text",
    [
        "I've refunded you $250.",
        "I have issued a full refund to your card.",
        "Good news, your refund has been processed.",
        "Your refund was issued today.",
        "I'll refund you right away.",
        "We will issue you a refund for the mug.",
        "You'll receive a refund of $40 in 5 days.",
        "The order has been refunded.",
        "I've credited your account.",
        "No problem your refund has been processed.",  # "no" not negating the claim
        # Found by the code review: phrasings the first patterns missed.
        "I've processed your refund of $250.",
        "Your refund is processed.",
        "You'll be refunded $250.",
        "Refund issued!",
        "We have approved a full refund.",
    ],
)
def test_unbacked_refund_claims_block(text: str) -> None:
    check = check_reply(text, _ctx(), CONFIG)
    assert check.blocked_reason is not None
    assert check.decisions[-1].guardrail == "refund_claim"


@pytest.mark.parametrize(
    "text",
    [
        "Your refund is issued when we receive the item.",
        "You'll be refunded once we receive the mugs.",
        "Refunds up to $100 can be issued directly.",
        "A teammate needs to approve a refund that size.",
        "Would you like a refund or an exchange?",
        "No refund has been issued yet.",  # seen live: careful, not a claim
        "Your refund hasn't been processed yet.",
        "Not a single refund has been made on this order.",
        "I haven't issued the refund yet, so nothing has been refunded.",  # seen live
    ],
)
def test_conditional_refund_language_is_allowed(text: str) -> None:
    assert check_reply(text, _ctx(), CONFIG).blocked_reason is None


def test_refund_claim_allowed_when_tool_succeeded() -> None:
    ctx = _ctx(refund_succeeded_this_turn=True, refund_amounts_cents={2000})
    assert check_reply("I've refunded you $20.", ctx, CONFIG).blocked_reason is None
    assert check_reply("I've refunded you $20.00.", ctx, CONFIG).blocked_reason is None


def test_refund_amount_must_match_a_real_refund() -> None:
    # A $50 refund doesn't back "I've refunded $500".
    ctx = _ctx(refund_succeeded_this_turn=True, refund_amounts_cents={5000})
    check = check_reply("I've refunded $500 to your card.", ctx, CONFIG)
    assert check.blocked_reason is not None
    assert "$500.00" in check.blocked_reason


def test_reporting_an_existing_refund_is_allowed() -> None:
    ctx = _ctx(reported_refunds_this_turn=True, refund_amounts_cents={4000})
    check = check_reply("Your refund of $40.00 was issued on June 7.", ctx, CONFIG)
    assert check.blocked_reason is None


def test_existing_refund_does_not_back_a_new_refund_claim() -> None:
    # A $40 refund on record lets the agent report it, not claim it just refunded $250.
    check = check_reply("I've refunded you $250.", _ctx(reported_refunds_this_turn=True), CONFIG)
    assert check.blocked_reason is not None


def test_foreign_order_number_blocks() -> None:
    check = check_reply("Order NW-10004 shipped yesterday.", _ctx(), CONFIG)
    assert check.blocked_reason is not None
    assert "NW-10004" in check.blocked_reason


@pytest.mark.parametrize("spelling", ["NW 10004", "nw10004", "#10004", "NW-10004"])
def test_foreign_order_in_any_spelling_blocks(spelling: str) -> None:
    check = check_reply(f"Order {spelling} shipped yesterday.", _ctx(), CONFIG)
    assert check.blocked_reason is not None
    assert "NW-10004" in check.blocked_reason


def test_rma_numbers_are_not_order_numbers() -> None:
    assert check_reply("Your return is RMA-10002-1.", _ctx(), CONFIG).blocked_reason is None


def test_own_and_customer_typed_order_numbers_are_fine() -> None:
    ctx = _ctx(customer_text="is nw-10077 mine?")
    assert check_reply("NW-10002 is yours; NW-10077 isn't.", ctx, CONFIG).blocked_reason is None


def test_unprovided_pii_is_masked_but_customer_typed_pii_kept() -> None:
    ctx = _ctx(customer_text="I'm ava.thompson@example.com, call 206-555-0101")
    text = (
        "Thanks ava.thompson@example.com. I also found maya.n.work@example.com and "
        "(503) 555-0199. We'll call (206) 555-0101."
    )
    check = check_reply(text, ctx, CONFIG)
    assert "ava.thompson@example.com" in check.text
    assert "maya.n.work@example.com" not in check.text
    assert "555-0199" not in check.text
    assert "(206) 555-0101" in check.text
    assert any(d.guardrail == "pii" and d.action == "rewrite" for d in check.decisions)


def test_card_numbers_are_masked_in_replies() -> None:
    check = check_reply("The card on file is 4111 1111 1111 1111.", _ctx(), CONFIG)
    assert "4111 1111 1111 1111" not in check.text
    assert "****1111" in check.text


def test_length_cap_per_channel() -> None:
    long = "This is a sentence. " * 60  # 1200 chars
    sms = check_reply(long, _ctx(channel="sms"), CONFIG)
    assert len(sms.text) <= 480
    assert sms.text.endswith("…")
    assert any(d.guardrail == "length" and d.action == "rewrite" for d in sms.decisions)
    email = check_reply(long, _ctx(channel="email"), CONFIG)
    assert email.text == long


def test_truncation_never_leaves_half_a_citation_tag() -> None:
    from baton.runtime.guardrails import truncate

    text = "word " * 50 + "end [kb:shipping-international] more text"
    out = truncate(text, 270)
    assert out.endswith("…")
    assert "[kb:" not in out
