"""Diagram planner: architecture model -> plan.json (a list of DiagramSpec).

Deterministic. Every diagram answers one specific question; diagrams that would
be empty or trivial are listed in `skipped` with a reason instead. Anything left
out of a drawn diagram is listed in that diagram's `notes` (no silent
truncation). See docs/PLANNER.md for triggers, thresholds and how to add a type.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Any

from ..common import slug
from . import archmode
from .graph import connected_components, jaccard, label_propagation, pack, strongly_connected
from .view import CROSS_CUTTING, SYMBOL_KINDS, VERBS, ModelView, summarize

ROW = {
    "master": 0, "subsystem": 1, "dataflow": 2, "execution": 2, "sequence": 2,
    "algorithm": 3, "state": 3, "dependency": 4, "infrastructure": 4, "ros2": 4, "erd": 4,
}  # fmt: skip
# Generation order when the per-depth cap is applied (lower first).
TIER = {
    "master": 0, "ros2-graph": 1, "subsystem": 2, "dataflow": 3, "execution": 4, "sequence": 5,
    "algorithm": 6, "erd": 7, "state": 8, "dependency": 9, "infrastructure": 10, "ros2-tf": 11,
    "extra": 12,
}  # fmt: skip

LABEL_MAX = 48
CHANNEL_KINDS = {"event_channel", "topic"}
CROSS_GROUPS = {
    "datastore": ("Data stores", "data"), "db_entity": ("Data stores", "data"),
    "event_channel": ("Messaging", "messaging"), "topic": ("Messaging", "messaging"),
    "ros_service": ("Messaging", "messaging"), "ros_action": ("Messaging", "messaging"),
    "external_service": ("External services", "external"), "library": ("External services", "external"),
}  # fmt: skip
DATA_KINDS = {
    "publishes", "subscribes", "triggers", "db_read", "db_write", "api_request", "handles",
    "external_call", "data_flow",
}  # fmt: skip
# Edge kinds that have their own diagram and would only clutter the master.
MASTER_EXCLUDE = {"tf_transform", "config_dependency"}
DEPLOY_EDGE_KINDS = {"depends_on", "launches"}
EXEC_EXPAND = {"calls", "instantiates", "spawns", "triggers", "handles", "publishes", "api_request"}
EXEC_LEAF = {"db_read", "db_write", "db_access", "external_call", "service_call", "action_call"}
ROS_EDGE_KINDS = {
    "publishes", "subscribes", "service_call", "service_provide", "action_call", "action_provide",
}  # fmt: skip
ROS_KINDS = {"ros_node", "topic", "ros_service", "ros_action"}
ROLE_SHAPES = {
    "input": "lean_r", "output": "lean_l", "storage": "cylinder", "intermediate": "lean_r",
    "transform": "rect",
}  # fmt: skip
STAGE_SHAPES = {
    "decision": "diamond", "io": "lean_r", "terminal": "stadium", "loop": "hexagon",
    "error": "rect", "step": "rect",
}  # fmt: skip
EXEC_DEPTH = 6
SEQ_DEPTH = 4
REDUNDANT_JACCARD = 0.8
ERD_MAX = 20


def plan_diagrams(model: dict[str, Any], options: dict[str, Any], config: dict[str, Any]) -> dict:
    """Plan the diagrams for `model`. Returns the plan.json structure (see docs/DESIGN.md).

    options: depth (overview|standard|deep), types (list; empty = all), focus (str|None),
    generated_at (optional timestamp stamped by the caller; kept out of the planner so the
    output is deterministic).
    """
    return _Planner(model, options, config).run()


class _Planner:
    def __init__(self, model: dict[str, Any], options: dict[str, Any], config: dict[str, Any]):
        self.model = model
        self.v = ModelView(model)
        self.cfg = config["plan"]
        self.depth = options.get("depth") or "standard"
        self.types = sorted(set(options.get("types") or []))
        self.focus = (options.get("focus") or "").strip() or None
        self.options = {"depth": self.depth, "types": self.types, "focus": self.focus}
        self.generated_at = options.get("generated_at", "")
        self.max_nodes = self.cfg["max_nodes"]
        self.max_edges = self.cfg["max_edges"]
        self.candidates: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.scope: set[str] | None = None
        self.focus_subsystems: list[str] = []

    # ================================================================ driver
    def wants(self, typ: str) -> bool:
        return not self.types or typ in self.types

    def skip(self, typ: str, reason: str, did: str | None = None) -> None:
        entry = {"type": typ, "reason": reason}
        if did:
            entry["id"] = did
        self.skipped.append(entry)

    def run(self) -> dict[str, Any]:
        focus_ok = self._resolve_focus()
        if self.wants("master"):
            self._master()
        if focus_ok:
            steps = [
                ("ros2", self._ros2), ("subsystem", self._subsystems), ("dataflow", self._dataflows),
                ("execution", self._executions), ("sequence", self._sequences),
                ("algorithm", self._algorithms), ("erd", self._erd), ("state", self._states),
                ("dependency", self._dependency), ("infrastructure", self._infrastructure),
            ]  # fmt: skip
            for typ, fn in steps:
                if self.wants(typ):
                    fn()
        diagrams = self._apply_caps()
        return {
            "generated_at": self.generated_at,
            "model_revision": (self.model.get("meta") or {}).get("revision") or "",
            "options": self.options,
            "diagrams": diagrams,
            "skipped": self.skipped,
            "coverage": self._coverage(diagrams),
        }

    def _apply_caps(self) -> list[dict[str, Any]]:
        ordered = sorted(self.candidates, key=lambda d: (d["_tier"], d["_order"], d["id"]))
        cap = self.cfg["caps"][self.depth]
        out = []
        for d in ordered:
            if len(out) >= cap:
                self.skip(
                    d["type"], f"cap of {cap} diagrams for depth '{self.depth}' reached", d["id"]
                )
                continue
            d = {k: v for k, v in d.items() if not k.startswith("_")}
            d["priority"] = len(out)
            out.append(d)
        return out

    def add(self, spec: dict[str, Any], tier: str, order: int = 0) -> None:
        spec["_tier"] = TIER[tier]
        spec["_order"] = order
        self.candidates.append(spec)

    def redundant_with(self, model_nodes: list[str]) -> str | None:
        for d in self.candidates:
            if jaccard(model_nodes, d["model_nodes"]) > REDUNDANT_JACCARD:
                return d["id"]
        return None

    # ================================================================ focus
    def _resolve_focus(self) -> bool:
        if not self.focus:
            return True
        f = self.focus.lower()
        v = self.v
        subs = sorted(
            sid
            for sid, s in v.subsystems.items()
            if f in sid.lower()
            or f in (s.get("name") or "").lower()
            or any(f in (p or "").lower() for p in s.get("paths", []))
        )
        matched: set[str] = set()
        if subs:
            for sid in subs:
                matched.update(v.by_subsystem.get(sid, []))
        else:
            matched = {
                nid for nid, n in v.nodes.items() if f in n["name"].lower() or f in nid.lower()
            }
            subs = sorted({v.nodes[n]["subsystem"] for n in matched if v.nodes[n].get("subsystem")})
        if not matched:
            self.skip(
                "focus",
                f"--focus '{self.focus}' matches no subsystem or node; only the master diagram is planned",
            )
            return False
        scope = set(matched)
        for nid in matched:
            scope.update(e["to"] for e in v.out[nid])
            scope.update(e["from"] for e in v.inc[nid])
        self.scope = scope
        self.focus_subsystems = subs
        return True

    def in_scope(self, *ids: str) -> bool:
        return self.scope is None or any(i in self.scope for i in ids if i)

    # ================================================================ spec helpers
    def _flowchart(
        self,
        *,
        did: str,
        typ: str,
        title: str,
        purpose: str,
        level: str,
        units: list[str],
        edges: list[dict[str, Any]],
        notes: list[str],
        direction: str = "LR",
        group_of: dict[str, tuple[str, str, str]] | None = None,
        members: dict[str, list[str]] | None = None,
        category: dict[str, str] | None = None,
        shape: dict[str, str] | None = None,
        label: dict[str, str] | None = None,
        min_group: int = 2,
    ) -> dict[str, Any]:
        """Build a flowchart DiagramSpec from units and aggregated edges."""
        v = self.v
        groups, assign = _groups(units, group_of or {}, min_group)
        members = members or {}
        nodes = []
        for u in sorted(
            units, key=lambda u: (assign.get(u) or "", (label or {}).get(u) or v.label(u), u)
        ):
            nodes.append(
                {
                    "id": u,
                    "label": (label or {}).get(u) or v.label(u),
                    "shape": (shape or {}).get(u) or v.shape(u),
                    "category": (category or {}).get(u) or v.category(u),
                    "group": assign.get(u),
                    "model_ids": members.get(u) or ([u] if u in v.nodes else []),
                }
            )
        spec = _base(did, typ, title, purpose, "flowchart", direction, level)
        spec["groups"] = groups
        spec["nodes"] = nodes
        spec["edges"] = [_edge_entry(e) for e in sorted(edges, key=lambda e: (e["from"], e["to"]))]
        spec["notes"] = notes + _long_label_notes(n["label"] for n in nodes)
        spec["model_nodes"] = sorted({m for n in nodes for m in n["model_ids"]})
        return spec

    def _fit(
        self,
        units: list[str],
        edges: list[dict[str, Any]],
        max_nodes: int | None = None,
        max_edges: int | None = None,
        pinned: set[str] | None = None,
    ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        """Shrink to the readability limits, recording every omission in notes."""
        v = self.v
        max_nodes = max_nodes or self.max_nodes
        max_edges = max_edges or self.max_edges
        pinned = pinned or set()
        units = sorted(set(units))
        edges = [dict(e) for e in edges]
        notes: list[str] = []
        if len(units) > max_nodes:
            cfg = [u for u in units if v.kind_of(u) == "config" and u not in pinned]
            if cfg:
                units = [u for u in units if u not in cfg]
                edges = [e for e in edges if e["from"] in units and e["to"] in units]
                notes.append(f"{len(cfg)} configuration units omitted: {_names(v, cfg)}")
        if len(units) > max_nodes:
            chans = [u for u in units if v.kind_of(u) in CHANNEL_KINDS and u not in pinned]
            if chans:
                edges = _collapse_channels(v, chans, edges)
                units = [u for u in units if u not in chans]
                notes.append(
                    f"{len(chans)} channels drawn as direct producer-to-consumer edges labelled "
                    f"with the channel name: {_names(v, chans)}"
                )
        if len(units) > max_nodes:
            deg: Counter = Counter()
            for e in edges:
                deg[e["from"]] += 1
                deg[e["to"]] += 1
            ranked = sorted(units, key=lambda u: (u not in pinned, -deg[u], v.label(u), u))
            keep = set(ranked[:max_nodes])
            dropped = [u for u in ranked if u not in keep]
            lost = [e for e in edges if e["from"] not in keep or e["to"] not in keep]
            units = sorted(keep)
            edges = [e for e in edges if e["from"] in keep and e["to"] in keep]
            notes.append(
                f"{len(dropped)} least-connected units omitted (with {len(lost)} edges): "
                f"{_names(v, dropped)}"
            )
        if len(edges) > max_edges:
            over = len(edges) - max_edges
            order = sorted(
                edges,
                key=lambda e: (not e.get("import_only"), e.get("weight", 1), e["from"], e["to"]),
            )
            drop = order[:over]
            dropped_ids = {(e["from"], e["to"]) for e in drop}
            imports = sum(1 for e in drop if e.get("import_only"))
            edges = [e for e in edges if (e["from"], e["to"]) not in dropped_ids]
            parts = []
            if imports:
                parts.append(f"{imports} import-only")
            if over - imports:
                parts.append(f"{over - imports} lowest-weight")
            notes.append(f"{over} edges omitted ({', '.join(parts)}) to stay within {max_edges}")
        return units, edges, notes

    def _context_override(
        self, inside: set[str], same_sub_level: str | None = None
    ) -> dict[str, str]:
        """Units for 1-hop neighbours outside `inside`: cross-cutting nodes stay themselves,
        code collapses to its subsystem (or module when `same_sub_level` says so)."""
        v = self.v
        override: dict[str, str] = {}
        inside_subs = {v.nodes[n].get("subsystem") for n in inside}
        for nid in sorted(inside):
            for e in v.out[nid] + v.inc[nid]:
                other = e["to"] if e["from"] == nid else e["from"]
                if other in inside or other not in v.nodes:
                    continue
                n = v.nodes[other]
                if n["kind"] in CROSS_CUTTING or n["kind"] == "config":
                    override[other] = other
                elif n.get("subsystem") and n.get("subsystem") in inside_subs:
                    # Same subsystem but not drawn at this level (e.g. module-level code
                    # around a symbol diagram): only other split parts appear as context.
                    if same_sub_level:
                        override[other] = v.unit(other, same_sub_level) or other
                else:
                    override[other] = n.get("subsystem") or v.module_of(other)
        return override

    # ================================================================ master
    def _master(self) -> None:
        v = self.v
        lo, hi = self.cfg["master_target_min"], self.cfg["master_target_max"]
        sig_subs = [
            sid for sid in sorted(v.subsystems) if v.significant_in(sid, SYMBOL_KINDS | {"module"})
        ]
        level = "subsystem" if len(sig_subs) >= 3 else "module"
        exclude = set(MASTER_EXCLUDE)
        if self.model.get("dataflows") and self.wants("dataflow"):
            exclude.add("data_flow")  # data artifacts get their own data-flow diagram
        if self._deployment_units() >= 2:
            # Deployment wiring has its own diagram; mixing it with code boxes in the
            # overview puts two abstraction levels side by side.
            exclude |= DEPLOY_EDGE_KINDS
        units, edges = v.aggregate(level, exclude_kinds=exclude)
        if level == "subsystem" and len(units) < lo:
            mu, me = v.aggregate("module", exclude_kinds=exclude)
            if len(mu) <= hi:
                level, units, edges = "module", mu, me
        elif level == "module" and len(units) > hi and len(v.subsystems) >= 2:
            su, se = v.aggregate("subsystem", exclude_kinds=exclude)
            if len(su) >= 3:
                level, units, edges = "subsystem", su, se
        if len(units) < 2:
            self.skip(
                "master", "the model has no relationships between components to draw", "master"
            )
            return
        units, edges, notes = self._fit(units, edges, max_nodes=hi)
        # Package `__init__` files reached only through imports carry no architecture.
        linked = {x for e in edges if not e.get("import_only") for x in (e["from"], e["to"])}
        inits = [
            u
            for u in units
            if v.kind_of(u) == "module" and u.endswith("__init__.py") and u not in linked
        ]
        if inits and len(units) - len(inits) >= 2:
            units = [u for u in units if u not in inits]
            edges = [e for e in edges if e["from"] in units and e["to"] in units]
            notes.append(f"{len(inits)} package __init__ module(s) with only imports omitted")
        members = v.members(level, units)
        repo = ((self.model.get("meta") or {}).get("repo") or {}).get("name") or "System"
        title = f"{repo} system overview"
        what = "subsystems" if level == "subsystem" else "modules"
        purpose = (
            f"How the main {what} fit together and which data stores, channels and "
            f"external services they use."
        )
        if level == "subsystem":
            eligible, lanes, _reasons = archmode.check(
                v, units, edges, self.cfg["architecture_max_edges"]
            )
            if eligible:
                spec = self._flowchart(
                    did="master", typ="master", title=title, purpose=purpose, level=level,
                    units=units, edges=edges, notes=notes, members=members,
                )  # fmt: skip
                spec["renderer"] = "architecture"
                spec["groups"] = []
                for n in spec["nodes"]:
                    n["group"] = None
                spec["lanes"] = {u: lanes[u] for u in sorted(units)}
                for e in spec["edges"]:
                    ends = (lanes[e["from"]], lanes[e["to"]])
                    e["style"] = "dotted" if {"async", "external"} & set(ends) else "solid"
                self.add(spec, "master")
                return
        # Cross-cutting groups need 2+ members; subsystem boxes hold even a single module
        # so every module sits inside its subsystem.
        group_of: dict[str, tuple[str, str, str]] = {}
        cross = Counter(
            CROSS_GROUPS[v.kind_of(u)][0] for u in units if v.kind_of(u) in CROSS_GROUPS
        )
        for u in units:
            kind = v.kind_of(u)
            if kind in CROSS_GROUPS:
                lab, cat = CROSS_GROUPS[kind]
                if cross[lab] >= 2:
                    group_of[u] = ("g-" + slug(lab), lab, cat)
            elif level == "module" and len(v.subsystems) > 1:
                sid = (v.nodes.get(u) or {}).get("subsystem")
                if sid:
                    group_of[u] = ("g-" + slug(sid), v.label(sid), "app")
        spec = self._flowchart(
            did="master", typ="master", title=title, purpose=purpose, level=level,
            units=units, edges=edges, notes=notes, group_of=group_of, members=members,
            min_group=1,
        )  # fmt: skip
        self.add(spec, "master")

    # ================================================================ subsystems
    def _subsystems(self) -> None:
        v = self.v
        cap = self.cfg["max_subsystem_diagrams"][self.depth]
        cross: Counter = Counter()
        for e in v.edges:
            a = (v.nodes.get(e["from"]) or {}).get("subsystem")
            b = (v.nodes.get(e["to"]) or {}).get("subsystem")
            if a != b:
                cross[a] += 1
                cross[b] += 1
        ranked = []
        for sid in sorted(v.subsystems):
            if self.focus and sid not in self.focus_subsystems:
                continue
            sig = v.significant_in(sid)
            if len(sig) < 2:
                self.skip(
                    "subsystem",
                    f"{v.label(sid)} has fewer than 2 components with relationships",
                    f"subsystem-{_sid_slug(sid)}",
                )
                continue
            ranked.append((-(len(sig) + cross[sid]), sid))
        ranked.sort()
        for i, (_score, sid) in enumerate(ranked):
            did = f"subsystem-{_sid_slug(sid)}"
            if i >= cap:
                self.skip(
                    "subsystem",
                    f"max_subsystem_diagrams ({cap}) for depth '{self.depth}' reached",
                    did,
                )
                continue
            specs = self._subsystem_specs(sid, did)
            master = self.master_spec()
            if len(specs) == 1 and master is not None and not self._adds_detail(specs[0], sid):
                self.skip("subsystem", f"{v.label(sid)} adds no detail beyond 'master'", did)
                continue
            for j, spec in enumerate(specs):
                self.add(spec, "subsystem", i * 100 + j)

    def master_spec(self) -> dict[str, Any] | None:
        return next((d for d in self.candidates if d["id"] == "master"), None)

    def _adds_detail(self, spec: dict[str, Any], sid: str) -> bool:
        """True when the subsystem diagram draws its own content with more boxes than master."""
        inside = {n for n, node in self.v.nodes.items() if node.get("subsystem") == sid}

        def count(d: dict[str, Any]) -> int:
            return sum(1 for n in d["nodes"] if inside & set(n.get("model_ids") or []))

        master = self.master_spec()
        return master is None or count(spec) > count(master)

    def covered_by(
        self, spec: dict[str, Any], slack: int = 3, any_level: bool = False
    ) -> str | None:
        """Id of a planned diagram at the same level that already draws every node and
        connection of `spec` with at most `slack` extra boxes (the same picture twice)."""
        nodes = {n["id"] for n in spec["nodes"]}
        pairs = {frozenset((e["from"], e["to"])) for e in spec["edges"]}
        for d in self.candidates:
            if not any_level and d.get("level") != spec.get("level"):
                continue
            if len(d["nodes"]) > len(nodes) + slack:
                continue
            if nodes <= {n["id"] for n in d["nodes"]} and pairs <= {
                frozenset((e["from"], e["to"])) for e in d["edges"]
            }:
                return d["id"]
        return None

    def _subsystem_specs(self, sid: str, did: str) -> list[dict[str, Any]]:
        v = self.v
        name = v.label(sid)
        purpose = (
            f"What is inside {name}, how its parts call each other, and what it talks to outside."
        )
        inside = {n for n in v.significant_in(sid, SYMBOL_KINDS)}
        override = self._context_override(inside)
        scope = inside | set(override)
        units, edges = v.aggregate("symbol", scope=scope, unit_override=override, touching=inside)
        inside_units = [u for u in units if u in inside]
        if len(inside_units) >= 2 and len(units) <= self.max_nodes:
            return [
                self._subsystem_spec(
                    did, f"{name} internals", purpose, "symbol", inside, override, units, edges, []
                )
            ]
        # Module level.
        inside = set(v.by_subsystem.get(sid, []))
        inside = {n for n in inside if v.nodes[n]["kind"] != "config"}
        override = self._context_override(inside)
        scope = inside | set(override)
        units, edges = v.aggregate("module", scope=scope, unit_override=override, touching=inside)
        inside_units = sorted(u for u in units if u not in set(override.values()))
        if len(inside_units) < 2:
            self.skip("subsystem", f"{name} has fewer than 2 connected modules", did)
            return []
        if len(units) <= self.max_nodes:
            return [
                self._subsystem_spec(
                    did, f"{name} modules", purpose, "module", inside, override, units, edges, []
                )
            ]
        context = len(set(units) - set(inside_units))
        size = max(self.max_nodes - min(context, self.max_nodes // 3), 2)
        parts = self._split_modules(sid, inside_units, edges, size)
        out = []
        for k, part in enumerate(parts, start=1):
            part_inside = {n for n in inside if v.unit(n, "module") in part}
            ov = self._context_override(part_inside, same_sub_level="module")
            sc = part_inside | set(ov)
            pu, pe = v.aggregate("module", scope=sc, unit_override=ov, touching=part_inside)
            if not [u for u in pu if u in part]:
                continue
            hint = _common_dir(v, part)
            title = f"{name} modules (part {k} of {len(parts)}{': ' + hint if hint else ''})"
            notes = [
                f"{name} is split into {len(parts)} diagrams; modules of other parts appear as context."
            ]
            out.append(
                self._subsystem_spec(
                    f"{did}-part-{k}", title, purpose, "module", part_inside, ov, pu, pe, notes
                )
            )
        return out

    def _subsystem_spec(self, did, title, purpose, level, inside, override, units, edges, notes):
        v = self.v
        context_units = set(override.values()) - {v.unit(n, level) for n in inside}
        pinned = {u for u in units if u not in context_units}
        units, edges, fit_notes = self._fit(units, edges, pinned=pinned)
        category = {
            u: "context"
            for u in units
            if u in context_units and (u in v.subsystems or v.kind_of(u) not in CROSS_CUTTING)
        }
        group_of = {}
        if level == "symbol":
            for u in units:
                if u not in context_units:
                    mod = v.module_of(u)
                    if mod != u:
                        group_of[u] = ("g-" + slug(mod), v.label(mod), "app")
        scope = inside | set(override)
        members = v.members(level, units, scope=scope, unit_override=override)
        return self._flowchart(
            did=did, typ="subsystem", title=title, purpose=purpose, level=level, units=units,
            edges=edges, notes=notes + fit_notes, group_of=group_of, members=members,
            category=category,
        )  # fmt: skip

    def _split_modules(
        self, sid: str, modules: list[str], edges: list[dict], size: int
    ) -> list[list[str]]:
        v = self.v
        base = ((v.subsystems[sid].get("paths") or [""])[0] or "").strip("/")
        by_dir: dict[str, list[str]] = defaultdict(list)
        for m in modules:
            path = v.file_of(m)
            rel = path[len(base) + 1 :] if base and path.startswith(base + "/") else path
            by_dir[rel.split("/", 1)[0] if "/" in rel else "."].append(m)
        groups = [sorted(g) for _, g in sorted(by_dir.items())]
        if len(groups) >= 2 and all(len(g) <= size for g in groups):
            return pack(groups, size) if len(groups) > 1 else groups
        mods = set(modules)
        pairs = [(e["from"], e["to"]) for e in edges if e["from"] in mods and e["to"] in mods]
        return label_propagation(sorted(modules), pairs, size)

    # ================================================================ dataflow
    def _dataflows(self) -> None:
        v = self.v
        provided = [df for df in self.model.get("dataflows") or [] if df.get("stages")]
        if provided:
            for i, df in enumerate(
                sorted(provided, key=lambda d: d.get("id") or d.get("name", ""))
            ):
                if not self.in_scope(*[s.get("node") for s in df["stages"]]):
                    continue
                self._provided_dataflow(df, i)
            return
        if not v.edges:
            self.skip("dataflow", "no data-moving relationships in the model", "dataflow-derived")
            return
        for level in ("symbol", "module", "subsystem"):
            units, edges = self._data_edges(level)
            if len(units) <= self.max_nodes:
                break
        if len(units) < 4:
            self.skip(
                "dataflow", f"fewer than 4 components move data ({len(units)})", "dataflow-derived"
            )
            return
        units, edges, notes = self._fit(units, edges)
        has_in = {e["to"] for e in edges}
        has_out = {e["from"] for e in edges}
        shape = {}
        for u in units:
            if v.kind_of(u) in CROSS_CUTTING:
                continue
            if u not in has_in:
                shape[u] = "lean_r"
            elif u not in has_out:
                shape[u] = "lean_l"
        members = v.members(level, units, scope=self.scope)
        spec = self._flowchart(
            did="dataflow-derived", typ="dataflow", title="Data flow",
            purpose="Where data enters, which components move or transform it, and where it is stored or sent.",
            level=level, units=units, edges=edges, notes=notes, shape=shape, members=members,
        )  # fmt: skip
        dup = self.redundant_with(spec["model_nodes"])
        if dup:
            self.skip(
                "dataflow",
                f"derived data flow overlaps '{dup}' by more than 80%",
                "dataflow-derived",
            )
            return
        dup = self.covered_by(spec)
        if dup:
            self.skip(
                "dataflow", f"every data path is already drawn in '{dup}'", "dataflow-derived"
            )
            return
        self.add(spec, "dataflow")

    def _data_edges(self, level: str) -> tuple[list[str], list[dict[str, Any]]]:
        """Data-moving edges at `level`, oriented in the direction data travels."""
        v = self.v
        picked = []
        for e in v.edges:
            if e["kind"] not in DATA_KINDS:
                continue
            if e["kind"] == "triggers" and v.kind_of(e["from"]) not in CHANNEL_KINDS:
                continue
            if self.scope is not None and (
                e["from"] not in self.scope or e["to"] not in self.scope
            ):
                continue
            picked.append(e)
        pairs: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for e in picked:
            a, b = v.unit(e["from"], level), v.unit(e["to"], level)
            if not a or not b or a == b:
                continue
            if e["kind"] == "db_read":
                a, b = b, a  # data travels from the store to the reader
            pairs[(a, b)].append(e)
        units: set[str] = set()
        out = []
        for (a, b), es in sorted(pairs.items()):
            units.update((a, b))
            out.append(summarize(a, b, es))
        return sorted(units), out

    def _provided_dataflow(self, df: dict[str, Any], order: int) -> None:
        did = f"dataflow-{slug(df.get('id') or df['name'])}"
        stages = [s for s in df["stages"] if s.get("node")]
        units = sorted({s["node"] for s in stages})
        rep = {s["node"]: s.get("representation") or "" for s in stages}
        edges = []
        for link in df.get("links") or []:
            a, b = link.get("from"), link.get("to")
            if a not in units or b not in units or a == b:
                continue
            edges.append(_plain_edge(a, b, link.get("label") or rep.get(a) or "", "data_flow"))
        edges = _dedupe(edges)
        if len(units) < 2 or not edges:
            self.skip(
                "dataflow", f"data flow '{df.get('name')}' has fewer than 2 linked stages", did
            )
            return
        units, edges, notes = self._fit(units, edges)
        shape = {s["node"]: ROLE_SHAPES.get(s.get("role"), "rect") for s in stages}
        category = {s["node"]: "data" for s in stages if s.get("role") == "storage"}
        spec = self._flowchart(
            did=did, typ="dataflow", title=df.get("name") or "Data flow",
            purpose=df.get("description") or f"How data moves through {df.get('name')}: inputs, transformations and outputs.",
            level="symbol", units=units, edges=edges, notes=notes, shape=shape, category=category,
        )  # fmt: skip
        self.add(spec, "dataflow", order)

    # ================================================================ execution
    def _executions(self) -> None:
        flows = [
            f for f in self.model.get("flows") or [] if f.get("kind", "execution") == "execution"
        ]
        if flows:
            for i, flow in enumerate(sorted(flows, key=lambda f: f["id"])):
                ends = [x for s in flow.get("steps", []) for x in (s.get("from"), s.get("to"))]
                if self.in_scope(*ends):
                    self._provided_execution(flow, i)
            return
        self._derived_executions()

    def _provided_execution(self, flow: dict[str, Any], order: int) -> None:
        v = self.v
        did = f"execution-{_flow_slug(flow['id'])}"
        steps = [
            s for s in flow.get("steps", []) if s.get("from") in v.nodes and s.get("to") in v.nodes
        ]
        units = sorted({x for s in steps for x in (s["from"], s["to"])})
        if len(units) < 2:
            self.skip("execution", f"flow '{flow.get('name')}' has fewer than 2 steps", did)
            return
        shape, category, phase_of = {}, {}, {}
        edges: list[dict[str, Any]] = []
        for s in steps:
            kind = s.get("kind", "call")
            label = s.get("label") or s.get("condition") or ""
            if kind == "loop" and s.get("condition"):
                label = f"loop: {s['condition']}"
            e = _plain_edge(s["from"], s["to"], label, kind)
            if kind in ("async", "event"):
                e["async"] = True
            if kind == "error":
                e["error"] = True
                category[s["to"]] = "error"
            if kind == "decision":
                shape[s["from"]] = "diamond"
            edges.append(e)
            ph = s.get("phase") or "unknown"
            for x in (s["from"], s["to"]):
                phase_of.setdefault(x, ph)
        edges = _dedupe(edges)
        units, edges, notes = self._fit(units, edges)
        spec = self._flowchart(
            did=did, typ="execution", title=flow.get("name") or "Execution flow",
            purpose=f"What happens, step by step, when {flow.get('trigger') or flow.get('name')} runs.",
            level="symbol", units=units, edges=edges, notes=notes, direction="TD",
            group_of=_phase_groups(phase_of, units), shape=shape, category=category, min_group=1,
        )  # fmt: skip
        self.add(spec, "execution", order)

    def _entrypoints(self) -> list[str]:
        v = self.v
        tagged = sorted(n for n, node in v.nodes.items() if "entrypoint" in node.get("tags", []))
        fn_modules = {v.module_of(n) for n in tagged if v.kind_of(n) != "module"}
        return [n for n in tagged if not (v.kind_of(n) == "module" and n in fn_modules)]

    def _derived_executions(self) -> None:
        v = self.v
        entries = [e for e in self._entrypoints() if self.in_scope(e)]
        if not entries:
            self.skip(
                "execution",
                "no entry points or Claude execution flows in the model",
                "execution-derived",
            )
            return
        traces = []
        for ep in entries:
            units, edges, phase_of, notes = self._trace(ep)
            traces.append((-len(units), ep, units, edges, phase_of, notes))
        traces.sort(key=lambda t: (t[0], t[1]))
        limit = 3 if self.depth == "deep" else 1
        added = 0
        names = Counter(v.label(ep) for _n, ep, *_rest in traces)
        for _neg, ep, units, edges, _phase_of, notes in traces:
            name = v.label(ep)
            if names[name] > 1:  # e.g. several main() functions: qualify by module
                name = f"{v.label(v.module_of(ep))} {name}"
            did = f"execution-{slug(name)}"
            if len(units) < 4:
                self.skip(
                    "execution",
                    f"execution from {v.label(ep)} reaches fewer than 4 components",
                    did,
                )
                continue
            if added >= limit:
                self.skip(
                    "execution",
                    f"only {limit} derived execution diagram(s) at depth '{self.depth}'",
                    did,
                )
                continue
            units, edges, fit_notes = self._fit(units, edges, pinned={ep})
            shape = {ep: "stadium"}
            spec = self._flowchart(
                did=did, typ="execution", title=f"Execution from {v.label(ep)}",
                purpose=f"What runs, in order, after the entry point {v.label(ep)} starts.",
                level="symbol", units=units, edges=edges, notes=notes + fit_notes, direction="TD",
                shape=shape,
            )  # fmt: skip
            # No Initialization/Runtime boxes here: a scanned edge's phase says when the
            # relationship is set up (e.g. handler registration), not when the target runs.
            dup = self.redundant_with(spec["model_nodes"])
            reason = f"overlaps '{dup}' by more than 80%"
            if not dup:
                dup = self.covered_by(spec)
                reason = f"is already fully drawn in '{dup}'"
            if dup:
                self.skip("execution", f"execution from {v.label(ep)} {reason}", did)
                continue
            self.add(spec, "execution" if added == 0 else "extra", added)
            added += 1

    def _trace(self, start: str):
        """BFS from an entry point over control edges; side-effect edges become leaves."""
        v = self.v
        seen = {start: 0}
        phase_of = {start: "unknown"}
        order = [start]
        edges: list[dict[str, Any]] = []
        notes: list[str] = []
        queue = deque([start])
        cut_depth = cut_size = 0
        while queue:
            cur = queue.popleft()
            out = sorted(v.out[cur], key=v.edge_position)
            for e in out:
                kind = e["kind"]
                if kind not in EXEC_EXPAND and kind not in EXEC_LEAF:
                    continue
                if self.scope is not None and e["to"] not in self.scope:
                    continue
                nxt = e["to"]
                if nxt not in seen:
                    if seen[cur] + 1 > EXEC_DEPTH:
                        cut_depth += 1
                        continue
                    if len(seen) >= self.max_nodes:
                        cut_size += 1
                        continue
                    seen[nxt] = seen[cur] + 1
                    ph = e.get("phase") or "unknown"
                    phase_of[nxt] = ph if ph in ("init", "runtime") else phase_of[cur]
                    order.append(nxt)
                    if kind in EXEC_EXPAND:
                        queue.append(nxt)
                label = e.get("label") or ("" if kind == "calls" else VERBS.get(kind, kind))
                ed = _plain_edge(cur, nxt, label, kind)
                ed["model_edges"] = [e["id"]]
                ed["evidence_status"] = e.get("evidence_status", "unknown")
                edges.append(ed)
        if cut_depth:
            notes.append(f"{cut_depth} calls deeper than {EXEC_DEPTH} levels not traced")
        if cut_size:
            notes.append(f"{cut_size} further calls omitted to stay within {self.max_nodes} nodes")
        return order, _dedupe(edges), phase_of, notes

    # ================================================================ sequence
    def _sequences(self) -> None:
        v = self.v
        flows = [
            f for f in self.model.get("flows") or [] if f.get("kind") in ("sequence", "request")
        ]
        limit = 3 if self.depth == "deep" else 1
        chains = []
        if flows:
            for f in sorted(flows, key=lambda f: f["id"]):
                ends = [x for s in f.get("steps", []) for x in (s.get("from"), s.get("to"))]
                if self.in_scope(*ends):
                    chains.append(
                        (
                            f"sequence-{_flow_slug(f['id'])}",
                            f.get("name") or f["id"],
                            *self._flow_messages(f),
                        )
                    )
        else:
            starts = sorted(
                (e for e in v.edges if e["kind"] == "api_request"),
                key=lambda e: (e["to"], e["from"]),
            )
            requested = {e["to"] for e in starts}
            for e in starts:
                if self.in_scope(e["from"], e["to"]):
                    chains.append(
                        (
                            f"sequence-{slug(v.label(e['to']))}",
                            f"Request {v.label(e['to'])}",
                            *self._request_chain(e["from"], e["to"]),
                        )
                    )
            for ep in sorted(n for n, node in v.nodes.items() if node["kind"] == "api_endpoint"):
                if ep not in requested and v.handler_of(ep) and self.in_scope(ep):
                    chains.append(
                        (
                            f"sequence-{slug(v.label(ep))}",
                            f"Request {v.label(ep)}",
                            *self._request_chain(None, ep),
                        )
                    )
        if not chains:
            self.skip(
                "sequence",
                "no request chains (api_request edges, endpoints) or Claude sequence flows",
            )
            return
        seen_ids: set[str] = set()
        uniq = []
        for c in chains:
            if c[0] not in seen_ids:
                seen_ids.add(c[0])
                uniq.append(c)
        if not flows:
            uniq.sort(key=lambda c: (-len(c[2]), -len(c[3]), c[0]))
        added = 0
        for did, name, parts, msgs, notes in uniq:
            if len(parts) < 3:
                self.skip("sequence", f"{name} involves fewer than 3 participants", did)
                continue
            if added >= limit:
                self.skip(
                    "sequence", f"only {limit} sequence diagram(s) at depth '{self.depth}'", did
                )
                continue
            spec = _base(
                did,
                "sequence",
                name,
                f"Who talks to whom, in order, during {name}.",
                "sequence",
                "LR",
                "module",
            )
            spec["participants"] = [{"id": p, "label": self._participant_label(p)} for p in parts]
            spec["messages"] = msgs
            members = v.members("module", parts)
            spec["model_nodes"] = sorted({m for p in parts for m in members.get(p, [])})
            spec["notes"] = notes + _long_label_notes(p["label"] for p in spec["participants"])
            self.add(spec, "sequence" if added == 0 else "extra", added)
            added += 1

    def _participant_label(self, uid: str) -> str:
        """Module participants carry their subsystem name when there are several
        (`api.js` -> `Frontend api`), so generic file names stay unambiguous."""
        v = self.v
        label = v.label(uid)
        sid = (v.nodes.get(uid) or {}).get("subsystem")
        if v.kind_of(uid) != "module" or not sid or len(v.subsystems) < 2:
            return label
        sub = v.label(sid)
        stem = label.rsplit(".", 1)[0] if "." in label else label
        return label if sub.lower() in stem.lower() else f"{sub} {stem}"

    def _flow_messages(self, flow: dict[str, Any]):
        v = self.v
        parts: list[str] = []
        msgs = []
        for s in flow.get("steps", []):
            if s.get("from") not in v.nodes or s.get("to") not in v.nodes:
                continue
            a, b = v.unit(s["from"], "module"), v.unit(s["to"], "module")
            for p in (a, b):
                if p not in parts:
                    parts.append(p)
            kind = {"return": "reply", "async": "async", "event": "async"}.get(
                s.get("kind"), "sync"
            )
            msgs.append(
                {
                    "from": a,
                    "to": b,
                    "label": s.get("label") or s.get("condition") or VERBS["calls"],
                    "kind": kind,
                }
            )
        msgs, notes = self._cap_messages(msgs)
        return parts, msgs, notes

    def _request_chain(self, client: str | None, endpoint: str):
        v = self.v
        parts: list[str] = []
        msgs: list[dict[str, Any]] = []

        def part(nid: str) -> str:
            p = v.unit(nid, "module") or nid
            if p not in parts:
                parts.append(p)
            return p

        handler = v.handler_of(endpoint)
        if client:
            src = part(client)
            dst = part(handler or endpoint)
            msgs.append({"from": src, "to": dst, "label": v.label(endpoint), "kind": "sync"})
        else:
            src = endpoint
            parts.append(endpoint)
            dst = part(handler) if handler else endpoint
            msgs.append({"from": src, "to": dst, "label": "handled by", "kind": "sync"})
        visited: set[str] = set()

        def walk(fn: str, depth: int) -> None:
            if fn in visited or depth > SEQ_DEPTH:
                return
            visited.add(fn)
            me = part(fn)
            for e in sorted(v.out[fn], key=v.edge_position):
                k, to = e["kind"], e["to"]
                if self.scope is not None and to not in self.scope:
                    continue
                if k == "calls":
                    callee = part(to) if v.unit(to, "module") != me else me
                    if callee != me:
                        name = v.label(to).rsplit(".", 1)[-1]
                        msgs.append({"from": me, "to": callee, "label": name, "kind": "sync"})
                        walk(to, depth + 1)
                        msgs.append({"from": callee, "to": me, "label": "returns", "kind": "reply"})
                    else:
                        walk(to, depth + 1)
                elif k in (
                    "db_read",
                    "db_write",
                    "db_access",
                    "external_call",
                    "api_request",
                    "publishes",
                    "service_call",
                ):
                    tgt = part(to)
                    kind = "async" if k == "publishes" else "sync"
                    msgs.append(
                        {
                            "from": me,
                            "to": tgt,
                            "label": e.get("label") or VERBS.get(k, k),
                            "kind": kind,
                        }
                    )
                    if k in ("db_read", "external_call", "api_request", "service_call"):
                        msgs.append(
                            {
                                "from": tgt,
                                "to": me,
                                "label": "rows" if k == "db_read" else "response",
                                "kind": "reply",
                            }
                        )

        if handler:
            walk(handler, 0)
        msgs.append({"from": dst, "to": src, "label": "response", "kind": "reply"})
        msgs, notes = self._cap_messages(msgs)
        return parts, msgs, notes

    def _cap_messages(self, msgs: list[dict[str, Any]]):
        limit = self.max_edges
        if len(msgs) <= limit:
            return msgs, []
        return msgs[:limit], [f"{len(msgs) - limit} later messages omitted to stay within {limit}"]

    # ================================================================ algorithm / state
    def _algorithms(self) -> None:
        v = self.v
        algos = self.model.get("algorithms") or []
        if not algos:
            self.skip("algorithm", "no algorithms described in Claude findings")
            return
        for i, algo in enumerate(sorted(algos, key=lambda a: a["id"])):
            did = f"algorithm-{_algo_slug(algo['id'])}"
            stages = algo.get("stages") or []
            fns = [f for s in stages for f in s.get("functions", [])]
            if not self.in_scope(algo.get("node"), *fns):
                continue
            if len(stages) < 2:
                self.skip(
                    "algorithm", f"algorithm '{algo.get('name')}' has fewer than 2 stages", did
                )
                continue
            uid = {s["id"]: f"stage:{algo['id']}:{s['id']}" for s in stages}
            units = [uid[s["id"]] for s in stages]
            label = {uid[s["id"]]: s.get("name") or s["id"] for s in stages}
            shape = {uid[s["id"]]: STAGE_SHAPES.get(s.get("kind", "step"), "rect") for s in stages}
            category = {
                uid[s["id"]]: "error" if s.get("kind") == "error" else "processing" for s in stages
            }
            members = {
                uid[s["id"]]: sorted(f for f in s.get("functions", []) if f in v.nodes)
                for s in stages
            }
            kind_of = {s["id"]: s.get("kind", "step") for s in stages}
            edges = _dedupe([
                _plain_edge(uid[t["from"]], uid[t["to"]], t.get("label") or "",
                            "error_path" if kind_of[t["to"]] == "error" else "calls")
                for t in algo.get("transitions") or [] if t.get("from") in uid and t.get("to") in uid
            ])  # fmt: skip
            units, edges, notes = self._fit(units, edges)
            spec = self._flowchart(
                did=did, typ="algorithm", title=algo.get("name") or algo["id"],
                purpose=algo.get("purpose") or f"How {algo.get('name')} works, stage by stage.",
                level="symbol", units=units, edges=edges, notes=notes, direction="TD",
                label=label, shape=shape, category=category, members=members,
            )  # fmt: skip
            if algo.get("node") in v.nodes:
                spec["model_nodes"] = sorted(set(spec["model_nodes"]) | {algo["node"]})
            self.add(spec, "algorithm", i)

    def _states(self) -> None:
        v = self.v
        machines = self.model.get("state_machines") or []
        if not machines:
            self.skip("state", "no state machines described in Claude findings")
            return
        for i, sm in enumerate(sorted(machines, key=lambda s: s["id"])):
            did = f"state-{slug(sm['id'])}"
            if not self.in_scope(sm.get("node")) and self.scope is not None:
                continue
            states = sm.get("states") or []
            if len(states) < 2:
                self.skip("state", f"state machine '{sm.get('name')}' has fewer than 2 states", did)
                continue
            ids = {s["id"] for s in states}
            trans = [
                {"from": t["from"], "to": t["to"], "label": t.get("label") or ""}
                for t in sm.get("transitions") or []
                if t.get("from") in ids and t.get("to") in ids
            ]
            finals = sm.get("finals")
            if finals is None:
                has_out = {t["from"] for t in trans}
                finals = sorted(s for s in ids if s not in has_out and s != sm.get("initial"))
            spec = _base(
                did,
                "state",
                sm.get("name") or sm["id"],
                f"Which states {sm.get('name')} moves through and what triggers each transition.",
                "state",
                "LR",
                "symbol",
            )
            spec["states"] = [{"id": s["id"], "label": s.get("name") or s["id"]} for s in states]
            spec["transitions"] = trans
            spec["initial"] = sm.get("initial") or ""
            spec["finals"] = sorted(finals)
            spec["model_nodes"] = [sm["node"]] if sm.get("node") in v.nodes else []
            spec["notes"] = []
            self.add(spec, "state", i)

    # ================================================================ structure
    def _dependency(self) -> None:
        v = self.v
        scope = self.scope
        units, edges = v.aggregate("module", kinds={"imports"}, scope=scope)
        level = "module"
        if len(units) > self.max_nodes and len(v.subsystems) >= 2:
            su, se = v.aggregate("subsystem", kinds={"imports"}, scope=scope)
            if len(su) >= 2:
                level, units, edges = "subsystem", su, se
                for e in edges:
                    e["label"] = f"{e['weight']} import" + ("s" if e["weight"] > 1 else "")
        if len(units) < 3 or len(edges) < 2:
            self.skip(
                "dependency",
                f"import graph too small ({len(units)} units, {len(edges)} edges)",
                "dependency",
            )
            return
        spec = self._dependency_spec(
            "dependency",
            "Import dependencies",
            level,
            units,
            edges,
            group=(level == "module" and len(v.subsystems) > 1),
        )
        self.add(spec, "dependency")
        if self.depth != "deep" or level != "subsystem":
            return
        for i, sid in enumerate(sorted(v.subsystems)):
            inside = {
                n for n in v.by_subsystem.get(sid, []) if self.scope is None or n in self.scope
            }
            mu, me = v.aggregate("module", kinds={"imports"}, scope=inside)
            did = f"dependency-{_sid_slug(sid)}"
            if len(mu) < 3 or len(me) < 2:
                continue
            if len(mu) > self.max_nodes:
                self.skip(
                    "dependency",
                    f"{v.label(sid)} module imports exceed {self.max_nodes} units",
                    did,
                )
                continue
            self.add(
                self._dependency_spec(
                    did, f"{v.label(sid)} import dependencies", "module", mu, me, group=False
                ),
                "extra",
                i,
            )

    def _dependency_spec(self, did, title, level, units, edges, group):
        v = self.v
        notes: list[str] = []
        for e in edges:
            e["import_only"] = False  # every edge here is an import; draw solid
            if level == "module":
                e["label"] = ""
        units, edges, fit_notes = self._fit(units, edges)
        sccs = strongly_connected(units, [(e["from"], e["to"]) for e in edges])
        category = {}
        for comp in sccs:
            for u in comp:
                category[u] = "error"
            notes.append("Import cycle: " + " <-> ".join(v.label(u) for u in comp))
        group_of = {}
        if group:
            for u in units:
                sid = (v.nodes.get(u) or {}).get("subsystem")
                if sid:
                    group_of[u] = ("g-" + slug(sid), v.label(sid), "app")
        return self._flowchart(
            did=did, typ="dependency", title=title,
            purpose="Which parts import which (static structure, not runtime calls), including any import cycles.",
            level=level, units=units, edges=edges, notes=notes + fit_notes, group_of=group_of,
            category=category, members=v.members(level, units, scope=self.scope),
        )  # fmt: skip

    def _deployment_units(self) -> int:
        """Deployment units the infrastructure diagram will draw (0 when it is not planned)."""
        if not self.wants("infrastructure") or (
            self.scope is not None and "infrastructure" not in self.types
        ):
            return 0
        return sum(1 for node in self.v.nodes.values() if node["kind"] == "deployment_unit")

    def _infrastructure(self) -> None:
        v = self.v
        if self.scope is not None and "infrastructure" not in self.types:
            self.skip("infrastructure", "outside the --focus scope", "infrastructure")
            return
        deploy = sorted(
            n
            for n, node in v.nodes.items()
            if node["kind"] == "deployment_unit" or n.startswith("deploy:")
        )
        if len([n for n in deploy if v.kind_of(n) == "deployment_unit"]) < 2:
            self.skip(
                "infrastructure",
                "fewer than 2 deployment units (compose/k8s/launch files)",
                "infrastructure",
            )
            return
        units, edges = v.aggregate("symbol", kinds={"depends_on", "launches"})
        units = sorted(set(units) | set(deploy))
        units, edges, notes = self._fit(units, edges, pinned=set(deploy))
        spec = self._flowchart(
            did="infrastructure", typ="infrastructure", title="Deployment and runtime units",
            purpose="Which containers, services and launch files exist and what each one starts or depends on.",
            level="symbol", units=units, edges=edges, notes=notes,
        )  # fmt: skip
        self.add(spec, "infrastructure")

    def _erd(self) -> None:
        v = self.v
        erd = self.model.get("erd") or {}
        ents = sorted(erd.get("entities") or [], key=lambda e: e["id"])
        ents = [e for e in ents if self.in_scope(f"ent:{e['id']}") or self.scope is None]
        if len(ents) < 2:
            self.skip("erd", f"fewer than 2 database entities ({len(ents)})", "erd")
            return
        ids = [e["id"] for e in ents]
        rels = [
            r for r in erd.get("relations") or [] if r.get("from") in ids and r.get("to") in ids
        ]
        parts = [ids]
        if len(ids) > ERD_MAX:
            comps = connected_components(ids, [(r["from"], r["to"]) for r in rels])
            parts = pack(comps, ERD_MAX)
        by_id = {e["id"]: e for e in ents}
        for k, part in enumerate(parts, start=1):
            did = "erd" if len(parts) == 1 else f"erd-part-{k}"
            pset = set(part)
            title = "Database schema" + ("" if len(parts) == 1 else f" (part {k} of {len(parts)})")
            spec = _base(
                did,
                "erd",
                title,
                "Which tables exist, their keys, and how they relate.",
                "erd",
                "LR",
                "symbol",
            )
            spec["entities"] = [
                {"id": i, "attributes": by_id[i].get("attributes") or []} for i in sorted(part)
            ]
            spec["relations"] = [
                {"from": r["from"], "to": r["to"], "cardinality": r.get("cardinality") or "one-to-many",
                 "label": r.get("label") or "", "identifying": bool(r.get("identifying"))}
                for r in sorted(rels, key=lambda r: (r["from"], r["to"]))
                if r["from"] in pset and r["to"] in pset
            ]  # fmt: skip
            cut = [r for r in rels if (r["from"] in pset) != (r["to"] in pset)]
            spec["notes"] = (
                [f"{len(cut)} relations to entities in other parts not drawn"] if cut else []
            )
            spec["model_nodes"] = sorted(f"ent:{i}" for i in part if f"ent:{i}" in v.nodes)
            self.add(spec, "erd", k)

    # ================================================================ ROS 2
    def _ros2(self) -> None:
        v = self.v
        if not any(n["kind"] == "ros_node" for n in v.nodes.values()):
            self.skip("ros2", "not a ROS 2 repository (no ROS nodes found)", "ros2-graph")
            return
        pairs = []
        for e in v.edges:
            if e["kind"] not in ROS_EDGE_KINDS:
                continue
            if v.kind_of(e["from"]) not in ROS_KINDS or v.kind_of(e["to"]) not in ROS_KINDS:
                continue
            if self.scope is not None and (
                e["from"] not in self.scope or e["to"] not in self.scope
            ):
                continue
            a, b = e["from"], e["to"]
            if e["kind"] in ("service_provide", "action_provide"):
                a, b = b, a  # draw request direction: client -> service -> server
            ed = _plain_edge(a, b, "", e["kind"])
            ed["model_edges"] = [e["id"]]
            ed["evidence_status"] = e.get("evidence_status", "unknown")
            ed["async"] = e["kind"] in ("publishes", "subscribes")
            ed["label"] = "served by" if e["kind"] in ("service_provide", "action_provide") else ""
            pairs.append(ed)
        edges = _dedupe(pairs)
        if not edges:
            self.skip("ros2", "ROS nodes have no topic/service/action connections", "ros2-graph")
        else:
            self._ros2_graph(edges)
        self._ros2_tf()

    def _ros2_graph(self, edges: list[dict[str, Any]]) -> None:
        v = self.v
        units = sorted({x for e in edges for x in (e["from"], e["to"])})
        rosnodes = [u for u in units if v.kind_of(u) == "ros_node"]
        by_pkg: dict[str, list[str]] = defaultdict(list)
        for u in rosnodes:
            by_pkg[v.nodes[u].get("subsystem") or ""].append(u)
        parts = [rosnodes]
        if len(units) > self.max_nodes:
            ifaces_of = {
                n: {
                    x
                    for e in edges
                    for x in (e["from"], e["to"])
                    if n in (e["from"], e["to"]) and x != n
                }
                for n in rosnodes
            }
            parts, cur = [], []
            for pkg in sorted(by_pkg):
                for n in by_pkg[pkg]:
                    trial = cur + [n]
                    size = len(trial) + len(set().union(*(ifaces_of[m] for m in trial)))
                    if cur and size > self.max_nodes:
                        parts.append(cur)
                        cur = [n]
                    else:
                        cur = trial
            if cur:
                parts.append(cur)
        group_of = {}
        for u in rosnodes:
            sid = v.nodes[u].get("subsystem")
            if sid:
                group_of[u] = ("g-" + slug(sid), v.label(sid), "app")
        for k, part in enumerate(parts, start=1):
            pset = set(part)
            pedges = [e for e in edges if e["from"] in pset or e["to"] in pset]
            punits = sorted({x for e in pedges for x in (e["from"], e["to"])} | pset)
            notes = []
            if len(parts) > 1:
                notes.append(
                    f"ROS graph split into {len(parts)} parts by package; shared interfaces repeat across parts."
                )
            punits, pedges, fit_notes = self._fit(punits, pedges, pinned=pset)
            did = "ros2-graph" if len(parts) == 1 else f"ros2-graph-part-{k}"
            title = "ROS 2 node graph" + ("" if len(parts) == 1 else f" (part {k} of {len(parts)})")
            spec = self._flowchart(
                did=did, typ="ros2", title=title,
                purpose="Which ROS 2 nodes publish, subscribe, call and serve which topics, services and actions.",
                level="symbol", units=punits, edges=pedges, notes=notes + fit_notes, group_of=group_of,
            )  # fmt: skip
            dup = self.covered_by(spec, any_level=True) if len(parts) == 1 else None
            if dup:
                self.skip("ros2", f"every node and connection is already drawn in '{dup}'", did)
                continue
            self.add(spec, "ros2-graph", k)

    def _ros2_tf(self) -> None:
        v = self.v
        units, edges = v.aggregate("symbol", kinds={"tf_transform"}, scope=self.scope)
        if len(units) < 2:
            self.skip("ros2", "fewer than 2 connected TF frames", "ros2-tf")
            return
        units, edges, notes = self._fit(units, edges)
        spec = self._flowchart(
            did="ros2-tf", typ="ros2", title="TF frame tree",
            purpose="How coordinate frames are connected and which node broadcasts each transform.",
            level="symbol", units=units, edges=edges, notes=notes, direction="TD",
        )  # fmt: skip
        self.add(spec, "ros2-tf")

    # ================================================================ coverage
    def _coverage(self, diagrams: list[dict[str, Any]]) -> dict[str, Any]:
        v = self.v
        total = sorted(v.nodes)
        master = set()
        detail = set()
        drawn = set()
        for d in diagrams:
            ids = set(d.get("model_nodes", []))
            (master if d["type"] == "master" else detail).update(ids)
            drawn.update(n["id"] for n in d.get("nodes", []) if n["id"] in v.nodes)
        shown = master | detail
        only = [n for n in total if n not in shown]
        n = len(total)
        return {
            "definition": (
                "A model node counts as shown when it is drawn itself or folded into a drawn "
                "unit (its module or subsystem)."
            ),
            "model_nodes": n,
            "master": {"count": len(master & set(total)), "of": n},
            "detail": {"count": len(detail & set(total)), "of": n},
            "any_diagram": {"count": len(shown & set(total)), "of": n},
            "drawn_individually": {"count": len(drawn), "of": n},
            "only_in_model": {
                "count": len(only),
                "of": n,
                "by_kind": dict(sorted(Counter(v.kind_of(x) for x in only).items())),
                "ids": only,
            },
        }


# ==================================================================== helpers


def _base(did, typ, title, purpose, renderer, direction, level) -> dict[str, Any]:
    return {
        "id": did,
        "title": title,
        "type": typ,
        "purpose": purpose,
        "renderer": renderer,
        "direction": direction,
        "level": level,
        "row": ROW[typ],
        "priority": 0,
        "groups": [],
        "nodes": [],
        "edges": [],
        "notes": [],
        "model_nodes": [],
        "content_hash": "",
    }


def _edge_entry(e: dict[str, Any]) -> dict[str, Any]:
    imports = bool(e.get("import_only"))
    return {
        "from": e["from"],
        "to": e["to"],
        "label": "imports" if imports else (e.get("label") or ""),
        "style": "dotted" if imports or e.get("async") else "solid",
        "end": "cross" if e.get("error") else "arrow",
        "bidir": False,
        "model_edges": sorted(e.get("model_edges", [])),
        "evidence_status": e.get("evidence_status", "unknown"),
    }


def _plain_edge(a: str, b: str, label: str, kind: str) -> dict[str, Any]:
    return {
        "from": a, "to": b, "kind": kind, "label": label, "model_edges": [],
        "evidence_status": "unknown", "import_only": False, "weight": 1,
        "async": kind in ("publishes", "subscribes", "spawns", "triggers", "external_call", "async", "event"),
        "error": kind in ("error_path", "error"),
    }  # fmt: skip


def _dedupe(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge edges between the same pair: labels joined, model edges unioned."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for e in edges:
        key = (e["from"], e["to"])
        if key not in merged:
            merged[key] = dict(e, model_edges=list(e.get("model_edges", [])))
            continue
        m = merged[key]
        if e.get("label") and e["label"] not in m["label"].split(" / "):
            m["label"] = f"{m['label']} / {e['label']}" if m["label"] else e["label"]
        m["model_edges"] = sorted(set(m["model_edges"]) | set(e.get("model_edges", [])))
        m["weight"] = m.get("weight", 1) + e.get("weight", 1)
        m["error"] = m.get("error") or e.get("error")
        m["async"] = m.get("async") and e.get("async")
    return list(merged.values())


_STRENGTH = ("confirmed", "dynamic_observed", "static_inferred", "documented_only", "unknown")


def _weaker(a: str, b: str) -> str:
    ia = _STRENGTH.index(a) if a in _STRENGTH else 4
    ib = _STRENGTH.index(b) if b in _STRENGTH else 4
    return _STRENGTH[max(ia, ib)]


def _collapse_channels(
    v: ModelView, chans: list[str], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cset = set(chans)
    kept = [e for e in edges if e["from"] not in cset and e["to"] not in cset]
    new = []
    for c in sorted(chans):
        producers = [e for e in edges if e["to"] == c and e["from"] not in cset]
        consumers = [e for e in edges if e["from"] == c and e["to"] not in cset]
        for p in producers:
            for q in consumers:
                if p["from"] == q["to"]:
                    continue
                ed = _plain_edge(p["from"], q["to"], v.label(c), "publishes")
                ed["model_edges"] = sorted(
                    set(p.get("model_edges", [])) | set(q.get("model_edges", []))
                )
                ed["evidence_status"] = _weaker(
                    p.get("evidence_status", "unknown"), q.get("evidence_status", "unknown")
                )
                new.append(ed)
    return _dedupe(kept + new)


def _groups(units, group_of, min_group):
    """Create group entries for groups with at least `min_group` members."""
    counts = Counter(group_of[u][0] for u in units if u in group_of)
    meta = {}
    for u in units:
        if u in group_of:
            gid, lab, cat = group_of[u]
            meta.setdefault(gid, (lab, cat))
    keep = {g for g, c in counts.items() if c >= min_group}
    groups = [
        {"id": g, "label": meta[g][0], "parent": None, "category": meta[g][1]} for g in sorted(keep)
    ]
    assign = {u: group_of[u][0] for u in units if u in group_of and group_of[u][0] in keep}
    return groups, assign


def _phase_groups(phase_of: dict[str, str], units: list[str]) -> dict[str, tuple[str, str, str]]:
    phases = {phase_of.get(u, "unknown") for u in units}
    if not {"init", "runtime"} <= phases:
        return {}
    names = {
        "init": ("g-init", "Initialization", "app"),
        "runtime": ("g-runtime", "Runtime", "app"),
    }
    return {u: names[phase_of[u]] for u in units if phase_of.get(u) in names}


def _names(v: ModelView, ids, limit: int = 12) -> str:
    labels = sorted(v.label(i) for i in ids)
    text = ", ".join(labels[:limit])
    if len(labels) > limit:
        text += f" and {len(labels) - limit} more (see model.json)"
    return text


def _long_label_notes(labels) -> list[str]:
    long = sorted({lab for lab in labels if len(lab) > LABEL_MAX})
    return [f"Label longer than {LABEL_MAX} characters kept in full: {lab}" for lab in long]


def _common_dir(v: ModelView, modules: list[str]) -> str:
    dirs = sorted({v.file_of(m).rsplit("/", 1)[0] for m in modules if "/" in v.file_of(m)})
    return dirs[0] if len(dirs) == 1 else ""


def _sid_slug(sid: str) -> str:
    return slug(sid.split(":", 1)[-1])


def _flow_slug(fid: str) -> str:
    s = slug(fid)
    return s[5:] if s.startswith("flow-") and len(s) > 5 else s


def _algo_slug(aid: str) -> str:
    s = slug(aid)
    return s[5:] if s.startswith("algo-") and len(s) > 5 else s
