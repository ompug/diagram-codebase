"""Unit tests for plan/planner.py (plus ModelView/archmode behavior the planner relies on)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from diagram_codebase import config as dc_config
from diagram_codebase.model import schema
from diagram_codebase.plan import archmode
from diagram_codebase.plan.planner import _Planner, plan_diagrams
from diagram_codebase.plan.view import ModelView

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_A = ROOT / "tests" / "fixtures" / "a_python_app"

KIND_BY_PREFIX = {
    "mod": "module", "fn": "function", "cls": "class", "api": "api_endpoint", "ros": "ros_node",
    "topic": "topic", "srv": "ros_service", "action": "ros_action", "tf": "tf_frame",
    "chan": "event_channel", "store": "datastore", "ent": "db_entity", "ext": "external_service",
    "cfg": "config", "param": "config", "deploy": "deployment_unit", "launch": "deployment_unit",
}  # fmt: skip


def mk(nodes, edges, subsystems=(), **extra):
    """Build a small valid model.

    nodes: [id | (id, {overrides})]. Kind comes from the id prefix; code nodes get a
    file and a parent module derived from the id. edges: (from, to, kind[, {overrides}]).
    """
    model = schema.empty_model()
    model["meta"] = {"repo": {"name": "demo"}, "revision": "abc123"}
    model["subsystems"] = [
        {
            "id": s,
            "name": s.split(":", 1)[1].title(),
            "description": "",
            "paths": [s.split(":", 1)[1]],
        }
        for s in subsystems
    ]
    ids = []
    for i, spec in enumerate(nodes):
        nid, over = (spec, {}) if isinstance(spec, str) else spec
        ids.append(nid)
        prefix = nid.split(":", 1)[0]
        kind = over.pop("kind", KIND_BY_PREFIX.get(prefix, "algorithm"))
        name = over.pop("name", None)
        meta = {}
        parent = over.pop("parent", None)
        if prefix in ("mod", "fn", "cls"):
            path = nid.split(":")[1]
            meta["file"] = path
            if prefix != "mod" and parent is None:
                parent = f"mod:{path}"
            if name is None:
                name = path.rsplit("/", 1)[-1] if prefix == "mod" else nid.split(":", 2)[2]
                if prefix == "fn":
                    name += "()"
        ev = schema.evidence(f"f{i}.py", i + 1, symbol=nid)
        model["evidence"].append(ev)
        node = schema.node(
            nid, name or nid.split(":", 1)[-1], kind, parent=parent, evidence_ids=[ev["id"]],
            subsystem=over.pop("subsystem", None), tags=over.pop("tags", None), **meta,
        )  # fmt: skip
        node.update(over)
        model["nodes"].append(node)
    for nid in list(ids):
        n = next(x for x in model["nodes"] if x["id"] == nid)
        if n["parent"] and n["parent"] not in ids:
            ev = schema.evidence("auto.py", 1, symbol=n["parent"])
            model["evidence"].append(ev)
            path = n["parent"].split(":")[1]
            model["nodes"].append(
                schema.node(
                    n["parent"],
                    path.rsplit("/", 1)[-1],
                    "module",
                    evidence_ids=[ev["id"]],
                    subsystem=n["subsystem"],
                    file=path,
                )  # fmt: skip
            )
            ids.append(n["parent"])
    for j, spec in enumerate(edges):
        a, b, kind = spec[:3]
        over = dict(spec[3]) if len(spec) > 3 else {}
        ev = schema.evidence("e.py", over.pop("line", j + 1), symbol=f"{a}>{b}")
        if ev["id"] not in {x["id"] for x in model["evidence"]}:
            model["evidence"].append(ev)
        model["edges"].append(schema.edge(a, b, kind, evidence_ids=[ev["id"]], **over))
    model.update(extra)
    schema.apply_derived(model)
    result = schema.validate_model(model)
    assert not result["errors"], result["errors"]
    return model


def cfg(**plan):
    c = dc_config.load_config(None)
    c["plan"].update(plan)
    return c


def opts(depth="standard", types=(), focus=None):
    return {"depth": depth, "types": list(types), "focus": focus}


def by_id(plan):
    return {d["id"]: d for d in plan["diagrams"]}


def edge_pairs(spec):
    return {(e["from"], e["to"]) for e in spec["edges"]}


def skipped_ids(plan):
    return {s.get("id") for s in plan["skipped"]}


# ------------------------------------------------------------------ fixtures


def web_model():
    """Three subsystems: backend (API + repo), worker (event consumer), billing."""
    return mk(
        [
            ("fn:backend/routes.py:list_todos", {"subsystem": "sub:backend"}),
            ("fn:backend/routes.py:create_todo", {"subsystem": "sub:backend"}),
            ("fn:backend/repo.py:query", {"subsystem": "sub:backend"}),
            ("fn:backend/repo.py:insert", {"subsystem": "sub:backend"}),
            ("fn:backend/bill.py:charge_client", {"subsystem": "sub:backend"}),
            ("api:GET /api/todos", {"name": "GET /api/todos"}),
            ("fn:worker/consumer.py:on_created", {"subsystem": "sub:worker"}),
            ("fn:worker/consumer.py:notify", {"subsystem": "sub:worker"}),
            ("fn:billing/charge.py:charge", {"subsystem": "sub:billing"}),
            ("fn:billing/charge.py:audit", {"subsystem": "sub:billing"}),
            ("store:postgres", {"name": "PostgreSQL"}),
            ("chan:todo.created", {"name": "todo.created"}),
            ("ext:mail.example.com", {"name": "mail.example.com"}),
            ("cfg:env:DB_URL", {"name": "DB_URL", "subsystem": "sub:backend"}),
        ],
        [
            ("api:GET /api/todos", "fn:backend/routes.py:list_todos", "handles"),
            ("fn:backend/routes.py:list_todos", "fn:backend/repo.py:query", "calls"),
            ("fn:backend/routes.py:create_todo", "fn:backend/repo.py:insert", "calls"),
            ("fn:backend/repo.py:query", "store:postgres", "db_read", {"payload": ["todos"]}),
            ("fn:backend/repo.py:insert", "store:postgres", "db_write", {"payload": ["todos"]}),
            ("fn:backend/routes.py:create_todo", "chan:todo.created", "publishes"),
            ("chan:todo.created", "fn:worker/consumer.py:on_created", "subscribes"),
            ("fn:worker/consumer.py:on_created", "fn:worker/consumer.py:notify", "calls"),
            ("fn:worker/consumer.py:notify", "ext:mail.example.com", "external_call"),
            ("fn:backend/routes.py:create_todo", "fn:backend/bill.py:charge_client", "calls"),
            ("fn:backend/bill.py:charge_client", "fn:billing/charge.py:charge", "calls"),
            ("fn:billing/charge.py:charge", "fn:billing/charge.py:audit", "calls"),
            ("fn:backend/repo.py:query", "cfg:env:DB_URL", "config_dependency"),
        ],
        subsystems=["sub:backend", "sub:worker", "sub:billing"],
    )


# ------------------------------------------------------------------ integration


@pytest.fixture(scope="module")
def fixture_a_model(tmp_path_factory):
    from diagram_codebase import scan
    from diagram_codebase.model import merge

    out = tmp_path_factory.mktemp("dc-a")
    scan.run_scan(FIXTURE_A, out, dc_config.load_config(None))
    model, validation = merge.merge_all(FIXTURE_A, out)
    assert not validation["errors"]
    return model


def test_fixture_a_master_shows_main_path(fixture_a_model):
    plan = plan_diagrams(fixture_a_model, opts(), dc_config.load_config(None))
    ds = by_id(plan)
    master = ds["master"]
    assert plan["diagrams"][0]["id"] == "master" and master["priority"] == 0 and master["row"] == 0
    assert master["level"] == "module" and master["renderer"] == "flowchart"
    labels = {n["id"]: n["label"] for n in master["nodes"]}
    assert labels["mod:app/service.py"] == "service.py"
    assert labels["store:sqlite"] == "SQLite"
    pairs = edge_pairs(master)
    assert ("mod:app/service.py", "mod:app/repository.py") in pairs
    assert ("mod:app/repository.py", "store:sqlite") in pairs
    assert ("mod:app/notifier.py", "ext:hooks.example.com") in pairs
    shapes = {n["id"]: n["shape"] for n in master["nodes"]}
    assert shapes["store:sqlite"] == "cylinder"
    assert not any(n["id"].startswith("cfg:") for n in master["nodes"])
    # Import-only edge is dotted and labelled "imports"; a runtime edge is solid.
    e = {(x["from"], x["to"]): x for x in master["edges"]}
    assert e[("mod:app/forecast.py", "mod:app/models.py")]["style"] == "dotted"
    assert e[("mod:app/forecast.py", "mod:app/models.py")]["label"] == "imports"
    assert e[("mod:app/service.py", "mod:app/repository.py")]["style"] == "solid"
    assert "subsystem-app" in ds and ds["subsystem-app"]["level"] == "symbol"
    # The derived execution trace covers the same symbols as subsystem-app: skipped.
    skipped = {x.get("id"): x["reason"] for x in plan["skipped"]}
    assert "execution-main" not in ds and "overlaps 'subsystem-app'" in skipped["execution-main"]
    # Every diagram has a purpose; content_hash left for dc.py plan.
    assert all(d["purpose"] and d["content_hash"] == "" for d in plan["diagrams"])
    cov = plan["coverage"]
    assert cov["model_nodes"] == len(fixture_a_model["nodes"])
    assert cov["any_diagram"]["of"] == cov["model_nodes"]
    assert "cfg:env:INVENTORY_DB" in cov["only_in_model"]["ids"]
    assert cov["any_diagram"]["count"] + cov["only_in_model"]["count"] == cov["model_nodes"]


def test_fixture_a_is_deterministic(fixture_a_model):
    a = plan_diagrams(fixture_a_model, opts("deep"), dc_config.load_config(None))
    b = plan_diagrams(copy.deepcopy(fixture_a_model), opts("deep"), dc_config.load_config(None))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ------------------------------------------------------------------ master / archmode


def test_master_subsystem_level_uses_architecture_when_eligible():
    plan = plan_diagrams(web_model(), opts(), cfg(master_target_min=3))
    master = by_id(plan)["master"]
    assert master["level"] == "subsystem"
    assert master["renderer"] == "architecture"
    assert master["lanes"]["store:postgres"] == "datastore"
    assert master["lanes"]["chan:todo.created"] == "async"
    assert master["lanes"]["sub:backend"] == "service"
    assert master["groups"] == []
    for e in master["edges"]:
        touches = {master["lanes"][e["from"]], master["lanes"][e["to"]]}
        assert (e["style"] == "dotted") == bool(touches & {"async", "external"})


def test_master_flowchart_with_groups_when_architecture_ineligible():
    model = web_model()
    # A second datastore plus a billing -> backend call creates a forward cycle.
    extra = mk(
        [
            ("fn:billing/charge.py:charge", {"subsystem": "sub:billing"}),
            ("fn:backend/repo.py:query", {"subsystem": "sub:backend"}),
            ("store:redis", {"name": "Redis"}),
        ],
        [
            ("fn:billing/charge.py:charge", "fn:backend/repo.py:query", "calls"),
            ("fn:billing/charge.py:charge", "store:redis", "db_write"),
        ],
        subsystems=["sub:backend", "sub:billing"],
    )
    model["evidence"] += extra["evidence"]
    model["nodes"].append(next(n for n in extra["nodes"] if n["id"] == "store:redis"))
    model["edges"] += extra["edges"]
    plan = plan_diagrams(model, opts(), cfg(master_target_min=3))
    master = by_id(plan)["master"]
    assert master["renderer"] == "flowchart"
    groups = {g["label"]: g for g in master["groups"]}
    assert groups["Data stores"]["category"] == "data"
    grouped = {n["id"] for n in master["nodes"] if n["group"] == groups["Data stores"]["id"]}
    assert grouped == {"store:postgres", "store:redis"}


def test_archmode_check_eligible_and_reasons():
    model = web_model()
    v = ModelView(model)
    units, edges = v.aggregate("subsystem")
    ok, lanes, reasons = archmode.check(v, units, edges, 20)
    assert ok and not reasons
    bad = edges + [
        {"from": "store:postgres", "to": "ext:mail.example.com", "label": "x"},
    ]
    ok, _lanes, reasons = archmode.check(v, units, bad, 20)
    assert not ok and any("not an allowed lane pair" in r for r in reasons)
    ok, _lanes, reasons = archmode.check(v, units, edges, 2)
    assert not ok and any("edges >" in r for r in reasons)


def test_master_module_level_groups_modules_by_subsystem():
    model = mk(
        [
            ("fn:a/x.py:f", {"subsystem": "sub:a"}),
            ("fn:a/y.py:g", {"subsystem": "sub:a"}),
            ("fn:b/z.py:h", {"subsystem": "sub:b"}),
            ("fn:b/w.py:k", {"subsystem": "sub:b"}),
        ],
        [
            ("fn:a/x.py:f", "fn:a/y.py:g", "calls"),
            ("fn:a/y.py:g", "fn:b/z.py:h", "calls"),
            ("fn:b/z.py:h", "fn:b/w.py:k", "calls"),
        ],
        subsystems=["sub:a", "sub:b"],
    )
    master = by_id(plan_diagrams(model, opts(), cfg()))["master"]
    assert master["level"] == "module"
    assert {g["label"] for g in master["groups"]} == {"A", "B"}


def test_empty_model_skips_master():
    plan = plan_diagrams(schema.empty_model(), opts(), cfg())
    assert plan["diagrams"] == []
    assert "master" in skipped_ids(plan)


# ------------------------------------------------------------------ fit


def _planner(model, **plan_cfg):
    return _Planner(model, opts(), cfg(**plan_cfg))


def _agg(a, b, **kw):
    e = {"from": a, "to": b, "label": kw.pop("label", "calls"), "model_edges": [f"e:{a}>{b}"],
         "import_only": False, "async": False, "error": False, "weight": 1,
         "evidence_status": "confirmed", "kind": "calls"}  # fmt: skip
    e.update(kw)
    return e


def test_fit_drops_config_then_collapses_channels_then_trims_by_degree():
    model = mk(
        ["fn:p.py:a", "fn:p.py:b", "fn:p.py:c", "fn:p.py:d", "chan:q", "cfg:env:X", "cfg:env:Y"],
        [],
    )
    p = _planner(model)
    units = ["fn:p.py:a", "fn:p.py:b", "fn:p.py:c", "fn:p.py:d", "chan:q", "cfg:env:X", "cfg:env:Y"]
    edges = [
        _agg("fn:p.py:a", "chan:q", kind="publishes", **{"async": True}),
        _agg("chan:q", "fn:p.py:b", kind="subscribes", **{"async": True}),
        _agg("fn:p.py:a", "fn:p.py:c"),
        _agg("fn:p.py:a", "fn:p.py:d"),
        _agg("fn:p.py:c", "fn:p.py:d"),
        _agg("fn:p.py:a", "cfg:env:X"),
        _agg("fn:p.py:a", "cfg:env:Y"),
    ]
    u, e, notes = p._fit(units, edges, max_nodes=4)
    assert u == ["fn:p.py:a", "fn:p.py:b", "fn:p.py:c", "fn:p.py:d"]
    collapsed = next(x for x in e if (x["from"], x["to"]) == ("fn:p.py:a", "fn:p.py:b"))
    assert collapsed["label"] == "q" and collapsed["async"]
    assert len(collapsed["model_edges"]) == 2
    assert any("configuration units omitted" in n for n in notes)
    assert any("channels drawn as direct" in n for n in notes)
    u, e, notes = p._fit(units, edges, max_nodes=2)
    assert len(u) == 2 and "fn:p.py:a" in u
    assert any("least-connected units omitted" in n for n in notes)


def test_fit_drops_import_only_edges_first():
    model = mk(["fn:p.py:a", "fn:p.py:b", "fn:p.py:c"], [])
    p = _planner(model)
    edges = [
        _agg("fn:p.py:a", "fn:p.py:b", import_only=True),
        _agg("fn:p.py:b", "fn:p.py:c", weight=5),
        _agg("fn:p.py:a", "fn:p.py:c", weight=2),
    ]
    _u, e, notes = p._fit(["fn:p.py:a", "fn:p.py:b", "fn:p.py:c"], edges, max_edges=2)
    assert {(x["from"], x["to"]) for x in e} == {
        ("fn:p.py:b", "fn:p.py:c"),
        ("fn:p.py:a", "fn:p.py:c"),
    }
    assert "1 import-only" in notes[0]
    _u, e, notes = p._fit(["fn:p.py:a", "fn:p.py:b", "fn:p.py:c"], edges, max_edges=1)
    assert [(x["from"], x["to"]) for x in e] == [("fn:p.py:b", "fn:p.py:c")]
    assert "lowest-weight" in notes[0]


# ------------------------------------------------------------------ subsystems


def test_subsystem_diagram_symbol_level_with_context_and_module_groups():
    plan = plan_diagrams(web_model(), opts(), cfg(master_target_min=3))
    sub = by_id(plan)["subsystem-backend"]
    assert sub["level"] == "symbol" and sub["row"] == 1
    cats = {n["id"]: n["category"] for n in sub["nodes"]}
    assert cats["sub:billing"] == "context"
    assert cats["store:postgres"] == "data"
    groups = {g["label"] for g in sub["groups"]}
    assert {"routes.py", "repo.py"} <= groups
    # The endpoint is drawn at symbol level, handled by its function.
    assert ("api:GET /api/todos", "fn:backend/routes.py:list_todos") in edge_pairs(sub)


def test_subsystem_too_large_splits_into_parts():
    nodes, edges = [], []
    for d in ("core", "io"):
        for i in range(8):
            nodes.append((f"fn:big/{d}/m{i}.py:f", {"subsystem": "sub:big"}))
            if i:
                edges.append((f"fn:big/{d}/m{i - 1}.py:f", f"fn:big/{d}/m{i}.py:f", "calls"))
    edges.append(("fn:big/core/m0.py:f", "fn:big/io/m0.py:f", "calls"))
    model = mk(nodes, edges, subsystems=["sub:big"])
    plan = plan_diagrams(model, opts(types=["subsystem"]), cfg(max_nodes=10))
    ids = [d["id"] for d in plan["diagrams"]]
    assert ids == ["subsystem-big-part-1", "subsystem-big-part-2"]
    for d in plan["diagrams"]:
        assert d["level"] == "module" and len(d["nodes"]) <= 10
        assert any("split into 2 diagrams" in n for n in d["notes"])


def test_subsystem_cap_and_trivial_skips():
    plan = plan_diagrams(
        web_model(), opts(), cfg(max_subsystem_diagrams={"overview": 0, "standard": 1, "deep": 12})
    )
    ids = by_id(plan)
    assert sum(1 for d in plan["diagrams"] if d["type"] == "subsystem") == 1
    assert "subsystem-backend" in ids
    reasons = [s["reason"] for s in plan["skipped"] if s["type"] == "subsystem"]
    assert any("max_subsystem_diagrams" in r for r in reasons)


# ------------------------------------------------------------------ dataflow


def test_derived_dataflow_orients_reads_and_marks_sources_and_sinks():
    plan = plan_diagrams(web_model(), opts(types=["dataflow"]), cfg())
    df = by_id(plan)["dataflow-derived"]
    pairs = edge_pairs(df)
    assert ("store:postgres", "fn:backend/repo.py:query") in pairs  # db_read reversed
    shapes = {n["id"]: n["shape"] for n in df["nodes"]}
    assert shapes["store:postgres"] == "cylinder"
    assert shapes["fn:backend/routes.py:create_todo"] == "lean_r"


def test_derived_dataflow_skipped_when_too_small():
    model = mk(["fn:a.py:f", "store:s"], [("fn:a.py:f", "store:s", "db_write")])
    plan = plan_diagrams(model, opts(types=["dataflow"]), cfg())
    assert not plan["diagrams"]
    assert "fewer than 4" in plan["skipped"][0]["reason"]


def test_derived_dataflow_skipped_when_redundant():
    model = web_model()
    p = _Planner(model, opts(types=["dataflow"]), cfg())
    units, _ = p._data_edges("symbol")
    p.candidates.append({"id": "other", "model_nodes": sorted(units), "_tier": 0, "_order": 0})
    p._dataflows()
    assert any("overlaps 'other'" in s["reason"] for s in p.skipped)


def test_provided_dataflow_roles_and_representation_labels():
    model = web_model()
    model["dataflows"] = [
        {
            "id": "ingest",
            "name": "Todo ingest",
            "stages": [
                {
                    "node": "fn:backend/routes.py:create_todo",
                    "role": "input",
                    "representation": "JSON",
                },
                {"node": "fn:backend/repo.py:insert", "role": "transform", "representation": "Row"},
                {"node": "store:postgres", "role": "storage"},
            ],
            "links": [
                {"from": "fn:backend/routes.py:create_todo", "to": "fn:backend/repo.py:insert"},
                {"from": "fn:backend/repo.py:insert", "to": "store:postgres", "label": "INSERT"},
            ],
        }
    ]
    df = by_id(plan_diagrams(model, opts(types=["dataflow"]), cfg()))["dataflow-ingest"]
    shapes = {n["id"]: n["shape"] for n in df["nodes"]}
    assert shapes["fn:backend/routes.py:create_todo"] == "lean_r"
    assert shapes["store:postgres"] == "cylinder"
    labels = {(e["from"], e["to"]): e["label"] for e in df["edges"]}
    assert labels[("fn:backend/routes.py:create_todo", "fn:backend/repo.py:insert")] == "JSON"
    assert labels[("fn:backend/repo.py:insert", "store:postgres")] == "INSERT"


# ------------------------------------------------------------------ execution


def exec_model():
    return mk(
        [
            ("fn:app/main.py:main", {"tags": ["entrypoint"]}),
            ("mod:app/main.py", {"tags": ["entrypoint"]}),
            "fn:app/main.py:setup",
            "fn:app/svc.py:run",
            "fn:app/svc.py:step_b",
            "fn:app/svc.py:step_a",
            "store:db",
        ],
        [
            ("fn:app/main.py:main", "fn:app/svc.py:run", "calls", {"line": 30, "phase": "runtime"}),
            ("fn:app/main.py:main", "fn:app/main.py:setup", "calls", {"line": 10, "phase": "init"}),
            ("fn:app/svc.py:run", "fn:app/svc.py:step_b", "calls", {"line": 50}),
            ("fn:app/svc.py:run", "fn:app/svc.py:step_a", "calls", {"line": 40}),
            ("fn:app/svc.py:step_a", "store:db", "db_write", {"line": 60}),
            ("mod:app/main.py", "fn:app/main.py:main", "calls", {"line": 99}),
        ],
    )


def test_derived_execution_from_function_entrypoint():
    plan = plan_diagrams(exec_model(), opts(types=["execution"]), cfg())
    ex = by_id(plan)["execution-main"]
    assert ex["direction"] == "TD" and ex["row"] == 2
    ids = {n["id"] for n in ex["nodes"]}
    assert "mod:app/main.py" not in ids  # function entry point preferred
    assert {"store:db", "fn:app/svc.py:step_a"} <= ids
    shapes = {n["id"]: n["shape"] for n in ex["nodes"]}
    assert shapes["fn:app/main.py:main"] == "stadium"
    # Derived traces are not boxed by phase: a scanned edge's phase records when a
    # relationship is set up, not when its target runs.
    assert not any(g["label"] in ("Initialization", "Runtime") for g in ex["groups"])


def test_trace_orders_children_by_evidence_line():
    p = _Planner(exec_model(), opts(), cfg())
    order, _edges, _ph, _notes = p._trace("fn:app/main.py:main")
    assert order[:5] == [
        "fn:app/main.py:main",
        "fn:app/main.py:setup",
        "fn:app/svc.py:run",
        "fn:app/svc.py:step_a",
        "fn:app/svc.py:step_b",
    ]


def test_provided_execution_flow_styles():
    model = exec_model()
    model["flows"] = [
        {
            "id": "flow-run",
            "name": "Run",
            "kind": "execution",
            "trigger": "CLI run",
            "steps": [
                {
                    "from": "fn:app/main.py:main",
                    "to": "fn:app/svc.py:run",
                    "kind": "decision",
                    "condition": "if ok",
                },
                {"from": "fn:app/svc.py:run", "to": "fn:app/svc.py:step_a", "kind": "async"},
                {
                    "from": "fn:app/svc.py:run",
                    "to": "fn:app/svc.py:step_b",
                    "kind": "error",
                    "label": "fails",
                },
            ],
        }
    ]
    ex = by_id(plan_diagrams(model, opts(types=["execution"]), cfg()))["execution-run"]
    nodes = {n["id"]: n for n in ex["nodes"]}
    assert nodes["fn:app/main.py:main"]["shape"] == "diamond"
    assert nodes["fn:app/svc.py:step_b"]["category"] == "error"
    e = {(x["from"], x["to"]): x for x in ex["edges"]}
    assert e[("fn:app/svc.py:run", "fn:app/svc.py:step_a")]["style"] == "dotted"
    assert e[("fn:app/svc.py:run", "fn:app/svc.py:step_b")]["end"] == "cross"
    assert e[("fn:app/main.py:main", "fn:app/svc.py:run")]["label"] == "if ok"


def test_execution_skipped_without_entrypoints():
    model = mk(["fn:a.py:f", "fn:a.py:g"], [("fn:a.py:f", "fn:a.py:g", "calls")])
    plan = plan_diagrams(model, opts(types=["execution"]), cfg())
    assert not plan["diagrams"] and plan["skipped"][0]["type"] == "execution"


# ------------------------------------------------------------------ sequence


def seq_model():
    return mk(
        [
            ("fn:web/api.js:fetchTodos", {"subsystem": "sub:web"}),
            ("api:GET /api/todos", {"name": "GET /api/todos"}),
            ("fn:srv/routes.py:list_todos", {"subsystem": "sub:srv"}),
            ("fn:srv/repo.py:query", {"subsystem": "sub:srv"}),
            ("store:pg", {"name": "PostgreSQL"}),
        ],
        [
            ("fn:web/api.js:fetchTodos", "api:GET /api/todos", "api_request"),
            ("api:GET /api/todos", "fn:srv/routes.py:list_todos", "handles"),
            ("fn:srv/routes.py:list_todos", "fn:srv/repo.py:query", "calls"),
            (
                "fn:srv/repo.py:query",
                "store:pg",
                "db_read",
                {"payload": ["todos"], "label": "SELECT todos"},
            ),
        ],
        subsystems=["sub:web", "sub:srv"],
    )


def test_derived_sequence_web_request_chain():
    plan = plan_diagrams(seq_model(), opts(types=["sequence"]), cfg())
    seq = by_id(plan)["sequence-get-api-todos"]
    assert seq["renderer"] == "sequence"
    labels = [p["label"] for p in seq["participants"]]
    # Module participants carry their subsystem when there are several.
    assert labels == ["Web api", "Srv routes", "Srv repo", "PostgreSQL"]
    msgs = [
        (m["from"].split("/")[-1], m["to"].split("/")[-1], m["label"], m["kind"])
        for m in seq["messages"]
    ]
    assert msgs[0] == ("api.js", "routes.py", "GET /api/todos", "sync")
    assert ("repo.py", "store:pg", "SELECT todos", "sync") in msgs
    assert msgs[-1] == ("routes.py", "api.js", "response", "reply")
    assert any(m[3] == "reply" and m[0] == "store:pg" for m in msgs)


def test_sequence_skipped_with_fewer_than_three_participants():
    model = mk(
        [("fn:a/x.py:c", {}), ("api:GET /x", {"name": "GET /x"}), ("fn:a/x.py:h", {})],
        [("fn:a/x.py:c", "api:GET /x", "api_request"), ("api:GET /x", "fn:a/x.py:h", "handles")],
    )
    plan = plan_diagrams(model, opts(types=["sequence"]), cfg())
    assert not plan["diagrams"]
    assert "fewer than 3 participants" in plan["skipped"][0]["reason"]


def test_provided_sequence_flow():
    model = seq_model()
    model["flows"] = [
        {
            "id": "flow-login",
            "name": "Login",
            "kind": "sequence",
            "steps": [
                {
                    "from": "fn:web/api.js:fetchTodos",
                    "to": "fn:srv/routes.py:list_todos",
                    "label": "POST /login",
                },
                {"from": "fn:srv/routes.py:list_todos", "to": "store:pg", "label": "lookup"},
                {
                    "from": "fn:srv/routes.py:list_todos",
                    "to": "fn:web/api.js:fetchTodos",
                    "kind": "return",
                    "label": "token",
                },
            ],
        }
    ]
    seq = by_id(plan_diagrams(model, opts(types=["sequence"]), cfg()))["sequence-login"]
    assert [m["kind"] for m in seq["messages"]] == ["sync", "sync", "reply"]
    assert len(seq["participants"]) == 3


# ------------------------------------------------------------------ algorithm / state / erd


def test_algorithm_from_claude_findings():
    model = mk(["algo:icp", "fn:p/icp.py:match"], [("algo:icp", "fn:p/icp.py:match", "calls")])
    model["algorithms"] = [
        {
            "id": "algo-icp",
            "name": "ICP",
            "node": "algo:icp",
            "purpose": "Align two point clouds.",
            "stages": [
                {"id": "s1", "name": "Load clouds", "kind": "io"},
                {"id": "s2", "name": "Match", "kind": "step", "functions": ["fn:p/icp.py:match"]},
                {"id": "s3", "name": "Converged?", "kind": "decision"},
                {"id": "s4", "name": "Diverged", "kind": "error"},
                {"id": "s5", "name": "Done", "kind": "terminal"},
            ],
            "transitions": [
                {"from": "s1", "to": "s2"},
                {"from": "s2", "to": "s3"},
                {"from": "s3", "to": "s2", "label": "no"},
                {"from": "s3", "to": "s5", "label": "yes"},
                {"from": "s3", "to": "s4", "label": "max iterations"},
            ],
        }
    ]
    algo = by_id(plan_diagrams(model, opts(types=["algorithm"]), cfg()))["algorithm-icp"]
    assert algo["direction"] == "TD" and algo["row"] == 3
    shapes = {n["label"]: n["shape"] for n in algo["nodes"]}
    assert shapes == {"Load clouds": "lean_r", "Match": "rect", "Converged?": "diamond",
                      "Diverged": "rect", "Done": "stadium"}  # fmt: skip
    cats = {n["label"]: n["category"] for n in algo["nodes"]}
    assert cats["Diverged"] == "error"
    assert {"algo:icp", "fn:p/icp.py:match"} <= set(algo["model_nodes"])
    ends = {e["label"]: e["end"] for e in algo["edges"]}
    assert ends["max iterations"] == "cross" and ends["yes"] == "arrow"


def test_state_machine():
    model = mk(["fn:a.py:f"], [])
    model["state_machines"] = [
        {
            "id": "job",
            "name": "Job",
            "states": [{"id": "Idle"}, {"id": "Running"}, {"id": "Done"}],
            "initial": "Idle",
            "transitions": [{"from": "Idle", "to": "Running", "label": "start"},
                            {"from": "Running", "to": "Done", "label": "finish"}],
        }
    ]  # fmt: skip
    st = by_id(plan_diagrams(model, opts(types=["state"]), cfg()))["state-job"]
    assert st["renderer"] == "state" and st["initial"] == "Idle" and st["finals"] == ["Done"]
    assert len(st["transitions"]) == 2


def test_erd_requires_two_entities_and_splits_large_schemas():
    model = mk(["fn:a.py:f"], [])
    model["erd"] = {"entities": [{"id": "users", "attributes": []}], "relations": []}
    plan = plan_diagrams(model, opts(types=["erd"]), cfg())
    assert not plan["diagrams"] and "fewer than 2" in plan["skipped"][0]["reason"]
    ents = [
        {"id": f"t{i:02d}", "attributes": [{"name": "id", "type": "int", "key": "PK"}]}
        for i in range(25)
    ]
    rels = [
        {"from": f"t{i:02d}", "to": f"t{i + 1:02d}", "cardinality": "one-to-many"}
        for i in range(0, 24, 2)
    ]
    model["erd"] = {"entities": ents, "relations": rels}
    plan = plan_diagrams(model, opts(types=["erd"]), cfg())
    ids = [d["id"] for d in plan["diagrams"]]
    assert ids == ["erd-part-1", "erd-part-2"]
    assert all(len(d["entities"]) <= 20 for d in plan["diagrams"])
    assert sum(len(d["entities"]) for d in plan["diagrams"]) == 25
    assert sum(len(d["relations"]) for d in plan["diagrams"]) == 12


# ------------------------------------------------------------------ structure


def test_dependency_marks_import_cycles():
    model = mk(
        ["mod:p/a.py", "mod:p/b.py", "mod:p/c.py", "mod:p/d.py"],
        [
            ("mod:p/a.py", "mod:p/b.py", "imports"),
            ("mod:p/b.py", "mod:p/c.py", "imports"),
            ("mod:p/c.py", "mod:p/a.py", "imports"),
            ("mod:p/c.py", "mod:p/d.py", "imports"),
        ],
    )
    dep = by_id(plan_diagrams(model, opts(types=["dependency"]), cfg()))["dependency"]
    cats = {n["id"]: n["category"] for n in dep["nodes"]}
    assert cats["mod:p/a.py"] == "error" and cats["mod:p/d.py"] == "app"
    assert any(n.startswith("Import cycle:") for n in dep["notes"])
    assert all(e["style"] == "solid" for e in dep["edges"])


def test_infrastructure_needs_two_deployment_units():
    model = mk(
        [("deploy:api", {"name": "api"}), ("deploy:worker", {"name": "worker"}),
         ("deploy:db", {"name": "db", "kind": "datastore"})],
        [("deploy:api", "deploy:db", "depends_on"), ("deploy:worker", "deploy:db", "depends_on")],
    )  # fmt: skip
    infra = by_id(plan_diagrams(model, opts(types=["infrastructure"]), cfg()))["infrastructure"]
    shapes = {n["id"]: n["shape"] for n in infra["nodes"]}
    assert shapes == {"deploy:api": "rounded", "deploy:worker": "rounded", "deploy:db": "cylinder"}
    single = mk([("deploy:api", {"name": "api"})], [])
    plan = plan_diagrams(single, opts(types=["infrastructure"]), cfg())
    assert not plan["diagrams"]


# ------------------------------------------------------------------ ROS 2


def ros_model():
    return mk(
        [
            ("ros:scan_filter", {"subsystem": "sub:perception"}),
            ("ros:planner", {"subsystem": "sub:nav"}),
            ("topic:/scan", {"name": "/scan"}),
            ("topic:/scan_filtered", {"name": "/scan_filtered"}),
            ("srv:/plan", {"name": "/plan"}),
            ("ros:ui", {"subsystem": "sub:nav"}),
            ("tf:map", {"name": "map"}),
            ("tf:odom", {"name": "odom"}),
            ("tf:base_link", {"name": "base_link"}),
        ],
        [
            ("topic:/scan", "ros:scan_filter", "subscribes"),
            ("ros:scan_filter", "topic:/scan_filtered", "publishes"),
            ("topic:/scan_filtered", "ros:planner", "subscribes"),
            ("ros:planner", "srv:/plan", "service_provide"),
            ("ros:ui", "srv:/plan", "service_call"),
            ("tf:map", "tf:odom", "tf_transform", {"label": "by planner"}),
            ("tf:odom", "tf:base_link", "tf_transform"),
        ],
        subsystems=["sub:perception", "sub:nav"],
    )


def test_ros2_graph_and_tf_priority_after_master():
    full = plan_diagrams(ros_model(), opts(), cfg())
    assert not any(n["id"].startswith("tf:") for n in by_id(full)["master"]["nodes"])
    # This small ROS master already draws every node and topic: the graph is redundant.
    skipped = {x.get("id"): x["reason"] for x in full["skipped"]}
    assert "already drawn in 'master'" in skipped["ros2-graph"]
    plan = plan_diagrams(ros_model(), opts(types=["ros2"]), cfg())
    ids = [d["id"] for d in plan["diagrams"]]
    assert ids[0] == "ros2-graph"
    graph = by_id(plan)["ros2-graph"]
    assert graph["row"] == 4
    shapes = {n["id"]: n["shape"] for n in graph["nodes"]}
    assert shapes["topic:/scan"] == "lean_r" and shapes["srv:/plan"] == "hexagon"
    assert shapes["ros:planner"] == "rounded"
    assert ("srv:/plan", "ros:planner") in edge_pairs(graph)  # provide drawn as request flow
    styles = {(e["from"], e["to"]): e["style"] for e in graph["edges"]}
    assert styles[("ros:scan_filter", "topic:/scan_filtered")] == "dotted"
    assert {g["label"] for g in graph["groups"]} == {"Nav"}
    tf = by_id(plan)["ros2-tf"]
    assert tf["type"] == "ros2" and tf["direction"] == "TD"
    assert all(n["shape"] == "circle" for n in tf["nodes"])
    assert ids.index("ros2-tf") > ids.index("ros2-graph")


def test_ros2_graph_split_by_package():
    nodes, edges = [], []
    for pkg in ("a", "b", "c"):
        for i in range(4):
            nodes.append((f"ros:{pkg}{i}", {"subsystem": f"sub:{pkg}"}))
            nodes.append((f"topic:/{pkg}{i}", {"name": f"/{pkg}{i}"}))
            edges.append((f"ros:{pkg}{i}", f"topic:/{pkg}{i}", "publishes"))
    model = mk(nodes, edges, subsystems=["sub:a", "sub:b", "sub:c"])
    plan = plan_diagrams(model, opts(types=["ros2"]), cfg(max_nodes=10))
    parts = [d for d in plan["diagrams"] if d["id"].startswith("ros2-graph")]
    assert len(parts) >= 2 and all(len(d["nodes"]) <= 10 for d in parts)
    assert parts[0]["id"] == "ros2-graph-part-1"


def test_non_ros_repo_skips_ros2():
    plan = plan_diagrams(exec_model(), opts(types=["ros2"]), cfg())
    assert not plan["diagrams"] and "not a ROS 2" in plan["skipped"][0]["reason"]


# ------------------------------------------------------------------ filters / caps / labels


def test_type_filter_excludes_master_unless_requested():
    plan = plan_diagrams(web_model(), opts(types=["dependency", "dataflow"]), cfg())
    assert {d["type"] for d in plan["diagrams"]} <= {"dependency", "dataflow"}
    plan = plan_diagrams(web_model(), opts(types=["master"]), cfg())
    assert [d["id"] for d in plan["diagrams"]] == ["master"]


def test_focus_on_subsystem_limits_detail_diagrams():
    plan = plan_diagrams(web_model(), opts(focus="worker"), cfg(master_target_min=3))
    ids = by_id(plan)
    assert "master" in ids
    subs = [d["id"] for d in plan["diagrams"] if d["type"] == "subsystem"]
    assert subs == ["subsystem-worker"]
    assert plan["options"]["focus"] == "worker"


def test_focus_on_node_name_matches_case_insensitively():
    p = _Planner(web_model(), opts(focus="CHARGE"), cfg())
    assert p._resolve_focus()
    assert "fn:billing/charge.py:charge" in p.scope
    assert "fn:backend/bill.py:charge_client" in p.scope
    assert p.focus_subsystems == ["sub:backend", "sub:billing"]


def test_focus_without_match_plans_master_only():
    plan = plan_diagrams(web_model(), opts(focus="nonexistent"), cfg())
    assert [d["id"] for d in plan["diagrams"]] == ["master"]
    assert any(s["type"] == "focus" for s in plan["skipped"])


def test_caps_record_capped_diagrams():
    plan = plan_diagrams(web_model(), opts("overview"), cfg())
    assert len(plan["diagrams"]) <= 2
    assert plan["diagrams"][0]["id"] == "master"
    assert any("cap of 2" in s["reason"] for s in plan["skipped"])
    assert [d["priority"] for d in plan["diagrams"]] == list(range(len(plan["diagrams"])))


def test_overview_has_no_subsystem_diagrams():
    plan = plan_diagrams(web_model(), opts("overview"), cfg())
    assert not any(d["type"] == "subsystem" for d in plan["diagrams"])


def test_duplicate_module_basenames_are_disambiguated_and_long_labels_noted():
    long_name = "a_really_long_function_name_that_keeps_going_and_going_forever"
    model = mk(
        [
            "mod:pkg_a/utils.py",
            "mod:pkg_b/utils.py",
            "mod:pkg_a/main.py",
            (f"fn:pkg_a/main.py:{long_name}", {}),
        ],
        [
            ("mod:pkg_a/main.py", "mod:pkg_a/utils.py", "imports"),
            ("mod:pkg_a/main.py", "mod:pkg_b/utils.py", "imports"),
        ],
    )
    v = ModelView(model)
    assert v.label("mod:pkg_a/utils.py") == "pkg_a/utils.py"
    assert v.label("mod:pkg_a/main.py") == "main.py"
    p = _Planner(model, opts(), cfg())
    spec = p._flowchart(
        did="x", typ="master", title="t", purpose="p", level="symbol",
        units=[f"fn:pkg_a/main.py:{long_name}", "mod:pkg_a/main.py"], edges=[], notes=[],
    )  # fmt: skip
    assert any("longer than 48" in n for n in spec["notes"])
    assert any(n["label"] == long_name + "()" for n in spec["nodes"])


def test_web_model_is_deterministic_across_input_order():
    m1 = web_model()
    m2 = copy.deepcopy(m1)
    m2["nodes"].reverse()
    m2["edges"].reverse()
    a = plan_diagrams(m1, opts("deep"), cfg(master_target_min=3))
    b = plan_diagrams(m2, opts("deep"), cfg(master_target_min=3))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
