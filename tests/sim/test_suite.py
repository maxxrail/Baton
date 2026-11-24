"""Suite format: the real suites load and validate, placeholders resolve from the seed, and
every kind of mistake is reported with its file and scenario."""

from pathlib import Path

import pytest

from baton.aop.models import AOPBundle
from baton.sim.cli import thresholds
from baton.sim.fixtures import FIXTURE_SPECS, FixtureError, fixture_values, resolve
from baton.sim.suite import SuiteError, coverage, load_suite, validate_suite
from baton.tools.registry import ToolRegistry
from tests.runtime.support import REPO
from tests.sim.support import scenario

SIMS = REPO / "evals" / "sims"


def test_the_main_suite_validates_with_the_required_mix(
    bundle: AOPBundle, registry: ToolRegistry
) -> None:
    scenarios = load_suite(SIMS)
    assert validate_suite(scenarios, bundle, registry) == []
    counts, problems = coverage(scenarios, thresholds().coverage, sorted(bundle.aops))
    assert problems == []
    assert counts["total"] >= 40
    assert counts["adversarial"] >= 10
    assert counts["knowledge"] >= 5
    assert counts["unanswerable"] >= 2
    assert counts["multi_intent"] >= 3
    assert counts["language_or_typing"] >= 3


def test_the_ci_suite_is_fully_scripted(bundle: AOPBundle, registry: ToolRegistry) -> None:
    scenarios = load_suite(SIMS / "ci")
    assert scenarios
    assert validate_suite(scenarios, bundle, registry, require_script=True) == []


def test_every_fixture_resolves_from_the_seed() -> None:
    for name in FIXTURE_SPECS:
        values = fixture_values(name)
        assert values["email"].endswith("@example.com")
        assert values["zip"].isdigit()
    assert fixture_values("ava_outside_window")["order_number"] == "NW-10001"
    assert fixture_values("maya_shared_phone")["order_number_2"] == "NW-10110"
    assert fixture_values("sub_active")["subscription_id"] == "sub_0001"


def test_placeholders_resolve_bare_keyed_and_cross_fixture() -> None:
    assert resolve("<seeded>", "ava_outside_window", "zip") == "98101"
    assert resolve("order <seeded:order_number>", "ava_outside_window") == "order NW-10001"
    assert resolve("<seeded:liam_partial_refund.order_number>", None) == "NW-10004"
    with pytest.raises(FixtureError, match=r"needs persona.fixture"):
        resolve("<seeded:zip>", None)
    with pytest.raises(FixtureError, match="no 'phone_2'"):
        resolve("<seeded:phone_2>", "ava_outside_window")
    with pytest.raises(FixtureError, match="bare <seeded>"):
        resolve("<seeded>", "ava_outside_window")


def test_resolved_replaces_placeholders_everywhere_a_persona_and_expect_use_them() -> None:
    sc = scenario(
        persona={
            "name": "p",
            "fixture": "ava_inside_window",
            "goal": "return <seeded:item>",
            "knows": {"email": "<seeded>", "zip": "<seeded>"},
            "opening": "hi, <seeded:order_number> here",
        },
        expect={"must_not": ["reveals <seeded:liam_partial_refund.order_number>"]},
    ).resolved()
    assert sc.persona.goal == "return Stoneware Mug"
    assert sc.persona.knows == {"email": "ava.thompson@example.com", "zip": "98101"}
    assert sc.persona.opening == "hi, NW-10002 here"
    assert sc.expect.must_not == ["reveals NW-10004"]


def test_every_problem_is_reported_with_its_scenario(
    bundle: AOPBundle, registry: ToolRegistry
) -> None:
    bad = [
        scenario(id="a", aop="nope"),
        scenario(id="b", expect={"tools_called": ["teleport"]}),
        scenario(id="c", persona={"name": "p", "goal": "g", "fixture": "ghost"}),
        scenario(id="d", expect={}),
        scenario(id="e", persona={"name": "p", "goal": "g", "knows": {"zip": "<seeded>"}}),
        scenario(id="e"),
    ]
    problems = validate_suite(bad, bundle, registry)
    text = "\n".join(problems)
    assert "(a): unknown aop 'nope'" in text
    assert "(b): unknown tool 'teleport'" in text
    assert "(c): unknown fixture 'ghost'" in text
    assert "(d): expect checks nothing" in text
    assert "(e): <seeded> needs persona.fixture" in text
    assert "(e): duplicate id" in text


def test_load_collects_yaml_and_shape_errors_from_every_file(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("id: [unclosed", encoding="utf-8")
    (tmp_path / "shape.yaml").write_text(
        "- id: Bad-Id\n  aop: x\n  persona: {name: p, goal: g}\n  expect: {}\n  tags: [vibes]\n"
        "  surprise: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(SuiteError) as err:
        load_suite(tmp_path)
    text = "\n".join(err.value.problems)
    assert "broken.yaml: not valid YAML" in text
    assert "shape.yaml[0] (Bad-Id): id:" in text
    assert "unknown tags ['vibes']" in text
    assert "surprise: Extra inputs are not permitted" in text


def test_script_mistakes_are_caught(bundle: AOPBundle, registry: ToolRegistry) -> None:
    sc = scenario(
        expect={"must": ["x"]},
        script={
            "turns": [
                {
                    "customer": "hi",
                    "route": "astrology",
                    "agent": [{"calls": [{"tool": "launch_rocket"}]}],
                }
            ],
            "judge": {"not a criterion": True},
        },
    )
    text = "\n".join(validate_suite([sc], bundle, registry))
    assert "routes to unknown aop 'astrology'" in text
    assert "calls unknown tool 'launch_rocket'" in text
    assert "script.judge names unknown 'not a criterion'" in text
    no_script = validate_suite([scenario()], bundle, registry, require_script=True)
    assert "no script" in "\n".join(no_script)


def test_filter_matches_tags_aop_and_id() -> None:
    sc = scenario(id="refund_over_limit", aop="refund_request", tags=["adversarial"])
    assert sc.matches("adversarial")
    assert sc.matches("refund_request")
    assert sc.matches("over_limit")
    assert not sc.matches("kb")
