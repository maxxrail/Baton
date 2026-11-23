"""The Mermaid output is checked for syntax *shape*: header, node and edge line grammar,
balanced quotes, and every edge endpoint defined as a node."""

import re
from pathlib import Path

import pytest

from baton.aop.cli import main
from baton.aop.compiler import compile_aops
from baton.aop.mermaid import to_mermaid
from baton.aop.parser import parse_aop

REPO = Path(__file__).resolve().parents[2]

_ID = r"[A-Za-z][A-Za-z0-9_]*"
_LABEL = r'"[^"]*"'
NODE = re.compile(rf"^\s{{4}}({_ID})(\[{_LABEL}\]|\(\[{_LABEL}\]\)|\{{\{{{_LABEL}\}}\}})$")
EDGE = re.compile(rf"^\s{{4}}({_ID}) (-->|-\.->)(\|{_LABEL}\|)? ({_ID})$")
CLASSDEF = re.compile(rf"^\s{{4}}classDef {_ID} [a-z-]+:#[0-9a-f]{{6}}(,[a-z-]+:#[0-9a-f]{{6}})*$")
CLASS = re.compile(rf"^\s{{4}}class {_ID} {_ID}$")


def assert_valid_mermaid(text: str) -> tuple[set[str], list[tuple[str, str, str | None]]]:
    lines = text.rstrip("\n").split("\n")
    assert lines[0] == "flowchart TD"
    nodes: set[str] = set()
    edges: list[tuple[str, str, str | None]] = []
    for line in lines[1:]:
        assert line.count('"') % 2 == 0, line
        if m := NODE.match(line):
            assert m.group(1) not in nodes, f"node defined twice: {line}"
            nodes.add(m.group(1))
        elif m := EDGE.match(line):
            edges.append((m.group(1), m.group(4), m.group(3)))
        else:
            assert CLASSDEF.match(line) or CLASS.match(line), f"not valid Mermaid: {line!r}"
    for src, dst, _ in edges:
        assert src in nodes, f"edge from undefined node {src}"
        assert dst in nodes, f"edge to undefined node {dst}"
    return nodes, edges


def test_cli_graph_return_item_prints_valid_mermaid(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dir", str(REPO / "aops"), "graph", "return_item"]) == 0
    nodes, edges = assert_valid_mermaid(capsys.readouterr().out)
    assert nodes == {"start", "s1", "s2", "s3", "s4", "s5", "s6", "resolve", "escalate"}
    assert ("s2", "resolve", '|"the order is not delivered"|') in edges
    assert ("s3", "escalate", '|"delivered more than 30 days ago"|') in edges
    assert ("s6", "resolve", None) in edges


@pytest.mark.parametrize(
    "aop_id",
    [
        "return_item",
        "where_is_my_order",
        "cancel_order",
        "change_address",
        "refund_request",
        "manage_subscription",
        "loyalty_points",
        "general_question",
    ],
)
def test_every_northwind_graph_is_valid(aop_id: str) -> None:
    aop = compile_aops(REPO / "aops", prompts_dir=REPO / "prompts").get(aop_id)
    _, edges = assert_valid_mermaid(to_mermaid(aop))
    expected = 1 + sum(len(s.transitions) for s in aop.steps)  # +1 for start --> s1
    assert len(edges) == expected


def test_quotes_and_long_text_are_escaped_and_truncated() -> None:
    src = (
        '---\nid: q\nname: Say "hi"\nversion: 1\ntriggers: [x]\n---\n'
        '1. If they say "stop" right now please, ESCALATE. ' + "Lots of words " * 10 + "\n"
        "2. RESOLVE.\n"
    )
    text = to_mermaid(parse_aop(src))
    assert_valid_mermaid(text)
    assert "#quot;stop#quot;" in text
    assert "…" in text


def test_implicit_edges_are_dotted_and_defaults_labelled_otherwise() -> None:
    aop = compile_aops(REPO / "aops", prompts_dir=REPO / "prompts").get("return_item")
    text = to_mermaid(aop)
    assert '    s2 -.->|"otherwise"| s3' in text
    assert "    s4 -.-> s5" in text


def test_unused_terminals_are_not_drawn() -> None:
    src = "---\nid: q\nname: Q\nversion: 1\ntriggers: [x]\n---\n1. RESOLVE.\n"
    nodes, _ = assert_valid_mermaid(to_mermaid(parse_aop(src)))
    assert "escalate" not in nodes
