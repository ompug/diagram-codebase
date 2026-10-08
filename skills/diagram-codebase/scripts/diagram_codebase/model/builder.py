"""Accumulate nodes, edges, and evidence with de-duplication.

Used by the deterministic scanners and by the merge step. Adding a node or
edge that already exists unions its evidence instead of creating a duplicate.
"""

from __future__ import annotations

from typing import Any

from . import schema


class ModelBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.evidence: dict[str, dict[str, Any]] = {}
        self.subsystems: dict[str, dict[str, Any]] = {}
        self.conflicts: list[str] = []

    # evidence -----------------------------------------------------------
    def ev(self, file: str, line_start: int | None, line_end: int | None = None, **kw: Any) -> str:
        e = schema.evidence(file, line_start, line_end, **kw)
        existing = self.evidence.get(e["id"])
        if existing is None:
            self.evidence[e["id"]] = e
        return e["id"]

    def add_evidence_record(self, e: dict[str, Any]) -> str:
        self.evidence.setdefault(e["id"], e)
        return e["id"]

    # nodes --------------------------------------------------------------
    def add_node(self, n: dict[str, Any], *, override: bool = False) -> dict[str, Any]:
        cur = self.nodes.get(n["id"])
        if cur is None:
            self.nodes[n["id"]] = n
            return n
        if cur["kind"] != n["kind"]:
            self.conflicts.append(
                f"node {n['id']}: kind {cur['kind']!r} vs {n['kind']!r} (kept {cur['kind']!r})"
            )
        for eid in n.get("evidence_ids", []):
            if eid not in cur["evidence_ids"]:
                cur["evidence_ids"].append(eid)
        cur["tags"] = sorted(set(cur.get("tags", [])) | set(n.get("tags", [])))
        for key in ("symbols", "inputs", "outputs"):
            for v in n.get(key, []) or []:
                if v not in cur.setdefault(key, []):
                    cur[key].append(v)
        for key in ("description", "subsystem", "parent", "name"):
            if n.get(key) and (override or not cur.get(key)):
                cur[key] = n[key]
        for k, v in (n.get("metadata") or {}).items():
            if override or k not in cur.setdefault("metadata", {}):
                cur["metadata"][k] = v
        return cur

    def node(self, id: str, name: str, kind: str, **kw: Any) -> dict[str, Any]:
        return self.add_node(schema.node(id, name, kind, **kw))

    # edges --------------------------------------------------------------
    def add_edge(self, e: dict[str, Any]) -> dict[str, Any] | None:
        if e["from"] == e["to"] and e["kind"] in ("imports", "inherits"):
            return None
        cur = self.edges.get(e["id"])
        if cur is None:
            self.edges[e["id"]] = e
            return e
        for eid in e.get("evidence_ids", []):
            if eid not in cur["evidence_ids"]:
                cur["evidence_ids"].append(eid)
        cur["payload"] = sorted(set(cur.get("payload", [])) | set(e.get("payload", [])))
        if e.get("label") and not cur.get("label"):
            cur["label"] = e["label"]
        if cur.get("phase", "unknown") == "unknown" and e.get("phase", "unknown") != "unknown":
            cur["phase"] = e["phase"]
        for k, v in (e.get("metadata") or {}).items():
            cur.setdefault("metadata", {}).setdefault(k, v)
        return cur

    def edge(self, src: str, dst: str, kind: str, **kw: Any) -> dict[str, Any] | None:
        return self.add_edge(schema.edge(src, dst, kind, **kw))

    # subsystems -----------------------------------------------------------
    def subsystem(self, id: str, name: str, *, description: str = "", paths: list[str] | None = None,
                  override: bool = False) -> dict[str, Any]:  # fmt: skip
        cur = self.subsystems.get(id)
        if cur is None or override:
            prev_paths = cur.get("paths", []) if cur else []
            cur = {"id": id, "name": name, "description": description, "paths": []}
            self.subsystems[id] = cur
            cur["paths"] = list(prev_paths) if not paths else []
        for p in paths or []:
            if p not in cur["paths"]:
                cur["paths"].append(p)
        if description and not cur.get("description"):
            cur["description"] = description
        return cur

    def to_fragment(self) -> dict[str, Any]:
        return {
            "subsystems": list(self.subsystems.values()),
            "nodes": list(self.nodes.values()),
            "edges": list(self.edges.values()),
            "evidence": list(self.evidence.values()),
        }
