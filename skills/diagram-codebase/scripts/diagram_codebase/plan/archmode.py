"""Eligibility check for Figma's architecture layout (`useArchitectureLayoutCode`).

The architecture renderer has hard rules (see the official figma-generate-diagram
skill, references/architecture.md): six fixed lanes, a DAG of forward edges,
dotted edges for async/external, and a fixed table of allowed lane pairs. We
only use it when the master diagram satisfies every rule; otherwise the planner
falls back to an ordinary `flowchart LR` with subgraphs.
"""

from __future__ import annotations

from typing import Any

from ..mermaid.lint import ARCH_DOTTED, ARCH_FORWARD, LANES
from .graph import is_dag
from .view import ModelView

# Single source of truth: the lane tables mermaid/lint.py enforces.
ALLOWED = ARCH_FORWARD | ARCH_DOTTED
__all__ = ["ALLOWED", "LANES", "check", "lane_of"]
GATEWAY_HINTS = (
    "nginx",
    "gateway",
    "traefik",
    "envoy",
    "haproxy",
    "proxy",
    "ingress",
    "load-balancer",
    "loadbalancer",
)


def lane_of(view: ModelView, uid: str) -> str:
    kind = view.kind_of(uid)
    if kind == "subsystem":
        return "client" if view.subsystem_is_ui(uid) else "service"
    if kind in ("datastore", "db_entity"):
        return "datastore"
    if kind in ("external_service", "library"):
        return "external"
    if kind in ("event_channel", "topic"):
        return "async"
    if kind == "ui_component":
        return "client"
    if kind == "deployment_unit" and any(h in view.label(uid).lower() for h in GATEWAY_HINTS):
        return "gateway"
    if kind == "module" and "ui" in view.nodes.get(uid, {}).get("tags", []):
        return "client"
    return "service"


def check(
    view: ModelView, units: list[str], edges: list[dict[str, Any]], max_edges: int
) -> tuple[bool, dict[str, str], list[str]]:
    """Return (eligible, lane per unit, reasons it is not eligible)."""
    reasons: list[str] = []
    lanes = {u: lane_of(view, u) for u in units}
    if len(edges) > max_edges:
        reasons.append(f"{len(edges)} edges > {max_edges}")
    seen_pairs: set[frozenset[str]] = set()
    directed: set[tuple[str, str]] = set()
    forward = []
    for e in edges:
        pair = (lanes[e["from"]], lanes[e["to"]])
        if pair not in ALLOWED:
            reasons.append(
                f"edge {view.label(e['from'])} -> {view.label(e['to'])} is {pair[0]}->{pair[1]}, not an allowed lane pair"
            )
        key = frozenset((e["from"], e["to"]))
        if key in seen_pairs:
            # A unit that both produces to and consumes from a channel draws two dotted
            # edges in opposite directions; Figma's rules allow that pair.
            opposite = (e["to"], e["from"]) in directed and "async" in pair
            if not opposite:
                reasons.append(
                    f"two edges between {view.label(e['from'])} and {view.label(e['to'])}"
                )
        seen_pairs.add(key)
        directed.add((e["from"], e["to"]))
        if "async" not in pair and "external" not in pair:
            forward.append((e["from"], e["to"]))
    if not is_dag(units, forward):
        reasons.append("forward edges contain a cycle")
    if not any(lane == "service" for lane in lanes.values()):
        reasons.append("no service lane")
    return (not reasons), lanes, reasons
