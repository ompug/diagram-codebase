"""`--update` impact analysis: what changed since the last published revision.

Reads manifest.json (previous revision), model.json, inventory.json, plan.json and
findings/*.json from the output directory and runs read-only `git diff` via
scan.gitinfo. Recommends `full` (re-run the whole analysis), `partial`
(re-analyze affected areas, regenerate affected diagrams) or `none`. Affected
findings areas are named by slug (`execution`, `dataflow`, `sub-<slug>`, ...).
"""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..common import read_json
from ..scan import gitinfo
from . import manifest as mf

BUILD_PATTERNS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements*.txt",
    "Pipfile",
    "poetry.lock",
    "package.json",
    "pnpm-workspace.yaml",
    "tsconfig*.json",
    "CMakeLists.txt",
    "*.cmake",
    "Makefile",
    "package.xml",
    "*.launch.py",
    "*.launch.xml",
    "*.launch",
    "Cargo.toml",
    "go.mod",
    "Dockerfile",
    "*.dockerfile",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "compose*.yaml",
)


def is_build_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pat) for pat in BUILD_PATTERNS)


def _read(path: Path) -> Any:
    return read_json(path, default={}) if path.is_file() else {}


def _cited_files(value: Any) -> set[str]:
    """Every `file` cited anywhere in a findings document (inline evidence)."""
    out: set[str] = set()
    stack = [value]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            f = cur.get("file")
            if isinstance(f, str):
                out.add(f)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def _under(path: str, prefixes: list[str]) -> bool:
    return any(p and (path == p or path.startswith(p.rstrip("/") + "/")) for p in prefixes)


def affected(out_dir: Path | str, root: Path | str, config: dict[str, Any]) -> dict[str, Any]:
    out_dir, root = Path(out_dir), Path(root)
    m = mf.load(out_dir)
    since = (m or {}).get("previous_revision") or (m or {}).get("revision")
    base: dict[str, Any] = {"since": since, "recommendation": "full"}
    if m is None or not since:
        return {**base, "reason": "no previous run revision recorded; run a full analysis"}
    changes = gitinfo.changed_files(root, since)
    if changes is None:
        return {
            **base,
            "reason": f"git cannot diff against {since} (not a git repo or history "
            "rewritten); run a full analysis",
        }
    out_rel = _rel_out(out_dir, root)
    changed = {
        k: sorted(p for p in v if not (out_rel and _under(p, [out_rel])))
        for k, v in changes.items()
    }
    touched = set(changed["added"]) | set(changed["modified"]) | set(changed["deleted"])

    model = _read(out_dir / "model.json")
    evidence_file = {e["id"]: e.get("file") for e in model.get("evidence") or [] if "id" in e}

    def touches(item: dict[str, Any]) -> bool:
        return any(evidence_file.get(eid) in touched for eid in item.get("evidence_ids") or [])

    direct: set[str] = set()
    for n in model.get("nodes") or []:
        nid = n.get("id", "")
        if touches(n) or (nid.startswith("mod:") and nid[4:] in touched):
            direct.add(nid)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for e in model.get("edges") or []:
        a, b = e.get("from"), e.get("to")
        if not a or not b:
            continue
        adjacency[a].add(b)
        adjacency[b].add(a)
        if touches(e):
            direct.update((a, b))
    hop = {nb for nid in direct for nb in adjacency.get(nid, ())} - direct

    node_sub = {n["id"]: n.get("subsystem") for n in model.get("nodes") or [] if "id" in n}
    subsystems = {node_sub.get(nid) for nid in direct} - {None}
    for s in model.get("subsystems") or []:
        if any(_under(p, s.get("paths") or []) for p in touched):
            subsystems.add(s["id"])

    findings_dir = out_dir / "findings"
    stale_findings = []
    if findings_dir.is_dir():
        for path in sorted(findings_dir.glob("*.json")):
            if path.name == "scan.json":
                continue
            cited = _cited_files(_read(path))
            hits = sorted(cited & touched)
            if hits:
                stale_findings.append({"file": f"findings/{path.name}", "cites": hits[:20]})

    affected_nodes = direct | hop
    plan = _read(out_dir / "plan.json")
    diagrams = sorted(
        d["id"]
        for d in plan.get("diagrams") or []
        if affected_nodes & set(d.get("model_nodes") or [])
    )

    inventory = _read(out_dir / "inventory.json")
    total = len(inventory.get("files") or []) or 1
    churn = round(len(touched) / total, 3)
    build = sorted(p for p in touched if is_build_file(p))
    threshold = float(config["update"]["full_reanalysis_churn"])

    if not touched:
        rec, reason = "none", "no files changed since the last run"
    elif churn >= threshold:
        rec, reason = "full", f"churn {churn:.0%} >= {threshold:.0%} of inventoried files"
    elif not model:
        rec, reason = "full", "model.json is missing"
    else:
        rec, reason = "partial", f"{len(touched)} file(s) changed ({churn:.0%} churn)"
        if build:
            reason += "; build/manifest files changed, re-run the scan before merging"
    return {
        "since": since,
        "changed": changed,
        "changed_count": len(touched),
        "affected_nodes": sorted(direct),
        "neighbor_nodes": sorted(hop),
        "affected_subsystems": sorted(subsystems),
        "findings_to_reanalyze": stale_findings,
        "areas": _areas(stale_findings, subsystems),
        "affected_diagrams": diagrams,
        "build_files_changed": build,
        "churn": churn,
        "recommendation": rec,
        "reason": reason,
    }


def _areas(stale_findings: list[dict[str, Any]], subsystems: set[str]) -> list[str]:
    """Findings area slugs to re-analyze: stale findings files plus touched subsystems."""
    areas = {Path(f["file"]).stem for f in stale_findings}
    areas |= {"sub-" + sid.split(":", 1)[-1] for sid in subsystems}
    return sorted(areas)


def _rel_out(out_dir: Path, root: Path) -> str | None:
    try:
        return out_dir.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
