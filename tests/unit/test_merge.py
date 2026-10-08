"""Tests for findings merge: evidence checking, updates, conflicts, rejections, pruning."""

from __future__ import annotations

import json

import pytest
from diagram_codebase.common import write_json
from diagram_codebase.config import DEFAULTS
from diagram_codebase.model.merge import EvidenceChecker, Merger, merge_all
from diagram_codebase.scan import run_scan

REPO = {
    "app/__init__.py": "",
    "app/service.py": (
        '"""Service."""\n'  # 1
        "\n"  # 2
        "from app.store import save\n"  # 3
        "\n"  # 4
        "\n"  # 5
        "def handle(event):\n"  # 6
        "    data = transform(event)\n"  # 7
        "    save(data)\n"  # 8
        "    return data\n"  # 9
        "\n"  # 10
        "\n"  # 11
        "def transform(event):\n"  # 12
        "    return {'v': event}\n"  # 13
    ),
    "app/store.py": "def save(data):\n    pass\n",
    "README.md": "# Demo\n\nEvents are archived nightly by a cron job.\n",
    ".github/NOTES.md": "Deployment notes.\n",
}


@pytest.fixture
def repo(make_repo, tmp_path):
    root = make_repo(REPO)
    out = tmp_path / "out"
    run_scan(root, out, DEFAULTS)
    return root, out


def merge(repo, **findings):
    root, out = repo
    for name, data in findings.items():
        target = out / "findings" / f"{name}.json"
        if isinstance(data, str):
            target.write_text(data)
        else:
            write_json(target, data)
    return merge_all(root, out)


def ev(file="app/service.py", lines="6-9", symbol="handle", status="confirmed", **kw):
    return {"file": file, "lines": lines, "symbol": symbol, "status": status, **kw}


def node_by_id(model, nid):
    return next((n for n in model["nodes"] if n["id"] == nid), None)


def reasons(report):
    return [r["reason"] for r in report["rejected"]]


# --------------------------------------------------------------- EvidenceChecker


@pytest.fixture
def checker(repo):
    root, out = repo
    inv = json.loads((out / "inventory.json").read_text())
    return EvidenceChecker(root, {f["path"] for f in inv["files"]})


def check(checker, **e):
    e.setdefault("status", "confirmed")
    return checker.check(e)


def test_checker_accepts_exact_and_windowed_symbol(checker):
    assert (
        check(checker, file="app/service.py", line_start=7, line_end=7, symbol="transform") is None
    )
    # `save` is on line 8; citing line 5 is within the +/-3 line window
    assert (
        check(checker, file="app/service.py", line_start=5, line_end=5, symbol="store.save") is None
    )
    assert check(checker, file="./app/service.py", line_start=1, line_end=13) is None


def test_checker_rejects_wrong_file_lines_symbol(checker):
    assert "not in analyzed inventory" in check(checker, file="app/missing.py", line_start=1)
    assert "outside the repository" in check(checker, file="/etc/passwd", line_start=1)
    assert check(checker, file="../outside.md", line_start=1) is not None
    assert "out of range" in check(checker, file="app/service.py", line_start=12, line_end=40)
    assert "out of range" in check(checker, file="app/service.py", line_start=9, line_end=7)
    assert "out of range" in check(checker, file="app/service.py", line_start=0)
    assert "symbol 'register' not found" in check(
        checker, file="app/service.py", line_start=12, line_end=13, symbol="register"
    )
    # symbol present in the file but far from the cited lines
    assert "not found near" in check(checker, file="app/service.py", line_start=13, symbol="handle")
    assert check(checker, file="app/service.py") == "missing line numbers"
    assert check(checker, file="") == "missing file"


def test_checker_docs_and_dot_directories(checker):
    e = {"file": "./.github/NOTES.md", "line_start": 1, "status": "documented_only"}
    assert checker.check(e) is None and e["file"] == ".github/NOTES.md"
    assert check(checker, file="README.md", status="documented_only") is None  # no lines needed


# --------------------------------------------------------------- findings merge


def test_new_nodes_edges_and_documented_only(repo):
    model, report = merge(
        repo,
        flow={
            "nodes": [
                {
                    "id": "data:event",
                    "name": "Event",
                    "kind": "data_artifact",
                    "evidence": [ev(lines="6", symbol="event")],
                },
                {
                    "id": "ext:archive",
                    "name": "Nightly archive",
                    "kind": "external_service",
                    "evidence": [
                        ev(file="README.md", lines="3", symbol="cron", status="confirmed")
                    ],
                },
            ],
            "edges": [
                {
                    "from": "fn:app/service.py:handle",
                    "to": "data:event",
                    "kind": "data_flow",
                    "label": "reads",
                    "phase": "runtime",
                    "evidence": [ev(lines="7", symbol="event")],
                },
                {
                    "from": "fn:app/service.py:handle",
                    "to": "fn:app/service.py:transform",
                    "kind": "calls",
                    "evidence": [ev(lines="7", symbol="", status="confirmed")],
                },
            ],
            "uncertainties": ["Archive job is configured outside the repo."],
        },
    )
    assert report["errors"] == []
    archive = node_by_id(model, "ext:archive")
    assert archive["evidence_status"] == "documented_only"  # docs never count as confirmed
    data_edge = next(e for e in model["edges"] if e["to"] == "data:event")
    assert data_edge["evidence_status"] == "confirmed" and data_edge["label"] == "reads"
    calls = next(
        e
        for e in model["edges"]
        if e["id"] == "e:calls:fn:app/service.py:handle>fn:app/service.py:transform"
    )
    statuses = {x["status"] for x in model["evidence"] if x["id"] in calls["evidence_ids"]}
    assert "static_inferred" in statuses  # confirmed call without a symbol is downgraded
    assert model["meta"]["uncertainties"] == ["Archive job is configured outside the repo."]
    assert model["meta"]["coverage"]["findings_sources"] == ["scan", "flow"]


def test_rejections_are_listed(repo):
    _, report = merge(
        repo,
        bad={
            "nodes": [
                {
                    "id": "algo:ghost",
                    "name": "Ghost",
                    "kind": "algorithm",
                    "evidence": [ev(symbol="nonexistent_fn")],
                },
                {"id": "algo:nokind", "name": "X", "kind": "wizard", "evidence": [ev()]},
                {"id": "algo:noev", "name": "X", "kind": "algorithm"},
                {"name": "no id", "kind": "algorithm", "evidence": [ev()]},
                {
                    "id": "algo:dyn",
                    "name": "D",
                    "kind": "algorithm",
                    "evidence": [ev(status="dynamic_observed")],
                },
                {
                    "id": "algo:st",
                    "name": "S",
                    "kind": "algorithm",
                    "evidence": [ev(status="probably")],
                },
            ],
            "edges": [
                {
                    "from": "fn:app/service.py:handle",
                    "to": "fn:app/store.py:save",
                    "kind": "teleports",
                    "evidence": [ev()],
                },
                {
                    "from": "fn:app/service.py:handle",
                    "to": "fn:app/store.py:save",
                    "kind": "data_flow",
                    "evidence": [ev(file="app/store.py", lines="99")],
                },
                {"to": "fn:app/store.py:save", "kind": "calls", "evidence": [ev()]},
            ],
        },
    )
    r = reasons(report)
    assert "new node without accepted evidence" in r
    assert "invalid kind 'wizard'" in r and "invalid kind 'teleports'" in r
    assert "missing id" in r
    assert "dynamic_observed needs a detail describing the observation" in r
    assert "invalid status 'probably'" in r
    assert any("symbol 'nonexistent_fn' not found" in x for x in r)
    assert any("out of range" in x for x in r)
    assert "edge without accepted evidence" in r
    assert "edge needs string 'from' and 'to'" in r
    assert report["errors"] == []


def test_updates_to_existing_scan_nodes_and_conflicts(repo):
    model, report = merge(
        repo,
        upd={
            "subsystems": [
                {"id": "core", "name": "Core logic", "description": "d", "paths": ["app"]}
            ],
            "nodes": [
                {
                    "id": "fn:app/service.py:handle",
                    "description": "Entry point for events.",
                    "subsystem": "core",
                    "tags": ["hot"],
                },
                {"id": "fn:app/store.py:save", "kind": "class", "name": "save()"},
            ],
            "edges": [
                {
                    "from": "fn:app/service.py:handle",
                    "to": "fn:app/store.py:save",
                    "kind": "calls",
                    "label": "persist",
                    "phase": "runtime",
                },
            ],
        },
    )
    handle = node_by_id(model, "fn:app/service.py:handle")
    assert handle["description"] == "Entry point for events."
    assert handle["subsystem"] == "sub:core" and "hot" in handle["tags"]
    assert node_by_id(model, "fn:app/store.py:save")["kind"] == "function"
    assert any("says fn:app/store.py:save is 'class'" in w for w in report["warnings"])
    scan_edge = next(
        e
        for e in model["edges"]
        if e["id"] == "e:calls:fn:app/service.py:handle>fn:app/store.py:save"
    )
    assert scan_edge["label"] == "persist" and scan_edge["phase"] == "runtime"
    assert {s["id"] for s in model["subsystems"]} >= {"sub:core"}
    assert report["errors"] == []


def test_pruning_of_dangling_references(repo):
    model, report = merge(
        repo,
        prune={
            "nodes": [
                {
                    "id": "algo:a",
                    "name": "A",
                    "kind": "algorithm",
                    "subsystem": "sub:nowhere",
                    "parent": "mod:missing.py",
                    "evidence": [ev()],
                },
            ],
            "edges": [
                {
                    "from": "algo:a",
                    "to": "algo:never-created",
                    "kind": "data_flow",
                    "evidence": [ev()],
                },
            ],
            "flows": [
                {
                    "id": "f",
                    "name": "F",
                    "evidence": [ev()],
                    "steps": [
                        {"from": "algo:a", "to": "fn:app/service.py:handle"},
                        {"from": "algo:a", "to": "x:y"},
                    ],
                },
            ],
            "dataflows": [
                {
                    "id": "d",
                    "name": "D",
                    "evidence": [ev()],
                    "stages": [
                        {"node": "algo:a", "role": "input"},
                        {"node": "x:y", "role": "output"},
                    ],
                    "links": [{"from": "algo:a", "to": "x:y"}],
                },
            ],
            "algorithms": [
                {
                    "id": "al",
                    "name": "Al",
                    "node": "algo:missing",
                    "evidence": [ev()],
                    "stages": [
                        {"id": "s1", "evidence": [ev()]},
                        {"id": "s2", "evidence": [ev(symbol="nope_nope")]},
                    ],
                    "transitions": [{"from": "s1", "to": "s2"}],
                },
                {"id": "unbacked", "name": "U", "stages": [{"id": "s1"}]},
            ],
            "state_machines": [
                {
                    "id": "sm",
                    "name": "SM",
                    "evidence": [ev()],
                    "states": [{"id": "A"}],
                    "transitions": [{"from": "A", "to": "B"}],
                },
            ],
            "erd": {
                "entities": [
                    {"id": "events", "attributes": [], "evidence": [ev()]},
                    {"id": "ghost", "evidence": [ev(file="nope.py")]},
                ],
                "relations": [{"from": "events", "to": "ghost", "cardinality": "one-to-many"}],
            },
        },
    )
    assert report["errors"] == []
    a = node_by_id(model, "algo:a")
    assert a["parent"] is None and a["subsystem"] != "sub:nowhere"
    assert any("unknown subsystem sub:nowhere removed" in w for w in report["warnings"])
    assert any(r["reason"] == "endpoint node missing" for r in report["rejected"])
    assert model["flows"][0]["steps"] == [{"from": "algo:a", "to": "fn:app/service.py:handle"}]
    assert [s["node"] for s in model["dataflows"][0]["stages"]] == ["algo:a"]
    assert model["dataflows"][0]["links"] == []
    algo = model["algorithms"][0]
    assert algo["node"] is None and [s["id"] for s in algo["stages"]] == ["s1"]
    assert algo["transitions"] == []
    assert len(model["algorithms"]) == 1  # "unbacked" rejected
    assert any(r["where"].endswith("algorithms unbacked") for r in report["rejected"])
    assert model["state_machines"][0]["transitions"] == []
    assert [e["id"] for e in model["erd"]["entities"]] == ["events"]
    assert model["erd"]["relations"] == []


def test_non_object_findings_file_is_ignored(repo):
    model, report = merge(repo, junk="[1, 2]")
    assert "junk.json: not a JSON object; ignored" in report["warnings"]
    assert report["errors"] == []


def test_invalid_json_findings_raise_user_error(repo):
    from diagram_codebase.common import DCError

    with pytest.raises(DCError, match="not valid JSON"):
        merge(repo, broken="{nope")


def test_same_structure_id_replaces_with_warning(repo):
    flow = {"id": "f", "name": "F", "evidence": [ev()]}
    root, out = repo
    m = Merger(root, json.loads((out / "inventory.json").read_text()))
    m.add_findings("one", {"flows": [flow]})
    m.add_findings("two", {"flows": [dict(flow, name="F2")]})
    model = m.build()
    assert [f["name"] for f in model["flows"]] == ["F2"]
    assert any("replaces earlier definition" in w for w in m.warnings)


def test_scan_entities_become_erd_with_fk_relations(make_repo, tmp_path):
    root = make_repo(
        {
            "models.py": (
                "from sqlalchemy import Column, ForeignKey, Integer\n"
                "Base = object\n\n\n"
                "class User(Base):\n    __tablename__ = 'users'\n    id = Column(Integer, primary_key=True)\n\n\n"
                "class Post(Base):\n    __tablename__ = 'posts'\n    id = Column(Integer, primary_key=True)\n"
                "    author_id = Column(Integer, ForeignKey('users.id'))\n"
            )
        }
    )
    out = tmp_path / "o"
    run_scan(root, out, DEFAULTS)
    model, report = merge_all(root, out)
    assert [e["id"] for e in model["erd"]["entities"]] == ["posts", "users"]
    assert model["erd"]["relations"] == [
        {
            "from": "users",
            "to": "posts",
            "cardinality": "one-to-many",
            "label": "author_id",
            "identifying": False,
        }
    ]
    assert report["errors"] == []
