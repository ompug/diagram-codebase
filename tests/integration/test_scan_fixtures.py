"""Scan + merge every fixture repository and check the resulting model."""

from __future__ import annotations

import json

import pytest
from conftest import FIXTURES, scan_and_merge
from diagram_codebase.model.schema import validate_model

ALL = [
    "a_python_app",
    "b_web_app",
    "c_events",
    "d_pipeline",
    "e_ros2",
    "f_edge/empty",
    "f_edge/unsupported",
    "f_edge/cyclic",
    "f_edge/no_config",
    "f_edge/weird",
]


def nodes(model):
    return {n["id"]: n for n in model["nodes"]}


def edges(model):
    return {e["id"]: e for e in model["edges"]}


def has_edge(model, kind, src, dst):
    return f"e:{kind}:{src}>{dst}" in edges(model)


@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize("copy", [False, True], ids=["git", "walk"])
def test_every_fixture_produces_a_valid_model(scan_fixture, name, copy):
    findings = ("d_pipeline_findings/algorithms.json",) if name == "d_pipeline" else ()
    scan, model, report = scan_fixture(name, findings=findings, copy=copy)
    assert report["errors"] == []
    assert validate_model(model)["errors"] == []
    assert report["rejected"] == []
    json.dumps(model)  # serializable
    expected = "git ls-files" if not copy else "filesystem walk with .gitignore subset"
    assert scan["inventory"]["method"] == expected


@pytest.mark.parametrize("name", ["b_web_app", "e_ros2"])
def test_git_and_walk_agree(scan_fixture, name):
    _, git_model, _ = scan_fixture(name)
    _, walk_model, _ = scan_fixture(name, copy=True)
    assert set(nodes(git_model)) == set(nodes(walk_model))
    assert set(edges(git_model)) == set(edges(walk_model))


def test_scan_is_deterministic(tmp_path):
    _, m1, _ = scan_and_merge(FIXTURES / "c_events", tmp_path / "o1")
    _, m2, _ = scan_and_merge(FIXTURES / "c_events", tmp_path / "o2")
    strip = lambda m: {k: v for k, v in m.items() if k != "meta"}  # noqa: E731
    assert json.dumps(strip(m1), sort_keys=True) == json.dumps(strip(m2), sort_keys=True)
    f1 = (tmp_path / "o1" / "findings" / "scan.json").read_text()
    f2 = (tmp_path / "o2" / "findings" / "scan.json").read_text()
    assert f1 == f2


def test_a_python_app(scan_fixture):
    _, model, _ = scan_fixture("a_python_app")
    n = nodes(model)
    assert "entrypoint" in n["fn:app/main.py:main"]["tags"]
    assert has_edge(
        model, "calls", "fn:app/main.py:main", "fn:app/service.py:InventoryService.restock"
    )
    assert has_edge(model, "db_write", "fn:app/repository.py:SqliteRepository.add", "store:sqlite")
    assert has_edge(
        model, "external_call", "fn:app/notifier.py:WebhookNotifier.send", "ext:hooks.example.com"
    )
    assert has_edge(
        model, "config_dependency", "fn:app/main.py:build_service", "cfg:env:INVENTORY_DB"
    )
    assert [e["id"] for e in model["erd"]["entities"]] == ["items"]


def test_b_web_app(scan_fixture):
    scan, model, report = scan_fixture("b_web_app")
    n = nodes(model)
    assert {"fastapi", "react", "redux", "sqlalchemy", "postgres"} <= set(
        model["meta"]["frameworks"]
    )
    # Endpoint handled by the backend and requested by the frontend (cross-language match).
    api = "api:GET /api/todos"
    assert has_edge(model, "handles", api, "fn:backend/app/main.py:list_todos")
    assert has_edge(model, "api_request", "fn:frontend/src/api.js:fetchTodos", api)
    assert n["fn:frontend/src/api.js:fetchTodos"]["subsystem"] == "sub:frontend"
    assert n["fn:backend/app/main.py:list_todos"]["subsystem"] == "sub:backend"
    assert has_edge(
        model, "api_request", "fn:frontend/src/api.js:createTodo", "api:POST /api/todos"
    )
    assert has_edge(model, "handles", "api:POST /api/todos", "fn:backend/app/main.py:add_todo")
    assert has_edge(model, "handles", "api:GET /api/todos/{}", "fn:backend/app/main.py:read_todo")
    assert has_edge(
        model, "api_request", "fn:frontend/src/api.js:fetchTodo", "api:GET /api/todos/{}"
    )
    assert n[api]["subsystem"] == "sub:backend"
    # UI calls the API client; data access hits PostgreSQL with table names.
    assert has_edge(
        model, "calls", "fn:frontend/src/App.jsx:App", "fn:frontend/src/api.js:fetchTodos"
    )
    assert has_edge(
        model, "calls", "fn:backend/app/main.py:list_todos", "fn:backend/app/crud.py:get_todos"
    )
    read = edges(model)["e:db_read:fn:backend/app/crud.py:get_todos>store:postgresql"]
    assert read["payload"] == ["todos"]
    write = edges(model)["e:db_write:fn:backend/app/crud.py:create_todo>store:postgresql"]
    assert write["payload"] == ["todos"]
    assert model["erd"]["relations"] == [
        {
            "from": "users",
            "to": "todos",
            "cardinality": "one-to-many",
            "label": "owner_id",
            "identifying": False,
        }
    ]
    # Deployment
    assert n["deploy:db"]["kind"] == "datastore"
    assert has_edge(model, "depends_on", "deploy:backend", "deploy:db")
    assert has_edge(model, "depends_on", "deploy:frontend", "deploy:backend")
    assert "state:todos" in n
    # Scanners never copy credential-bearing literals into the model.
    assert "FAKE_PASSWORD" not in json.dumps(model)
    assert "FAKE_PASSWORD" not in json.dumps(scan["fragment"])


def test_c_events(scan_fixture):
    _, model, _ = scan_fixture("c_events")
    e = edges(model)
    assert has_edge(
        model, "publishes", "fn:shop/producers.py:Checkout.place_order", "chan:order.created"
    )
    assert has_edge(model, "subscribes", "chan:order.created", "fn:shop/consumers.py:reserve_stock")
    assert has_edge(
        model, "publishes", "fn:shop/producers.py:on_gateway_callback", "chan:payment.settled"
    )
    assert has_edge(
        model, "subscribes", "chan:payment.settled", "fn:shop/consumers.py:email_receipt"
    )
    assert has_edge(
        model,
        "publishes",
        "fn:shop/worker.py:FulfilmentWorker.enqueue",
        "chan:FulfilmentWorker.queue",
    )
    assert has_edge(
        model, "subscribes", "chan:FulfilmentWorker.queue", "fn:shop/worker.py:FulfilmentWorker.run"
    )
    spawn = e["e:spawns:fn:shop/app.py:run>fn:shop/worker.py:FulfilmentWorker.run"]
    assert spawn["label"] == "asyncio task" and spawn["evidence_status"] == "static_inferred"
    assert e["e:spawns:fn:shop/app.py:main>fn:shop/metrics.py:metrics_loop"]["label"] == "thread"
    assert e["e:triggers:chan:SIGINT>fn:shop/app.py:shutdown"]["phase"] == "shutdown"
    assert has_edge(model, "triggers", "chan:SIGTERM", "fn:shop/worker.py:FulfilmentWorker.stop")
    assert has_edge(model, "publishes", "fn:shop/consumers.py:email_receipt", "chan:task queue")
    assert has_edge(model, "triggers", "chan:task queue", "fn:shop/tasks.py:send_receipt")
    assert has_edge(
        model, "calls", "fn:shop/consumers.py:register", "fn:shop/bus.py:EventBus.subscribe"
    )
    assert "celery" in model["meta"]["frameworks"]


def test_d_pipeline_with_findings(scan_fixture):
    _, model, report = scan_fixture("d_pipeline", findings=("d_pipeline_findings/algorithms.json",))
    assert report["rejected"] == [] and not [w for w in report["warnings"] if "dropped" in w]
    n = nodes(model)
    assert has_edge(model, "calls", "fn:run.py:main", "fn:registration/icp.py:register")
    for f in ("find_correspondences", "estimate_transform", "apply_transform"):
        assert has_edge(
            model, "calls", "fn:registration/icp.py:register", f"fn:registration/icp.py:{f}"
        )
    assert has_edge(model, "config_dependency", "fn:run.py:main", "cfg:file:config.yaml")
    algo = model["algorithms"][0]
    assert [s["id"] for s in algo["stages"]] == [
        "init",
        "iterate",
        "match",
        "fit",
        "apply",
        "check",
        "done",
    ]
    assert all(s["evidence_ids"] for s in algo["stages"])
    assert algo["node"] == "algo:icp" and n["algo:icp"]["evidence_status"] == "confirmed"
    assert len(algo["transitions"]) == 8
    df = model["dataflows"][0]
    assert [s["node"] for s in df["stages"]] == [
        "data:raw-cloud",
        "data:downsampled-cloud",
        "data:correspondences",
        "data:transform",
        "data:result",
    ]
    assert len(df["links"]) == 4
    reg = n["fn:registration/icp.py:register"]
    assert (
        reg["description"].startswith("Entry point of the ICP loop") and "hot-path" in reg["tags"]
    )
    assert (
        next(s for s in model["subsystems"] if s["id"] == "sub:registration")["name"]
        == "Registration core"
    )


def test_e_ros2(scan_fixture):
    _, model, _ = scan_fixture("e_ros2")
    n = nodes(model)
    assert "ros2" in model["meta"]["frameworks"]
    assert {i for i in n if i.startswith("ros:")} == {
        "ros:scan_filter",
        "ros:localizer",
        "ros:controller",
    }
    assert has_edge(model, "subscribes", "topic:/lidar/scan", "ros:scan_filter")  # after remap
    assert "topic:/scan" not in n
    assert has_edge(model, "subscribes", "topic:/pose", "ros:controller")
    assert has_edge(model, "publishes", "ros:localizer", "topic:/pose")
    assert has_edge(model, "publishes", "ros:controller", "topic:/base/cmd_vel")
    assert has_edge(model, "service_provide", "ros:localizer", "srv:/relocalize")
    assert has_edge(model, "service_call", "ros:controller", "srv:/relocalize")
    assert has_edge(model, "tf_transform", "tf:map", "tf:odom")
    subs = {s["id"]: s["name"] for s in model["subsystems"]}
    assert subs == {
        "sub:src-lidar-localization": "Lidar Localization",
        "sub:src-motion-controller": "Motion Controller",
    }
    assert n["ros:controller"]["subsystem"] == "sub:src-motion-controller"
    assert "entrypoint" in n["fn:src/motion_controller/src/controller.cpp:main"]["tags"]
    assert (
        "scan_filter"
        in n["fn:src/lidar_localization/lidar_localization/scan_filter.py:main"]["metadata"][
            "entry_point_names"
        ]
    )


def test_f_edge_cases(scan_fixture):
    scan, model, _ = scan_fixture("f_edge/empty")
    assert model["nodes"] == [] and model["subsystems"] == []
    assert scan["brief"]["source_files"] == 0

    scan, model, _ = scan_fixture("f_edge/unsupported")
    assert scan["inventory"]["unsupported_languages"] == {"go": 1, "rust": 1}
    assert model["nodes"] == []
    assert model["meta"]["coverage"]["unsupported_language_files"] == {"go": 1, "rust": 1}


def test_f_edge_cyclic(scan_fixture):
    _, model, _ = scan_fixture("f_edge/cyclic")
    assert has_edge(model, "calls", "fn:a.py:ping", "fn:b.py:pong")
    assert has_edge(model, "calls", "fn:b.py:pong", "fn:a.py:ping")
    assert has_edge(model, "imports", "mod:a.py", "mod:b.py") and has_edge(
        model, "imports", "mod:b.py", "mod:a.py"
    )


def test_f_edge_no_config(scan_fixture):
    scan, model, _ = scan_fixture("f_edge/no_config")
    m = scan["inventory"]["manifests"]
    assert m["manifest_files"] == [] and m["frameworks"] == [] and m["entry_points"] == []
    assert has_edge(model, "calls", "fn:report.py:report", "fn:cleanup.py:old_files")
    assert "entrypoint" in nodes(model)["mod:cleanup.py"]["tags"]


def test_f_edge_weird(scan_fixture):
    scan, model, _ = scan_fixture("f_edge/weird")
    assert [p["file"] for p in scan["inventory"]["parse_errors"]] == ["broken.py"]
    n = nodes(model)
    assert n["mod:broken.py"]["metadata"]["parse_error"].startswith("SyntaxError")
    assert model["meta"]["coverage"]["parse_errors"] == 1
    assert has_edge(model, "handles", "api:GET /v1/naïve-café/{}", "fn:api.py:café_item")
    assert has_edge(model, "handles", "api:DELETE /v1/naïve-café/{}", "fn:api.py:café_item")
    assert "api:GET /v1/weird path/{}/{}/{}" in n
    assert has_edge(model, "calls", "fn:ünïcode_mod.py:use", "fn:ünïcode_mod.py:größe")
    text = json.dumps(model, ensure_ascii=False)
    assert "sk_test_FAKE" not in text


def test_large_generated_tree(large_tree, tmp_path):
    scan, model, report = scan_and_merge(large_tree, tmp_path / "out")
    inv = scan["inventory"]
    assert inv["analyzed_file_count"] == 2000
    assert not any("node_modules" in f["path"] or "build/" in f["path"] for f in inv["files"])
    assert scan["brief"]["use_parallel_agents"] is True
    assert report["errors"] == []
    assert sum(1 for n in model["nodes"] if n["kind"] == "module") == 2000
