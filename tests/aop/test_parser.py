from textwrap import dedent

import pytest

from baton.aop.models import Terminal, Transition
from baton.aop.parser import (
    AOPParseError,
    parse_aop,
    parse_tool_refs,
    parse_transitions,
    source_hash,
)

E, R = Terminal.ESCALATE, Terminal.RESOLVE

# (step text, expected explicit transitions as (condition, target) pairs)
PHRASINGS: list[tuple[str, list[tuple[str | None, int | Terminal]]]] = [
    ("Then go to step 3.", [(None, 3)]),
    ("Go to step 3.", [(None, 3)]),
    ("Skip to 3.", [(None, 3)]),
    ("skip to step 3", [(None, 3)]),
    ("Jump to step 5.", [(None, 5)]),
    ("Proceed to step 4.", [(None, 4)]),
    ("Continue with step 4.", [(None, 4)]),
    ("Continue at step 2.", [(None, 2)]),
    ("Return to step 1.", [(None, 1)]),
    ("Go back to step 1.", [(None, 1)]),
    ("Move on to step 6.", [(None, 6)]),
    ("Repeat step 2.", [(None, 2)]),
    ("Restart from step 1.", [(None, 1)]),
    ("Start over at step 1.", [(None, 1)]),
    ("GO TO STEP 7", [(None, 7)]),
    ("Go to step #3.", [(None, 3)]),
    ("-> step 4", [(None, 4)]),
    ("→ step 4", [(None, 4)]),
    ("Apologize, then ESCALATE.", [(None, E)]),
    ("ESCALATE.", [(None, E)]),
    ("Confirm the change. RESOLVE", [(None, R)]),
    ("If the order shipped, go to step 4.", [("the order shipped", 4)]),
    ("If they accept then go to step 3.", [("they accept", 3)]),
    ("When the customer confirms, continue to step 5.", [("the customer confirms", 5)]),
    ("Once they pick a size, skip to step 4.", [("they pick a size", 4)]),
    ("In case the label is lost, ESCALATE.", [("the label is lost", E)]),
    ("Unless they are verified, ESCALATE.", [("unless they are verified", E)]),
    ("Go to step 3 if they want an exchange.", [("they want an exchange", 3)]),
    ("Offer to ESCALATE if they insist.", [("they insist", E)]),
    ("Otherwise, skip to step 4.", [(None, 4)]),
    (
        "If they insist, ESCALATE; otherwise RESOLVE.",
        [("they insist", E), (None, R)],
    ),
    (
        "If it shipped, go to step 4, otherwise go to step 5.",
        [("it shipped", 4), (None, 5)],
    ),
    (
        "If the order is not delivered, explain returns start after delivery and RESOLVE.",
        [("the order is not delivered", R)],
    ),
    (
        "If delivered more than 30 days ago, apologize, explain the policy, and offer to "
        "ESCALATE if they insist.",
        [("delivered more than 30 days ago", E)],
    ),
    # Things that must NOT be read as transitions.
    ("We'll resolve this together and escalate only if needed.", []),
    ("Explain the policy from step 3 again.", []),
    ("Tell them refunds take 5-10 business days.", []),
    ("Return the item to the warehouse.", []),
    ("Repeat the address back to them 2 times.", []),
    ("Ask them to go to their email to find the label.", []),
    ("Continue with 2 of them.", []),
    # Review regressions: "else" inside a condition, "if" meaning "whether".
    ("If they want something else, go to step 2.", [("they want something else", 2)]),
    ("Ask if they want a refund, then go to step 4.", [(None, 4)]),
    ("Go to step 3 if they want it or ESCALATE.", [("they want it or", 3), (None, E)]),
    ("Else RESOLVE.", [(None, R)]),
]


@pytest.mark.parametrize(("text", "expected"), PHRASINGS, ids=[p[0][:40] for p in PHRASINGS])
def test_phrasings(text: str, expected: list[tuple[str | None, int | Terminal]]) -> None:
    got = [(t.condition, t.target) for t in parse_transitions(text)]
    assert got == expected


def test_there_are_at_least_15_transition_phrasings() -> None:
    assert sum(1 for _, expected in PHRASINGS if expected) >= 15


def test_duplicate_transitions_in_one_step_are_merged() -> None:
    assert parse_transitions("RESOLVE. Then RESOLVE.") == [Transition(condition=None, target=R)]


@pytest.mark.parametrize(
    ("text", "refs"),
    [
        ("Use @get_order.", ["get_order"]),
        ("Use @get_customer_orders, then @get_order.", ["get_customer_orders", "get_order"]),
        ("Call @get_order twice: @get_order.", ["get_order"]),
        ("Email support@northwind.example for help.", []),
        ("No tools here.", []),
    ],
)
def test_tool_refs(text: str, refs: list[str]) -> None:
    assert parse_tool_refs(text) == refs


GOOD = dedent(
    """\
    ---
    id: demo
    name: Demo
    version: 2
    triggers:
      - customer wants a demo
    requires_verification: true
    policies:
      - Be nice.
    ---
    # Optional heading
    1. Look up the order with @get_order.
       If it is missing, ESCALATE.
    2. If they changed their mind, RESOLVE.
    3. Confirm. RESOLVE.
    """
)


def test_parse_full_aop() -> None:
    aop = parse_aop(GOOD, "demo.md")
    assert (aop.id, aop.name, aop.version, aop.ref) == ("demo", "Demo", 2, "demo@2")
    assert aop.triggers == ["customer wants a demo"]
    assert aop.requires_verification is True
    assert aop.policies == ["Be nice."]
    assert [s.number for s in aop.steps] == [1, 2, 3]
    step1 = aop.step(1)
    assert step1.text == "Look up the order with @get_order. If it is missing, ESCALATE."
    assert step1.tool_refs == ["get_order"]
    assert step1.transitions == [
        Transition(condition="it is missing", target=E),
        Transition(condition=None, target=2, implicit=True),
    ]
    assert aop.step(3).transitions == [Transition(condition=None, target=R)]
    assert aop.tool_refs() == ["get_order"]


def test_last_step_gets_no_implicit_fallthrough() -> None:
    aop = parse_aop(GOOD.replace("3. Confirm. RESOLVE.", "3. If done, RESOLVE."))
    assert aop.step(3).transitions == [Transition(condition="done", target=R)]


def test_explicit_default_suppresses_fallthrough() -> None:
    aop = parse_aop(GOOD.replace("2. If they changed their mind, RESOLVE.", "2. Go to step 1."))
    assert aop.step(2).transitions == [Transition(condition=None, target=1)]


def test_hash_ignores_line_endings_but_not_content() -> None:
    assert source_hash(GOOD) == source_hash(GOOD.replace("\n", "\r\n"))
    assert source_hash(GOOD) != source_hash(GOOD.replace("Be nice.", "Be kind."))
    assert parse_aop(GOOD.replace("\n", "\r\n")).source_hash == parse_aop(GOOD).source_hash


def test_defaults_when_optional_fields_missing() -> None:
    src = "---\nid: x\nname: X\nversion: 1\n---\n1. RESOLVE.\n"
    aop = parse_aop(src)
    assert aop.triggers == []
    assert aop.policies == []
    assert aop.requires_verification is False


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("1. RESOLVE.\n", "missing YAML frontmatter"),
        ("---\nid: x\nname: X\nversion: 1\n1. RESOLVE.\n", "missing YAML frontmatter"),
        ("---\nid: [x\n---\n1. RESOLVE.\n", "invalid YAML"),
        ("---\n- a\n---\n1. RESOLVE.\n", "must be a YAML mapping"),
        ("---\nname: X\nversion: 1\n---\n1. RESOLVE.\n", "id: Field required"),
        ("---\nid: Bad-Id\nname: X\nversion: 1\n---\n1. RESOLVE.\n", "id: String should"),
        ("---\nid: x\nname: X\nversion: '1'\n---\n1. RESOLVE.\n", "version"),
        ("---\nid: x\nname: X\nversion: 0\n---\n1. RESOLVE.\n", "version"),
        (
            "---\nid: x\nname: X\nversion: 1\nrequires_verfication: true\n---\n1. RESOLVE.\n",
            "requires_verfication: Extra inputs are not permitted",
        ),
        ("---\nid: x\nname: X\nversion: 1\n---\n", "no steps"),
        ("---\nid: x\nname: X\nversion: 1\n---\nIntro text\n1. RESOLVE.\n", "expected a num"),
        ("---\nid: x\nname: X\nversion: 1\n---\n1. Ask.\n3. RESOLVE.\n", "expected step 2"),
        ("---\nid: x\nname: X\nversion: 1\n---\n1. Ask.\n1. RESOLVE.\n", "expected step 2"),
    ],
)
def test_parse_errors(source: str, message: str) -> None:
    with pytest.raises(AOPParseError, match=message):
        parse_aop(source, "bad.md")


def test_parse_error_reports_line_number() -> None:
    src = "---\nid: x\nname: X\nversion: 1\n---\n1. Ask.\n\n4. RESOLVE.\n"
    with pytest.raises(AOPParseError) as info:
        parse_aop(src, "bad.md")
    assert info.value.line == 8
    assert str(info.value).startswith("bad.md:8:")


@pytest.mark.parametrize(
    "text",
    [
        "Never RESOLVE before confirming.",
        "Do not ESCALATE unless they insist.",
        "Don't go to step 3 yet.",
        "Go to step 3 unless the order is cancelled, in which case RESOLVE.",
    ],
)
def test_ambiguous_phrasings_are_rejected_with_the_step_and_line(text: str) -> None:
    src = f"---\nid: x\nname: X\nversion: 1\n---\n1. Ask.\n2. {text}\n3. RESOLVE.\n"
    with pytest.raises(AOPParseError, match="step 2: ") as info:
        parse_aop(src, "x.md")
    assert info.value.line == 7


def test_none_is_a_reserved_id() -> None:
    with pytest.raises(AOPParseError, match="reserved"):
        parse_aop("---\nid: none\nname: X\nversion: 1\n---\n1. RESOLVE.\n")


def test_hash_ignores_a_leading_bom() -> None:
    assert source_hash("\ufeff" + GOOD) == source_hash(GOOD)
    assert parse_aop("\ufeff" + GOOD).source_hash == parse_aop(GOOD).source_hash
