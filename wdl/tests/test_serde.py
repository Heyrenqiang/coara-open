from __future__ import annotations

import pytest

from wdl.core import parse_graph, validate_graph
from wdl.core.serde import emit_graph

TWO_NODE_WDL = """\
name: two-node
nodes:
  a:
    task: 起草一段文字
  b:
    task: 润色
    input: "{{steps.a.text}}"
edges:
  - from: a
    to: b
"""


def test_parse_basic() -> None:
    graph = parse_graph(TWO_NODE_WDL)
    assert graph.name == "two-node"
    assert set(graph.nodes) == {"a", "b"}
    assert graph.nodes["b"].input == "{{steps.a.text}}"
    assert len(graph.edges) == 1
    assert graph.edges[0].frm == "a"
    assert graph.edges[0].to == "b"


def test_validate_ok() -> None:
    issues = validate_graph(parse_graph(TWO_NODE_WDL))
    assert [i for i in issues if i.level == "error"] == []


def test_validate_bad_edge_ref() -> None:
    bad = """\
name: bad
nodes:
  a:
    task: x
edges:
  - from: a
    to: ghost
"""
    issues = validate_graph(parse_graph(bad))
    assert any(i.level == "error" and "ghost" in i.message for i in issues)


def test_parse_missing_name() -> None:
    with pytest.raises(ValueError, match="name"):
        parse_graph("nodes:\n  a:\n    task: x\n")


def test_round_trip_identity() -> None:
    graph = parse_graph(TWO_NODE_WDL)
    assert emit_graph(parse_graph(emit_graph(graph))) == emit_graph(graph)
