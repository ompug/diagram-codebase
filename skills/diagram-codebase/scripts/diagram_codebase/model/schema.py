"""Architecture model schema: vocabularies, constructors, and the validator.

The model is plain JSON (dicts/lists) so it stays readable, diffable, and usable
by other backends. Presentation details never live here; evidence lives in its
own table and nodes/edges reference it by id.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from .. import SCHEMA_VERSION

NODE_KINDS = (
    "module",  # a source file / module
    "package",  # a directory-level package or library target
    "class",
    "function",  # functions and methods (methods have a class parent)
    "service",  # independently runnable/deployable unit (process, server, daemon)
    "worker",  # background worker / consumer process
    "ros_node",
    "api_endpoint",
    "ui_component",
    "datastore",  # database, cache, object store, file store
    "db_entity",  # table / ORM model
    "event_channel",  # queue, topic, stream, event bus, signal
    "topic",  # ROS 2 topic
    "ros_service",
    "ros_action",
    "tf_frame",
    "external_service",  # third-party API / SaaS
    "library",  # external dependency worth showing
    "config",  # configuration source (env var, file, parameter)
    "algorithm",
    "data_artifact",  # intermediate representation / data product
    "deployment_unit",  # container image, compose service, k8s workload
    "state",
)

EDGE_KINDS = (
    "calls",
    "imports",
    "inherits",
    "instantiates",
    "data_flow",
    "publishes",
    "subscribes",
    "api_request",
    "handles",
    "db_read",
    "db_write",
    "db_access",
    "ipc",
    "state_transition",
    "config_dependency",
    "tf_transform",
    "service_call",
    "service_provide",
    "action_call",
    "action_provide",
    "external_call",
    "spawns",
    "triggers",
    "launches",
    "depends_on",
    "error_path",
)

# Ordered from strongest to weakest. These are kept separate on every piece of
# evidence; a node's or edge's `evidence_status` is the strongest it has.
EVIDENCE_STATUSES = (
    "confirmed",  # direct syntactic/declarative evidence in source or config
    "dynamic_observed",  # seen at runtime (logs, traces) - never produced by default
    "static_inferred",  # reasoned from code but not directly declared
    "documented_only",  # stated in docs/README but not verified in code
    "unknown",
)
EVIDENCE_SOURCES = ("ast", "regex", "manifest", "claude", "doc", "runtime")
PHASES = ("init", "runtime", "shutdown", "error", "build", "unknown")

CONFIDENCE_BY_STATUS = {
    "confirmed": "high",
    "dynamic_observed": "high",
    "static_inferred": "medium",
    "documented_only": "low",
    "unknown": "low",
}

FLOW_STEP_KINDS = ("call", "return", "async", "event", "decision", "loop", "error", "io")
DATA_ROLES = ("input", "transform", "intermediate", "storage", "output")
STAGE_KINDS = ("step", "decision", "loop", "io", "terminal", "error")


# ---------------------------------------------------------------- constructors


def evidence(
    file: str,
    line_start: int | None,
    line_end: int | None = None,
    *,
    symbol: str = "",
    detail: str = "",
    source: str = "ast",
    status: str = "confirmed",
) -> dict[str, Any]:
    line_end = line_end if line_end is not None else line_start
    key = f"{file}|{line_start}|{line_end}|{symbol}|{source}|{detail}"
    return {
        "id": "ev:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12],
        "file": file,
        "line_start": line_start,
        "line_end": line_end,
        "symbol": symbol,
        "detail": detail,
        "source": source,
        "status": status,
    }


def node(
    id: str,
    name: str,
    kind: str,
    *,
    subsystem: str | None = None,
    description: str = "",
    parent: str | None = None,
    evidence_ids: list[str] | None = None,
    tags: list[str] | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "id": id,
        "name": name,
        "kind": kind,
        "subsystem": subsystem,
        "description": description,
        "parent": parent,
        "symbols": metadata.pop("symbols", []),
        "inputs": metadata.pop("inputs", []),
        "outputs": metadata.pop("outputs", []),
        "tags": sorted(set(tags or [])),
        "evidence_ids": list(evidence_ids or []),
        "metadata": metadata,
    }


def edge_id(kind: str, src: str, dst: str) -> str:
    return f"e:{kind}:{src}>{dst}"


def edge(
    src: str,
    dst: str,
    kind: str,
    *,
    label: str = "",
    payload: list[str] | None = None,
    phase: str = "unknown",
    evidence_ids: list[str] | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "id": edge_id(kind, src, dst),
        "from": src,
        "to": dst,
        "kind": kind,
        "label": label,
        "payload": sorted(set(payload or [])),
        "phase": phase,
        "evidence_ids": list(evidence_ids or []),
        "metadata": metadata,
    }


def empty_model() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {},
        "subsystems": [],
        "nodes": [],
        "edges": [],
        "evidence": [],
        "flows": [],
        "dataflows": [],
        "algorithms": [],
        "state_machines": [],
        "erd": {"entities": [], "relations": []},
    }


# ------------------------------------------------------------- derived fields


def strongest_status(statuses: list[str]) -> str:
    for status in EVIDENCE_STATUSES:
        if status in statuses:
            return status
    return "unknown"


def apply_derived(model: dict[str, Any]) -> None:
    """Compute evidence_status / confidence from the evidence table (never hand-set)."""
    ev = {e["id"]: e for e in model.get("evidence", [])}
    for item in [*model.get("nodes", []), *model.get("edges", [])]:
        statuses = [ev[i]["status"] for i in item.get("evidence_ids", []) if i in ev]
        status = strongest_status(statuses)
        item["evidence_status"] = status
        item["confidence"] = CONFIDENCE_BY_STATUS[status]
        item["evidence_counts"] = dict(Counter(statuses))


# ------------------------------------------------------------------ validation


def _check_type(errors: list, where: str, value: Any, typ: type | tuple, optional=False) -> bool:
    if value is None and optional:
        return True
    if not isinstance(value, typ):
        errors.append(
            f"{where}: expected {getattr(typ, '__name__', typ)}, got {type(value).__name__}"
        )
        return False
    return True


def validate_model(model: dict[str, Any], *, strict_evidence: bool = True) -> dict[str, Any]:
    """Return {'errors': [...], 'warnings': [...], 'stats': {...}}.

    Errors make a model unusable for planning; warnings are reported to the user.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(model, dict):
        return {"errors": ["model is not a JSON object"], "warnings": [], "stats": {}}
    for key in ("nodes", "edges", "evidence", "subsystems"):
        _check_type(errors, key, model.get(key), list)
    if errors:
        return {"errors": errors, "warnings": warnings, "stats": {}}

    ev_ids: Counter = Counter()
    for i, e in enumerate(model["evidence"]):
        where = f"evidence[{i}]"
        if not _check_type(errors, where, e, dict):
            continue
        ev_ids[e.get("id")] += 1
        if e.get("status") not in EVIDENCE_STATUSES:
            errors.append(f"{where} ({e.get('id')}): invalid status {e.get('status')!r}")
        if e.get("source") not in EVIDENCE_SOURCES:
            errors.append(f"{where} ({e.get('id')}): invalid source {e.get('source')!r}")
        if not e.get("file"):
            errors.append(f"{where} ({e.get('id')}): missing file")
    for eid, n in ev_ids.items():
        if n > 1:
            errors.append(f"duplicate evidence id {eid}")

    sub_ids = Counter(s.get("id") for s in model["subsystems"] if isinstance(s, dict))
    for sid, n in sub_ids.items():
        if n > 1:
            errors.append(f"duplicate subsystem id {sid}")

    node_ids: Counter = Counter()
    for i, n in enumerate(model["nodes"]):
        where = f"nodes[{i}]"
        if not _check_type(errors, where, n, dict):
            continue
        node_ids[n.get("id")] += 1
        if not n.get("id") or not n.get("name"):
            errors.append(f"{where}: id and name are required")
        if n.get("kind") not in NODE_KINDS:
            errors.append(f"{where} ({n.get('id')}): invalid kind {n.get('kind')!r}")
        if n.get("subsystem") is not None and n.get("subsystem") not in sub_ids:
            errors.append(f"{where} ({n.get('id')}): unknown subsystem {n.get('subsystem')!r}")
        for eid in n.get("evidence_ids", []):
            if eid not in ev_ids:
                errors.append(f"{where} ({n.get('id')}): unknown evidence id {eid}")
        if strict_evidence and not n.get("evidence_ids"):
            errors.append(f"{where} ({n.get('id')}): no evidence")
    for nid, count in node_ids.items():
        if count > 1:
            errors.append(f"duplicate node id {nid}")
    for n in model["nodes"]:
        if isinstance(n, dict) and n.get("parent") and n["parent"] not in node_ids:
            errors.append(f"node {n.get('id')}: unknown parent {n['parent']}")

    edge_ids: Counter = Counter()
    degree: Counter = Counter()
    for i, e in enumerate(model["edges"]):
        where = f"edges[{i}]"
        if not _check_type(errors, where, e, dict):
            continue
        edge_ids[e.get("id")] += 1
        if e.get("kind") not in EDGE_KINDS:
            errors.append(f"{where} ({e.get('id')}): invalid kind {e.get('kind')!r}")
        if e.get("phase", "unknown") not in PHASES:
            errors.append(f"{where} ({e.get('id')}): invalid phase {e.get('phase')!r}")
        for end in ("from", "to"):
            if e.get(end) not in node_ids:
                errors.append(
                    f"{where} ({e.get('id')}): {end} references missing node {e.get(end)!r}"
                )
        for eid in e.get("evidence_ids", []):
            if eid not in ev_ids:
                errors.append(f"{where} ({e.get('id')}): unknown evidence id {eid}")
        if strict_evidence and not e.get("evidence_ids"):
            errors.append(f"{where} ({e.get('id')}): no evidence")
        degree[e.get("from")] += 1
        degree[e.get("to")] += 1
    for eid, n in edge_ids.items():
        if n > 1:
            errors.append(f"duplicate edge id {eid}")

    # Orphans: nodes with no edges, no children, and not referenced by flows.
    referenced = set(degree)
    referenced.update(n.get("parent") for n in model["nodes"] if isinstance(n, dict))
    for coll in ("flows", "dataflows", "algorithms", "state_machines"):
        for item in model.get(coll, []) or []:
            referenced.add(item.get("node"))
            for step in item.get("steps", []) or []:
                referenced.update((step.get("from"), step.get("to")))
            for stage in item.get("stages", []) or []:
                referenced.add(stage.get("node"))
    orphan_kinds = {
        "service",
        "worker",
        "ros_node",
        "datastore",
        "external_service",
        "event_channel",
    }
    for n in model["nodes"]:
        if isinstance(n, dict) and n.get("id") not in referenced and n.get("kind") in orphan_kinds:
            warnings.append(f"orphaned component {n['id']} ({n['kind']}) has no relationships")

    _validate_structures(model, node_ids, errors)

    stats = {
        "nodes": len(model["nodes"]),
        "edges": len(model["edges"]),
        "evidence": len(model["evidence"]),
        "subsystems": len(model["subsystems"]),
        "flows": len(model.get("flows", []) or []),
        "dataflows": len(model.get("dataflows", []) or []),
        "algorithms": len(model.get("algorithms", []) or []),
        "state_machines": len(model.get("state_machines", []) or []),
        "erd_entities": len((model.get("erd") or {}).get("entities", [])),
        "node_kinds": dict(Counter(n.get("kind") for n in model["nodes"] if isinstance(n, dict))),
        "edge_kinds": dict(Counter(e.get("kind") for e in model["edges"] if isinstance(e, dict))),
    }
    return {"errors": errors, "warnings": warnings, "stats": stats}


def _validate_structures(model: dict, node_ids: Counter, errors: list[str]) -> None:
    for i, flow in enumerate(model.get("flows", []) or []):
        where = f"flows[{i}] ({flow.get('id')})"
        if not flow.get("id") or not flow.get("name"):
            errors.append(f"{where}: id and name are required")
        for j, step in enumerate(flow.get("steps", []) or []):
            for end in ("from", "to"):
                if step.get(end) not in node_ids:
                    errors.append(
                        f"{where} step {j}: {end} references missing node {step.get(end)!r}"
                    )
            if step.get("kind", "call") not in FLOW_STEP_KINDS:
                errors.append(f"{where} step {j}: invalid kind {step.get('kind')!r}")
    for i, df in enumerate(model.get("dataflows", []) or []):
        where = f"dataflows[{i}] ({df.get('id')})"
        stage_nodes = set()
        for j, st in enumerate(df.get("stages", []) or []):
            if st.get("node") not in node_ids:
                errors.append(f"{where} stage {j}: missing node {st.get('node')!r}")
            if st.get("role") not in DATA_ROLES:
                errors.append(f"{where} stage {j}: invalid role {st.get('role')!r}")
            stage_nodes.add(st.get("node"))
        for j, link in enumerate(df.get("links", []) or []):
            if link.get("from") not in stage_nodes or link.get("to") not in stage_nodes:
                errors.append(f"{where} link {j}: endpoints must be stages of this dataflow")
    for i, algo in enumerate(model.get("algorithms", []) or []):
        where = f"algorithms[{i}] ({algo.get('id')})"
        if algo.get("node") and algo["node"] not in node_ids:
            errors.append(f"{where}: missing node {algo['node']!r}")
        stage_ids = [s.get("id") for s in algo.get("stages", []) or []]
        if len(stage_ids) != len(set(stage_ids)):
            errors.append(f"{where}: duplicate stage ids")
        for s in algo.get("stages", []) or []:
            if s.get("kind", "step") not in STAGE_KINDS:
                errors.append(f"{where} stage {s.get('id')}: invalid kind {s.get('kind')!r}")
        for t in algo.get("transitions", []) or []:
            if t.get("from") not in stage_ids or t.get("to") not in stage_ids:
                errors.append(
                    f"{where}: transition {t.get('from')}->{t.get('to')} references unknown stage"
                )
    for i, sm in enumerate(model.get("state_machines", []) or []):
        where = f"state_machines[{i}] ({sm.get('id')})"
        states = {s.get("id") for s in sm.get("states", []) or []}
        for t in sm.get("transitions", []) or []:
            if t.get("from") not in states or t.get("to") not in states:
                errors.append(
                    f"{where}: transition {t.get('from')}->{t.get('to')} references unknown state"
                )
    erd = model.get("erd") or {}
    names = {e.get("id") for e in erd.get("entities", [])}
    for r in erd.get("relations", []):
        if r.get("from") not in names or r.get("to") not in names:
            errors.append(f"erd relation {r.get('from')}->{r.get('to')} references unknown entity")
