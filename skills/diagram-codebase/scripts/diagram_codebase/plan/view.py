"""Indexed, read-only view over the architecture model plus aggregation helpers.

Diagrams are drawn at one of three granularities ("levels"):
  subsystem  - each node is drawn as its subsystem (cross-cutting nodes stay themselves)
  module     - each code node is drawn as its file/module
  symbol     - functions/classes are drawn individually
Model edges are mapped onto the chosen units and aggregated with summarized labels.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

# Nodes that are never folded into a subsystem/module unit.
CROSS_CUTTING = {
    "datastore", "external_service", "event_channel", "topic", "ros_service", "ros_action",
    "deployment_unit", "tf_frame", "db_entity", "library",
}  # fmt: skip
CODE_KINDS = {"module", "class", "function", "ui_component", "package"}
NOISE_EDGE_KINDS = {"imports", "inherits", "config_dependency"}

CATEGORY_BY_KIND = {
    "module": "app", "package": "app", "class": "app", "function": "app", "service": "app",
    "worker": "app", "ros_node": "app", "ui_component": "app", "api_endpoint": "app",
    "algorithm": "processing", "datastore": "data", "db_entity": "data", "data_artifact": "data",
    "event_channel": "messaging", "topic": "messaging", "ros_service": "messaging", "ros_action": "messaging",
    "external_service": "external", "library": "external", "config": "infra",
    "deployment_unit": "infra", "tf_frame": "infra", "state": "app",
}  # fmt: skip

VERBS = {
    "calls": "calls", "imports": "imports", "inherits": "extends", "instantiates": "creates",
    "data_flow": "sends data", "publishes": "publishes", "subscribes": "delivers", "api_request": "requests",
    "handles": "handled by", "db_read": "reads", "db_write": "writes", "db_access": "uses",
    "ipc": "IPC", "state_transition": "transitions", "config_dependency": "configured by",
    "tf_transform": "transform", "service_call": "calls service", "service_provide": "serves",
    "action_call": "sends goal", "action_provide": "serves action", "external_call": "calls API",
    "spawns": "spawns", "triggers": "triggers", "launches": "launches", "depends_on": "depends on",
    "error_path": "on error",
}  # fmt: skip
ASYNC_KINDS = {"publishes", "subscribes", "spawns", "triggers", "external_call"}


class ModelView:
    def __init__(self, model: dict[str, Any]) -> None:
        self.model = model
        self.nodes: dict[str, dict] = {n["id"]: n for n in model["nodes"]}
        self.edges: list[dict] = model["edges"]
        self.subsystems: dict[str, dict] = {s["id"]: s for s in model["subsystems"]}
        self.out: dict[str, list[dict]] = defaultdict(list)
        self.inc: dict[str, list[dict]] = defaultdict(list)
        for e in self.edges:
            self.out[e["from"]].append(e)
            self.inc[e["to"]].append(e)
        self.children: dict[str, list[str]] = defaultdict(list)
        for n in model["nodes"]:
            if n.get("parent"):
                self.children[n["parent"]].append(n["id"])
        self._handler_of: dict[str, str] = {}
        for e in self.edges:
            if e["kind"] == "handles":
                self._handler_of.setdefault(e["from"], e["to"])

    # ------------------------------------------------------------ structure
    def module_of(self, nid: str) -> str:
        seen = set()
        cur = nid
        while cur in self.nodes and cur not in seen:
            seen.add(cur)
            n = self.nodes[cur]
            if n["kind"] == "module":
                return cur
            if not n.get("parent"):
                break
            cur = n["parent"]
        return nid

    def is_significant(self, nid: str) -> bool:
        """Has at least one relationship other than imports/inheritance/config."""
        return any(e["kind"] not in NOISE_EDGE_KINDS for e in self.out[nid] + self.inc[nid])

    def unit(self, nid: str, level: str) -> str | None:
        n = self.nodes.get(nid)
        if n is None:
            return None
        kind = n["kind"]
        if kind == "api_endpoint" and nid in self._handler_of and level != "symbol":
            return self.unit(self._handler_of[nid], level)
        if kind in CROSS_CUTTING or kind == "config":
            return nid
        if level == "subsystem":
            return n.get("subsystem") or self.module_of(nid)
        if level == "module":
            if kind in (
                "ros_node",
                "service",
                "worker",
                "algorithm",
                "data_artifact",
                "api_endpoint",
                "state",
            ):
                return nid
            return self.module_of(nid)
        return nid

    def label(self, uid: str) -> str:
        if uid in self.subsystems:
            return self.subsystems[uid]["name"]
        n = self.nodes.get(uid)
        if n is None:
            return uid
        if n["kind"] == "module":
            f = n.get("metadata", {}).get("file") or n["name"]
            return f.rsplit("/", 1)[-1]
        return n["name"]

    def kind_of(self, uid: str) -> str:
        if uid in self.subsystems:
            return "subsystem"
        return self.nodes[uid]["kind"] if uid in self.nodes else "unknown"

    def category(self, uid: str) -> str:
        if uid in self.subsystems:
            sub_nodes = [n for n in self.nodes.values() if n.get("subsystem") == uid]
            kinds = Counter(CATEGORY_BY_KIND.get(n["kind"], "app") for n in sub_nodes)
            if any("algorithm" in n.get("tags", []) for n in sub_nodes) and kinds.get(
                "processing", 0
            ) * 3 >= len(sub_nodes):
                return "processing"
            return "app"
        n = self.nodes.get(uid)
        if n is None:
            return "app"
        if "error" in n.get("tags", []):
            return "error"
        if "algorithm" in n.get("tags", []):
            return "processing"
        return CATEGORY_BY_KIND.get(n["kind"], "app")

    def subsystem_is_ui(self, sid: str) -> bool:
        sub_nodes = [n for n in self.nodes.values() if n.get("subsystem") == sid]
        ui = sum(1 for n in sub_nodes if n["kind"] == "ui_component" or "ui" in n.get("tags", []))
        return bool(sub_nodes) and ui * 2 >= len([n for n in sub_nodes if n["kind"] in CODE_KINDS])

    # ------------------------------------------------------------ aggregation
    def aggregate(
        self,
        level: str,
        *,
        scope: set[str] | None = None,
        kinds: set[str] | None = None,
        exclude_kinds: set[str] | None = None,
        imports_fallback: bool = True,
        unit_override: dict[str, str] | None = None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Map edges onto units. Returns (units, aggregated edges)."""
        exclude_kinds = set(exclude_kinds or set())
        pairs: dict[tuple[str, str], list[dict]] = defaultdict(list)
        import_pairs: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for e in self.edges:
            if scope is not None and (e["from"] not in scope or e["to"] not in scope):
                continue
            if kinds is not None and e["kind"] not in kinds:
                continue
            if e["kind"] in exclude_kinds:
                continue
            if e["kind"] == "handles" and level != "symbol":
                continue  # endpoint folded into its handler's unit
            a = (unit_override or {}).get(e["from"]) or self.unit(e["from"], level)
            b = (unit_override or {}).get(e["to"]) or self.unit(e["to"], level)
            if not a or not b or a == b:
                continue
            if e["kind"] == "imports":
                import_pairs[(a, b)].append(e)
            elif e["kind"] not in NOISE_EDGE_KINDS:
                pairs[(a, b)].append(e)
        if imports_fallback:
            for pair, es in import_pairs.items():
                if pair not in pairs and (pair[1], pair[0]) not in pairs:
                    pairs[pair] = es
        units: set[str] = set()
        out = []
        for (a, b), es in sorted(pairs.items()):
            units.update((a, b))
            out.append(summarize(a, b, es))
        return sorted(units), out


def summarize(a: str, b: str, es: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = Counter(e["kind"] for e in es)
    main_kind = kinds.most_common(1)[0][0]
    payload = sorted({p for e in es for p in e.get("payload", []) if p})
    labels = [e.get("label") for e in es if e.get("label")]
    if main_kind in ("publishes", "subscribes") and payload:
        label = ", ".join(payload[:2]) + ("..." if len(payload) > 2 else "")
    elif len(es) == 1 and labels:
        label = labels[0]
    elif main_kind in ("db_read", "db_write") and payload:
        verb = "reads" if main_kind == "db_read" else "writes"
        if kinds.get("db_read") and kinds.get("db_write"):
            verb = "reads/writes"
        label = f"{verb} {', '.join(payload[:2])}"
    elif main_kind == "calls":
        label = "calls" if len(es) == 1 else f"calls ({len(es)})"
    else:
        label = VERBS.get(main_kind, main_kind)
    statuses = [e.get("evidence_status", "unknown") for e in es]
    return {
        "from": a,
        "to": b,
        "kind": main_kind,
        "kinds": dict(kinds),
        "label": label,
        "model_edges": [e["id"] for e in es],
        "evidence_status": next(
            (
                s
                for s in ("confirmed", "dynamic_observed", "static_inferred", "documented_only")
                if s in statuses
            ),
            "unknown",
        ),
        "import_only": set(kinds) == {"imports"},
        "async": main_kind in ASYNC_KINDS,
        "error": main_kind == "error_path",
    }
