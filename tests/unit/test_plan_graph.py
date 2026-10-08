"""Unit tests for plan/graph.py."""

from __future__ import annotations

from diagram_codebase.plan.graph import (
    connected_components,
    is_dag,
    jaccard,
    label_propagation,
    pack,
    strongly_connected,
)


def test_strongly_connected_finds_cycles_and_self_loops():
    nodes = ["a", "b", "c", "d", "e"]
    edges = [("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("e", "e")]
    sccs = sorted(strongly_connected(nodes, edges))
    assert sccs == [["a", "b", "c"], ["e"]]


def test_is_dag():
    assert is_dag(["a", "b", "c"], [("a", "b"), ("b", "c"), ("a", "c")])
    assert not is_dag(["a", "b"], [("a", "b"), ("b", "a")])


def test_label_propagation_respects_max_size_and_is_deterministic():
    left = [f"l{i}" for i in range(6)]
    right = [f"r{i}" for i in range(6)]
    edges = [(a, b) for a in left for b in left if a < b] + [
        (a, b) for a in right for b in right if a < b
    ]
    edges.append(("l0", "r0"))
    nodes = left + right
    groups = label_propagation(nodes, edges, max_size=6)
    assert all(len(g) <= 6 for g in groups)
    assert sorted(x for g in groups for x in g) == sorted(nodes)
    assert groups == label_propagation(list(reversed(nodes)), edges, max_size=6)
    assert {frozenset(g) for g in groups} == {frozenset(left), frozenset(right)}


def test_connected_components_largest_first():
    comps = connected_components(["a", "b", "c", "d", "e"], [("a", "b"), ("b", "c"), ("d", "e")])
    assert comps == [["a", "b", "c"], ["d", "e"]]
    assert connected_components(["x"], []) == [["x"]]


def test_pack_first_fit_and_oversized_groups():
    bins = pack([["a", "b", "c"], ["d", "e"], ["f"], ["g", "h", "i", "j", "k"]], 4)
    assert all(len(b) <= 4 for b in bins)
    assert sorted(x for b in bins for x in b) == list("abcdefghijk")
    # Small groups stay whole.
    assert any({"d", "e"} <= set(b) for b in bins)


def test_jaccard():
    assert jaccard(["a", "b"], ["a", "b"]) == 1.0
    assert jaccard(["a"], ["b"]) == 0.0
    assert jaccard(["a", "b", "c"], ["a", "b"]) == 2 / 3
    assert jaccard([], []) == 1.0
