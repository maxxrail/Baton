"""Help-center answers through the full agent: search, citations, grounding, abstention.

The agent, store and help center are real; only the LLM is scripted. `llm.grounding` plays
the grounding judge with a simple rule (see tests/knowledge/judge.py).
"""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.models import AOPBundle
from baton.knowledge.base import KnowledgeBase
from baton.knowledge.grounding import ABSTAIN_REPLY, HEDGE_NOTE, citations
from baton.llm import LLMResponse
from baton.runtime.agent import Agent
from baton.tools.client import CommerceClient
from baton.tools.northwind import NorthwindTools
from tests.knowledge.judge import judge, sources_in, supported_if
from tests.runtime.support import (
    REPO,
    ScriptedLLM,
    advance,
    call,
    decisions,
    say,
    state_of,
    tool_names,
    traces_of,
)

CANADA_FACT = {"6-10 business days": "kb:shipping-international"}


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


def _search(query: str) -> LLMResponse:
    return call(("search_knowledge", {"query": query}))


def _ask(agent: Agent, llm: ScriptedLLM, message: str, *script: LLMResponse) -> tuple[str, str]:
    cid = agent.start_conversation("web")
    llm.route_to = "general_question"
    llm.push(*script)
    return cid, agent.handle_turn(cid, message).text


# ---------------------------------------------------------------- grounded, cited answers


def test_policy_answer_is_searched_cited_and_grounded(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session], knowledge: KnowledgeBase
) -> None:
    llm.grounding = judge(supported_if(**CANADA_FACT))
    cid = kb_agent.start_conversation("web")
    llm.route_to = "general_question"
    llm.push(
        _search("shipping time to Canada"),
        say(
            "Standard shipping to Canada takes 6-10 business days once your order leaves the "
            "warehouse [kb:shipping-international]. Anything else I can help with?"
        ),
    )
    reply = kb_agent.handle_turn(cid, "how long does shipping to Canada take?")

    # The search tool was offered and returned the right article first.
    assert "search_knowledge" in llm.last_tools
    (trace,) = traces_of(sessions, cid)
    (search,) = trace["knowledge_searches"]  # type: ignore[misc]
    assert search["articles"][0] == "shipping-international"
    assert search["covered"] is True

    # The reply passed grounding unchanged, and every citation resolves to a real article.
    assert "6-10 business days" in reply.text
    assert citations(reply.text) == ["shipping-international"]
    (cited,) = reply.citations
    assert cited.article_id == "shipping-international"
    assert cited.title == knowledge.title("shipping-international")
    assert cited.url.endswith("/shipping-international")
    assert trace["citations"] == ["shipping-international"]
    (check,) = trace["grounding"]  # type: ignore[misc]
    assert check["action"] == "pass"
    assert ("grounding", "allow") in decisions(trace)
    assert trace["prompt_versions"]["grounding"].startswith("grounding@")  # type: ignore[index]
    assert any(c["purpose"] == "grounding" for c in trace["llm_calls"])  # type: ignore[union-attr]

    # The judge saw the retrieved passage and the reply's sentences.
    assert "kb:shipping-international" in sources_in(llm.grounding_calls[0])


def test_a_missing_citation_is_added_from_the_judge_sources(
    kb_agent: Agent, llm: ScriptedLLM
) -> None:
    llm.grounding = judge(supported_if(**CANADA_FACT))
    _, text = _ask(
        kb_agent,
        llm,
        "how long does shipping to Canada take?",
        _search("Canada shipping time"),
        say("Standard shipping to Canada takes 6-10 business days."),
    )
    assert (
        text == "Standard shipping to Canada takes 6-10 business days [kb:shipping-international]."
    )


def test_an_answer_written_next_to_resolve_reaches_the_customer(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    # Found live: the model often writes the answer in the same response as
    # advance_step(RESOLVE), then ends with a short "anything else?". Both must be sent.
    llm.grounding = judge(supported_if(**CANADA_FACT))
    answer = call(advance(3), advance("RESOLVE"))
    answer.text = (
        "Standard shipping to Canada takes 6-10 business days [kb:shipping-international]."
    )
    cid, text = _ask(
        kb_agent,
        llm,
        "how long does shipping to Canada take?",
        _search("Canada shipping time"),
        call(advance(2)),
        answer,
        say("Is there anything else I can help with?"),
    )
    assert text == (
        "Standard shipping to Canada takes 6-10 business days [kb:shipping-international]."
        "\n\nIs there anything else I can help with?"
    )
    assert state_of(sessions, cid).status == "resolved"


def test_citations_to_articles_that_werent_retrieved_are_removed(
    kb_agent: Agent, llm: ScriptedLLM
) -> None:
    llm.grounding = judge(supported_if(**CANADA_FACT))
    _, text = _ask(
        kb_agent,
        llm,
        "how long does shipping to Canada take?",
        _search("Canada shipping time"),
        say(
            "Standard shipping to Canada takes 6-10 business days [kb:shipping-international] "
            "[kb:canada-express-2027]."
        ),
    )
    assert citations(text) == ["shipping-international"]


# ---------------------------------------------------------------- the grounding guardrail


def test_invented_claim_is_removed_and_a_teammate_offered(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    llm.grounding = judge(supported_if(**CANADA_FACT))
    invented = "We also offer free 2-day express to Canada on orders over $100."
    cid, text = _ask(
        kb_agent,
        llm,
        "how long does shipping to Canada take?",
        _search("Canada shipping time"),
        say(f"Standard shipping to Canada takes 6-10 business days. {invented}"),
    )
    assert "express" not in text
    assert "6-10 business days [kb:shipping-international]." in text
    assert text.endswith(HEDGE_NOTE)
    (trace,) = traces_of(sessions, cid)
    assert ("grounding", "rewrite") in decisions(trace)
    (check,) = trace["grounding"]  # type: ignore[misc]
    assert check["action"] == "trimmed"
    assert check["removed"] == [invented]
    assert trace["rejected_replies"] == [
        f"Standard shipping to Canada takes 6-10 business days. {invented}"
    ]


def test_unanswerable_question_with_invented_policy_becomes_an_abstention(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    llm.grounding = judge(supported_if())  # nothing in the help center supports a price match
    cid, text = _ask(
        kb_agent,
        llm,
        "do you price match?",
        _search("price match policy"),
        say("Yes! We match any competitor's price within 14 days of purchase."),
    )
    assert text == ABSTAIN_REPLY
    assert "14 days" not in text
    (trace,) = traces_of(sessions, cid)
    # A terse query like this can look "covered" to retrieval (it shares "price", "match" and
    # "policy" with other articles); the grounding check is what stops the invented policy.
    assert trace["reply_source"] == "abstention"
    assert trace["citations"] == []


def test_unanswerable_question_honest_abstention_passes_and_can_escalate(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    llm.grounding = judge(supported_if())
    honest = (
        "I'm not sure whether we price match: our help center doesn't cover it. "
        "Would you like me to bring in a teammate?"
    )
    cid, text = _ask(kb_agent, llm, "do you price match?", _search("price match"), say(honest))
    assert text == honest  # no claims, nothing to remove
    assert state_of(sessions, cid).status == "open"

    # The customer takes the offer: the procedure escalates to a human.
    llm.push(
        call(advance(2), advance(3), advance("ESCALATE", "customer wants a teammate")),
        say("Okay, a teammate will pick this up here shortly."),
    )
    reply = kb_agent.handle_turn(cid, "yes please")
    assert reply.status == "escalated"
    assert state_of(sessions, cid).handoff is not None


def test_answering_without_searching_is_still_checked_and_fails(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    # The model skips the search and answers from "general knowledge". general_question
    # answers from the help center, so the reply is checked anyway, against no KB evidence.
    llm.grounding = judge(supported_if())
    cid, text = _ask(
        kb_agent,
        llm,
        "do you offer a student discount?",
        say("Yes, students get 15% off with a valid student ID."),
    )
    assert text == ABSTAIN_REPLY
    assert not any(s.startswith("kb:") for s in sources_in(llm.grounding_calls[0]))
    (trace,) = traces_of(sessions, cid)
    assert "search_knowledge" not in tool_names(trace)


def test_a_broken_judge_fails_closed(kb_agent: Agent, llm: ScriptedLLM) -> None:
    llm.grounding = lambda system, messages, tools: say("looks good")  # no tool call, not JSON
    _, text = _ask(
        kb_agent,
        llm,
        "how long does shipping to Canada take?",
        _search("Canada shipping time"),
        say("Standard shipping to Canada takes 6-10 business days."),
    )
    assert text == ABSTAIN_REPLY


def test_follow_up_answers_are_grounded_in_the_previous_search(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    llm.grounding = judge(supported_if(**{**CANADA_FACT, "CA$18": "kb:shipping-international"}))
    cid, _ = _ask(
        kb_agent,
        llm,
        "how long does shipping to Canada take?",
        _search("Canada shipping time"),
        say("Standard shipping to Canada takes 6-10 business days [kb:shipping-international]."),
    )
    # Next turn: no new search; the model answers from the passage it already has.
    llm.push(say("Shipping to Canada is a flat CA$18 per order [kb:shipping-international]."))
    reply = kb_agent.handle_turn(cid, "and how much does it cost?")
    assert "CA$18" in reply.text
    assert "kb:shipping-international" in sources_in(llm.grounding_calls[-1])
    assert [c.article_id for c in reply.citations] == ["shipping-international"]


# ---------------------------------------------------------------- availability + scope


def test_search_is_available_in_account_procedures_before_verification(
    kb_agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    llm.grounding = judge(supported_if(**{"30 days": "kb:return-policy"}))
    cid = kb_agent.start_conversation("web")
    llm.route_to = "return_item"
    llm.push(
        _search("return window"),
        say(
            "You can return most items within 30 days of delivery [kb:return-policy]. "
            "Which order is this about?"
        ),
    )
    reply = kb_agent.handle_turn(cid, "how long do I have to return a mug?")
    (trace,) = traces_of(sessions, cid)
    assert tool_names(trace) == ["search_knowledge"]
    assert not any(g == "identity_gate" for g, _ in decisions(trace))
    (search,) = trace["knowledge_searches"]  # type: ignore[misc]
    # The newer 30-day policy outranks the 2024 60-day article.
    articles = search["articles"]
    assert articles[0] == "return-policy"
    if "return-policy-2024" in articles:
        assert articles.index("return-policy") < articles.index("return-policy-2024")
    assert reply.citations[0].article_id == "return-policy"


def test_account_procedure_replies_without_kb_use_are_not_grounding_checked(
    kb_agent: Agent, llm: ScriptedLLM
) -> None:
    # No search, no citation, and return_item doesn't answer from the help center: no judge
    # call (llm.grounding is unset, so a check would fail the test).
    cid = kb_agent.start_conversation("web")
    llm.route_to = "return_item"
    llm.push(say("Happy to help with a return. What's the email on your account?"))
    reply = kb_agent.handle_turn(cid, "I want to return something")
    assert reply.citations == []
    assert llm.grounding_calls == []


def test_without_a_knowledge_base_search_reports_unavailable(
    bundle: AOPBundle, llm: ScriptedLLM, commerce: CommerceClient, sessions: sessionmaker[Session]
) -> None:
    agent = Agent(
        bundle,
        llm,
        NorthwindTools(commerce),
        sessions,
        prompts_dir=REPO / "prompts",
        raise_errors=True,
    )
    llm.grounding = judge(supported_if())
    cid = agent.start_conversation("web")
    llm.route_to = "general_question"
    llm.push(_search("returns"), say("I'm not sure right now. I can bring in a teammate."))
    agent.handle_turn(cid, "what's your return policy?")
    trace = traces_of(sessions, cid)[0]
    (tool,) = trace["tool_calls"]  # type: ignore[misc]
    assert tool["error_code"] == "KNOWLEDGE_UNAVAILABLE"
