"""Regression tests for issues the Part 03 code review found in the reply path.

- Text written next to tool calls is kept only when every call in that response succeeded,
  the model is told it will be sent, and a repeat of it isn't sent twice.
- An abstention never hides a runtime-verified fact: an escalation or a side effect the store
  confirmed stays in the reply.
- On SMS, truncation cuts the answer, never the offer of a teammate.
- Sloppy citation tags are normalised, then resolved or stripped, never shown raw.
"""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.models import AOPBundle
from baton.knowledge.base import KnowledgeBase
from baton.knowledge.grounding import HEDGE_NOTE, citations
from baton.llm import LLMResponse
from baton.runtime.agent import DRAFT_KEPT_NOTE, ESCALATION_REPLY, Agent
from baton.tools.client import CommerceClient
from baton.tools.northwind import NorthwindTools
from tests.knowledge.judge import judge, supported_if
from tests.runtime.support import (
    REPO,
    ScriptedLLM,
    advance,
    call,
    say,
    state_of,
    traces_of,
)

CANADA = "Standard shipping to Canada takes 6-10 business days [kb:shipping-international]."
LIAM = {"customer_id": "cus_0002", "order_number": "NW-10004", "zip": "78701"}


@pytest.fixture
def kb_agent(
    bundle: AOPBundle,
    llm: ScriptedLLM,
    commerce: CommerceClient,
    sessions: sessionmaker[Session],
    knowledge: KnowledgeBase,
) -> Agent:
    tools = NorthwindTools(commerce, knowledge=knowledge)
    return Agent(bundle, llm, tools, sessions, prompts_dir=REPO / "prompts", raise_errors=True)


def _with_text(response: LLMResponse, text: str) -> LLMResponse:
    response.text = text
    return response


def _search(query: str) -> tuple[str, dict[str, object]]:
    return ("search_knowledge", {"query": query})


# ---------------------------------------------------------------- drafts next to tool calls


def test_a_draft_next_to_a_failed_tool_call_is_discarded(
    bundle: AOPBundle, llm: ScriptedLLM, commerce: CommerceClient, sessions: sessionmaker[Session]
) -> None:
    # No knowledge base: the search fails, so the policy the model wrote beside it is dropped.
    agent = Agent(
        bundle,
        llm,
        NorthwindTools(commerce),
        sessions,
        prompts_dir=REPO / "prompts",
        raise_errors=True,
    )
    cid = agent.start_conversation("web")
    llm.route_to = "return_item"
    llm.push(
        _with_text(call(_search("return window")), "Our return window is 60 days."),
        say("Sorry, I can't check our return policy right now. Which order is this about?"),
    )
    reply = agent.handle_turn(cid, "how long do I have to return something?")
    assert "60 days" not in reply.text
    assert reply.text.startswith("Sorry, I can't check")


def test_a_kept_draft_is_announced_and_not_sent_twice(kb_agent: Agent, llm: ScriptedLLM) -> None:
    llm.grounding = judge(supported_if(**{"6-10 business days": "kb:shipping-international"}))
    cid = kb_agent.start_conversation("web")
    llm.route_to = "general_question"
    llm.push(
        call(advance(2), _search("Canada shipping time")),
        _with_text(call(advance(3), advance("RESOLVE")), CANADA),
        say(CANADA),  # a live model often repeats its answer after RESOLVE
    )
    reply = kb_agent.handle_turn(cid, "how long does shipping to Canada take?")
    assert reply.text == CANADA
    # The model was told its draft will be sent, right after the tool results.
    last_messages = llm.agent_calls[-1][1]
    assert DRAFT_KEPT_NOTE in str(last_messages[-1].content)


# ---------------------------------------------------------------- abstention keeps verified facts


def test_a_failed_judge_keeps_the_refund_confirmation(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    llm.grounding = lambda system, messages, tools: say("unusable")  # judge fails closed
    cid = kb_agent.start_conversation("web")
    llm.route_to = "refund_request"
    llm.push(
        call(("lookup_customer", {"email": "liam.okafor@example.com"}), ("verify_customer", LIAM)),
        call(advance(2), ("get_order", {"order_number": "NW-10004"})),
        call(advance(3), advance(4), advance(5), advance(6)),
        call(
            ("create_refund", {"order_number": "NW-10004", "amount_cents": 2500, "reason": "late"})
        ),
        call(advance("RESOLVE"), _search("how long do refunds take")),
        say("I've refunded $25.00; it shows up in 5-10 business days [kb:refund-timing]."),
    )
    reply = kb_agent.handle_turn(
        cid, "NW-10004 was late, $25 back please. liam.okafor@example.com 78701"
    )
    # Grounding abstained, but the customer still learns the refund went through.
    assert reply.text == f"I've issued a refund of $25 on order NW-10004. {HEDGE_NOTE}"
    (trace,) = traces_of(sessions, cid)
    assert trace["reply_source"] == "abstention"
    assert state_of(sessions, cid).auto_refunded_cents == 2500


def test_an_abstention_during_escalation_says_a_teammate_is_coming(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    llm.grounding = judge(supported_if())  # nothing supported
    cid = kb_agent.start_conversation("web")
    llm.route_to = "general_question"
    llm.push(
        call(advance(2), _search("price match")),
        call(advance(3), advance("ESCALATE", "wants a price match")),
        say("We price match within 14 days. A teammate will follow up here."),
    )
    reply = kb_agent.handle_turn(cid, "do you price match? I'd like someone to check")
    assert reply.text == ESCALATION_REPLY  # not "would you like a teammate?"
    assert reply.status == "escalated"


# ---------------------------------------------------------------- channel length + citations


def test_on_sms_truncation_keeps_the_offer_of_a_teammate(kb_agent: Agent, llm: ScriptedLLM) -> None:
    llm.grounding = judge(supported_if(**{"6-10 business days": "kb:shipping-international"}))
    cid = kb_agent.start_conversation("sms")
    llm.route_to = "general_question"
    long_true = "Standard shipping to Canada takes 6-10 business days, " + "and so on " * 40
    llm.push(
        call(_search("Canada shipping time")),
        say(f"We also ship free to Canada on all orders. {long_true.strip()}."),
    )
    reply = kb_agent.handle_turn(cid, "canada shipping time?")
    assert len(reply.text) <= 480
    assert reply.text.endswith(HEDGE_NOTE)
    assert "ship free" not in reply.text
    assert "[kb:shipping-intern" not in reply.text.replace("[kb:shipping-international]", "")


def test_sloppy_citation_tags_are_normalised_or_stripped(kb_agent: Agent, llm: ScriptedLLM) -> None:
    llm.grounding = judge(supported_if(**{"6-10 business days": "kb:shipping-international"}))
    cid = kb_agent.start_conversation("web")
    llm.route_to = "general_question"
    llm.push(
        call(_search("Canada shipping time")),
        say(
            "Standard shipping to Canada takes 6-10 business days [KB: Shipping-International] "
            "[kb: gift-cards ]."
        ),
    )
    reply = kb_agent.handle_turn(cid, "canada shipping time?")
    assert "KB:" not in reply.text
    assert citations(reply.text) == ["shipping-international"]  # gift-cards wasn't retrieved
    assert [c.article_id for c in reply.citations] == ["shipping-international"]
