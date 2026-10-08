"""Tests for the model schema constructors, derived fields and validator."""

from __future__ import annotations

import copy

import pytest
from diagram_codebase.model import schema
from diagram_codebase.model.builder import ModelBuilder


def good_model():
    b = ModelBuilder()
    ev = b.ev("a.py", 1, 3, symbol="f")
    weak = b.ev("a.py", 5, 5, symbol="g", status="static_inferred")
    b.subsystem("sub:core", "Core", paths=["."])
    b.node("mod:a.py", "a", "module", evidence_ids=[ev], subsystem="sub:core")
    b.node("fn:a.py:f", "f()", "function", parent="mod:a.py", evidence_ids=[ev])
    b.node("fn:a.py:g", "g()", "function", parent="mod:a.py", evidence_ids=[weak])
    b.node("store:db", "DB", "datastore", evidence_ids=[ev])
    b.edge("fn:a.py:f", "fn:a.py:g", "calls", evidence_ids=[weak, ev], phase="runtime")
    b.edge("fn:a.py:g", "store:db", "db_read", evidence_ids=[weak])
    model = schema.empty_model()
    model.update(b.to_fragment())
    return model


def test_constructors_and_ids():
    e1 = schema.evidence("a.py", 3, symbol="x")
    e2 = schema.evidence("a.py", 3, 3, symbol="x")
    assert e1["id"] == e2["id"] and e1["line_end"] == 3
    assert schema.evidence("a.py", 4, symbol="x")["id"] != e1["id"]
    n = schema.node("fn:a", "a", "function", tags=["b", "a", "a"], symbols=["s"], file="a.py")
    assert n["tags"] == ["a", "b"] and n["symbols"] == ["s"] and n["metadata"] == {"file": "a.py"}
    e = schema.edge("x", "y", "calls", payload=["b", "a", "b"])
    assert e["id"] == "e:calls:x>y" and e["payload"] == ["a", "b"] and e["phase"] == "unknown"


def test_valid_model_has_no_errors():
    rep = schema.validate_model(good_model())
    assert rep["errors"] == [] and rep["warnings"] == []
    assert rep["stats"]["nodes"] == 4 and rep["stats"]["edge_kinds"] == {"calls": 1, "db_read": 1}


def test_apply_derived_uses_strongest_status():
    m = good_model()
    schema.apply_derived(m)
    edges = {e["id"]: e for e in m["edges"]}
    calls = edges["e:calls:fn:a.py:f>fn:a.py:g"]
    assert calls["evidence_status"] == "confirmed" and calls["confidence"] == "high"
    assert calls["evidence_counts"] == {"static_inferred": 1, "confirmed": 1}
    read = edges["e:db_read:fn:a.py:g>store:db"]
    assert read["evidence_status"] == "static_inferred" and read["confidence"] == "medium"
    assert schema.strongest_status([]) == "unknown"


def mutate(fn):
    m = good_model()
    fn(m)
    return schema.validate_model(m)


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        (lambda m: m["nodes"].append(copy.deepcopy(m["nodes"][0])), "duplicate node id mod:a.py"),
        (lambda m: m["edges"].append(copy.deepcopy(m["edges"][0])), "duplicate edge id"),
        (lambda m: m["evidence"].append(copy.deepcopy(m["evidence"][0])), "duplicate evidence id"),
        (lambda m: m["subsystems"].append(dict(m["subsystems"][0])), "duplicate subsystem id"),
        (lambda m: m["nodes"][0].update(kind="widget"), "invalid kind 'widget'"),
        (lambda m: m["edges"][0].update(kind="teleports"), "invalid kind 'teleports'"),
        (lambda m: m["edges"][0].update(phase="later"), "invalid phase 'later'"),
        (
            lambda m: m["edges"][0].update(to="fn:missing"),
            "to references missing node 'fn:missing'",
        ),
        (lambda m: m["nodes"][1].update(parent="mod:nope"), "unknown parent mod:nope"),
        (lambda m: m["nodes"][0].update(subsystem="sub:nope"), "unknown subsystem 'sub:nope'"),
        (lambda m: m["nodes"][0].update(evidence_ids=["ev:nope"]), "unknown evidence id ev:nope"),
        (lambda m: m["nodes"][0].update(evidence_ids=[]), "no evidence"),
        (lambda m: m["edges"][0].update(evidence_ids=[]), "no evidence"),
        (lambda m: m["evidence"][0].update(status="probably"), "invalid status 'probably'"),
        (lambda m: m["evidence"][0].update(source="vibes"), "invalid source 'vibes'"),
        (lambda m: m["evidence"][0].update(file=""), "missing file"),
        (lambda m: m["nodes"][0].update(name=""), "id and name are required"),
        (lambda m: m["nodes"].append("not a dict"), "expected dict"),
    ],
)
def test_validator_negatives(mutation, needle):
    rep = mutate(mutation)
    assert any(needle in e for e in rep["errors"]), rep["errors"]


def test_missing_collections_and_non_dict():
    assert schema.validate_model([])["errors"] == ["model is not a JSON object"]
    rep = schema.validate_model({"nodes": [], "edges": {}, "evidence": [], "subsystems": []})
    assert rep["errors"] == ["edges: expected list, got dict"]


def test_non_strict_evidence_allows_unbacked_items():
    m = good_model()
    m["nodes"][0]["evidence_ids"] = []
    assert schema.validate_model(m, strict_evidence=False)["errors"] == []


def test_orphan_warnings_only_for_components():
    m = good_model()
    ev = m["evidence"][0]["id"]
    m["nodes"].append(schema.node("ext:lonely", "Lonely", "external_service", evidence_ids=[ev]))
    m["nodes"].append(schema.node("cfg:env:X", "X", "config", evidence_ids=[ev]))
    m["nodes"].append(schema.node("algo:x", "X", "algorithm", evidence_ids=[ev]))
    m["nodes"].append(schema.node("ros:in_flow", "n", "ros_node", evidence_ids=[ev]))
    m["flows"] = [{"id": "f", "name": "F", "steps": [{"from": "ros:in_flow", "to": "fn:a.py:f"}]}]
    rep = schema.validate_model(m)
    assert rep["errors"] == []
    assert rep["warnings"] == [
        "orphaned component ext:lonely (external_service) has no relationships"
    ]


def test_structure_validation():
    m = good_model()
    m["flows"] = [
        {"id": "f1", "name": "", "steps": [{"from": "fn:a.py:f", "to": "fn:x", "kind": "teleport"}]}
    ]
    m["dataflows"] = [
        {
            "id": "d1",
            "stages": [{"node": "fn:a.py:f", "role": "input"}, {"node": "fn:zz", "role": "magic"}],
            "links": [{"from": "fn:a.py:f", "to": "fn:a.py:g"}],
        }
    ]
    m["algorithms"] = [
        {
            "id": "a1",
            "node": "algo:none",
            "stages": [{"id": "s1", "kind": "step"}, {"id": "s1", "kind": "wiggle"}],
            "transitions": [{"from": "s1", "to": "s9"}],
        }
    ]
    m["state_machines"] = [
        {"id": "sm", "states": [{"id": "Idle"}], "transitions": [{"from": "Idle", "to": "Run"}]}
    ]
    m["erd"] = {"entities": [{"id": "users"}], "relations": [{"from": "users", "to": "orders"}]}
    errors = schema.validate_model(m)["errors"]
    expected = [
        "flows[0] (f1): id and name are required",
        "flows[0] (f1) step 0: to references missing node 'fn:x'",
        "flows[0] (f1) step 0: invalid kind 'teleport'",
        "dataflows[0] (d1) stage 1: missing node 'fn:zz'",
        "dataflows[0] (d1) stage 1: invalid role 'magic'",
        "dataflows[0] (d1) link 0: endpoints must be stages of this dataflow",
        "algorithms[0] (a1): missing node 'algo:none'",
        "algorithms[0] (a1): duplicate stage ids",
        "algorithms[0] (a1) stage s1: invalid kind 'wiggle'",
        "algorithms[0] (a1): transition s1->s9 references unknown stage",
        "state_machines[0] (sm): transition Idle->Run references unknown state",
        "erd relation users->orders references unknown entity",
    ]
    assert errors == expected


def test_builder_dedupes_and_unions():
    b = ModelBuilder()
    e1, e2 = b.ev("a.py", 1), b.ev("a.py", 2)
    b.node("n", "N", "module", evidence_ids=[e1], tags=["x"])
    b.node("n", "N2", "function", evidence_ids=[e2], tags=["y"], description="d")
    n = b.nodes["n"]
    assert n["kind"] == "module" and n["name"] == "N" and n["description"] == "d"
    assert n["evidence_ids"] == [e1, e2] and n["tags"] == ["x", "y"]
    assert b.conflicts == ["node n: kind 'module' vs 'function' (kept 'module')"]
    b.edge("n", "m", "calls", evidence_ids=[e1], payload=["a"])
    b.edge("n", "m", "calls", evidence_ids=[e2], payload=["b"], phase="runtime", label="L")
    e = b.edges["e:calls:n>m"]
    assert e["evidence_ids"] == [e1, e2] and e["payload"] == ["a", "b"]
    assert e["phase"] == "runtime" and e["label"] == "L"
    assert b.edge("n", "n", "imports") is None and b.edge("n", "n", "calls") is not None
