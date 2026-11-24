"""Runtime fixes for failure modes the Part 05 live simulations found (docs/EVAL_REPORT.md)."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from baton.knowledge.grounding import ABSTAIN_REPLY, HEDGE_NOTE
from baton.runtime.agent import ESCALATION_REPLY, Agent
from baton.runtime.guardrails import wants_human
from baton.runtime.language import detect_language, localize
from baton.runtime.state import ConversationState
from tests.runtime.support import ScriptedLLM, advance, call, say


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Hola, quiero devolver las tazas de mi pedido NW-10002, por favor", "es"),
        ("Bonjour, je voudrais le suivi de ma commande NW-10008", "fr"),
        ("Where is my order NW-10002? I want to return it", "en"),
        ("return my blanket, la la land", "en"),  # two stray Spanish words aren't enough
        ("ok", "en"),
    ],
)
def test_detect_language(text: str, language: str) -> None:
    assert detect_language(text) == language


def test_runtime_sentences_are_translated_and_model_text_is_left_alone() -> None:
    text = f"Su devolución está creada.\n\n{HEDGE_NOTE}"
    spanish = localize(text, "es")
    assert spanish.startswith("Su devolución está creada.")
    assert HEDGE_NOTE not in spanish
    assert "centro de ayuda" in spanish
    assert localize(ESCALATION_REPLY, "fr").startswith("Merci de votre patience")
    assert localize(ABSTAIN_REPLY, "en") == ABSTAIN_REPLY


def test_a_spanish_conversation_gets_the_escalation_reply_in_spanish(agent: Agent) -> None:
    cid = agent.start_conversation("web")
    reply = agent.handle_turn(
        cid, "Hola, quiero devolver las tazas de mi pedido. Quiero hablar con una persona"
    )
    assert reply.status == "escalated"
    assert reply.text.startswith("Gracias por su paciencia")


@pytest.mark.parametrize(
    "text",
    [
        "fine. pass me to a teammate",
        "get me the teammate",
        "put me through to a person",
        "can I speak with a teammate",
        "connect me with an agent",
        "quiero hablar con una persona",
        "je veux parler à quelqu'un",
    ],
)
def test_more_ways_of_asking_for_a_person(text: str) -> None:
    assert wants_human(text)


@pytest.mark.parametrize(
    "text", ["I spoke to your teammate yesterday", "my teammate ordered it", "pass me the link"]
)
def test_mentions_of_people_are_not_requests(text: str) -> None:
    assert not wants_human(text)


def test_the_state_says_how_many_verification_attempts_are_left() -> None:
    state = ConversationState(conversation_id="c", channel="web", failed_verifications=["a", "b"])
    assert "Failed verification attempts: 2 of 3 (1 left before a teammate takes over)" in (
        state.summary(3)
    )
    assert "Failed verification attempts: 2\n" in state.summary() + "\n"


def _tool_results(llm: ScriptedLLM) -> list[dict[str, object]]:
    out = []
    for _, messages, _ in llm.agent_calls:
        for m in messages:
            if isinstance(m.content, list):
                out.extend(
                    json.loads(b.content)
                    for b in m.content
                    if getattr(b, "type", "") == "tool_result"
                )
    return out


def test_resolve_repeats_the_last_steps_instructions(
    agent: Agent, llm: ScriptedLLM, sessions: sessionmaker[Session]
) -> None:
    """Found by simulation: after RESOLVE the step left the system prompt, so the reply dropped
    what the step said to tell the customer ("tracking is emailed when it ships")."""
    cid = agent.start_conversation("web")
    llm.route_to = "where_is_my_order"
    ava = {"customer_id": "cus_0001", "order_number": "NW-10003", "zip": "98101"}
    llm.push(
        call(("verify_customer", ava)),
        call(advance(2), ("get_order", {"order_number": "NW-10003"})),
        call(advance(3)),
        call(advance("RESOLVE")),
        say("NW-10003 hasn't shipped yet; we'll email tracking when it does."),
    )
    agent.handle_turn(cid, "where is NW-10003? ava.thompson@example.com 98101")
    resolved = next(
        r for r in _tool_results(llm) if "Procedure resolved" in str(r.get("message", ""))
    )
    assert "Step 3 said: If the order is still processing" in str(resolved["message"])
    assert "tracking is emailed" in str(resolved["message"])


def test_grounding_evidence_includes_the_procedures_steps(agent: Agent) -> None:
    """Found by simulation: "the label is emailed" (return_item step 5) was trimmed as
    unsupported because only the policies were evidence."""
    aop = agent.bundle.get("return_item")

    turn = SimpleNamespace(
        kb_results=[],
        previous_kb_evidence=[],
        account_evidence=[],
        state=ConversationState(conversation_id="c", channel="web"),
        procedures={"return_item": aop},
    )
    evidence = agent._evidence(turn)  # type: ignore[arg-type]
    procedure = next(e for e in evidence if e.source == "procedure")
    assert "label is emailed" in procedure.text
    assert "Returns are accepted within 30 days of delivery." in procedure.text


@pytest.mark.parametrize(
    ("text", "closing"),
    [
        ("great, thanks, that's all", True),
        ("That's all I needed.", True),
        ("Muchas gracias, eso es todo", True),
        ("merci, au revoir", True),
        ("one more q", False),
        ("thanks! where is my other order?", False),
        ("I want to cancel my tyre order", False),  # "ty" inside a word doesn't count
    ],
)
def test_is_closing(text: str, closing: bool) -> None:
    from baton.runtime.guardrails import is_closing

    assert is_closing(text) is closing
