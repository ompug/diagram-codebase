"""Deterministic repository scan: inventory + structural findings.

Output files (in the run's output directory):
  inventory.json        files, languages, manifests, git info, skipped/excluded counts
  findings/scan.json    model fragment (subsystems/nodes/edges/evidence) in the same
                        format Claude's findings use, with source=ast|regex|manifest
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..common import slug, utc_now, write_json
from ..model.builder import ModelBuilder
from .gitinfo import git_info
from .inventory import build_inventory
from .lang_cpp import analyze_cpp
from .lang_js import analyze_js
from .lang_python import analyze_python
from .manifests import parse_manifests
from .patterns_py import PyPatterns
from .ros2 import Ros2Extractor

CONTAINER_DIRS = {
    "src",
    "lib",
    "source",
    "sources",
    "pkg",
    "internal",
    "include",
    "packages",
    "apps",
    "services",
    "modules",
    "crates",
}


def run_scan(root: Path, out_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    inv = build_inventory(root, config)
    git = git_info(root)
    manifests = parse_manifests(root, inv["files"])
    b = ModelBuilder()

    idx = analyze_python(root, inv["files"], b)
    PyPatterns(idx, b).run()
    cpp = analyze_cpp(root, inv["files"], b)
    js = analyze_js(root, inv["files"], b)
    ros = Ros2Extractor(root, b)
    ros.from_python(idx)
    ros.from_cpp(inv["files"])
    ros.from_launch(inv["files"], manifests, idx)
    if ros.found and "ros2" not in manifests["frameworks"]:
        manifests["frameworks"].append("ros2")

    _deployments(b, manifests)
    _entrypoints(b, manifests)
    assign_subsystems(b, inv, manifests)

    external = sorted(
        {m for pf in idx.files.values() for m in pf.external_imports}
        | set(js["external_packages"])
        | set(cpp["system_includes"])
    )
    parse_errors = [{"file": pf.path, "error": pf.error} for pf in idx.files.values() if pf.error]
    inventory = {
        "generated_at": utc_now(),
        "repo": {"name": root.name, "git": git},
        **inv,
        "manifests": manifests,
        "external_imports": external[:300],
        "parse_errors": parse_errors,
    }
    fragment = b.to_fragment()
    fragment["source"] = "scan"
    fragment["conflicts"] = b.conflicts
    write_json(out_dir / "inventory.json", inventory)
    write_json(out_dir / "findings" / "scan.json", fragment)
    return {
        "inventory": inventory,
        "fragment": fragment,
        "brief": analysis_brief(inventory, fragment, config),
    }


def _deployments(b: ModelBuilder, manifests: dict[str, Any]) -> None:
    names = {d["name"] for d in manifests["deployments"]}
    for d in manifests["deployments"]:
        ev = b.ev(
            d["file"], d["line"], d["line"], symbol=d["name"], detail=d["kind"], source="manifest"
        )
        nid = f"deploy:{d['name']}"
        image = d.get("image") or ""
        kind = "deployment_unit"
        store = next(
            (
                s
                for s in (
                    "postgres",
                    "mysql",
                    "mongo",
                    "redis",
                    "rabbitmq",
                    "kafka",
                    "elasticsearch",
                    "minio",
                )
                if s in image.lower()
            ),
            None,
        )
        if store in ("rabbitmq", "kafka"):
            kind = "event_channel"
        elif store:
            kind = "datastore"
        b.node(
            nid,
            d["name"],
            kind,
            evidence_ids=[ev],
            image=image or None,
            ports=d.get("ports", []),
            deploy_kind=d["kind"],
            command=d.get("command"),
        )
        for dep in d.get("depends_on", []):
            if dep in names:
                b.edge(
                    nid,
                    f"deploy:{dep}",
                    "depends_on",
                    evidence_ids=[ev],
                    label="depends on",
                    phase="build",
                )


def _entrypoints(b: ModelBuilder, manifests: dict[str, Any]) -> None:
    for ep in manifests["entry_points"]:
        target = ep["target"]
        cand = None
        if ep["kind"] == "console_script" and ":" in target:
            mod, func = target.split(":", 1)
            mod_path = mod.replace(".", "/")
            cand = next(
                (
                    nid
                    for nid in b.nodes
                    if nid.startswith("fn:") and nid.endswith(f":{func}") and mod_path in nid
                ),
                None,
            )
        elif ep["kind"] in ("node_main", "node_bin"):
            cand = f"mod:{target}" if f"mod:{target}" in b.nodes else None
        elif ep["kind"] == "cmake_executable":
            for src in target.split():
                if f"fn:{src}:main" in b.nodes:
                    cand = f"fn:{src}:main"
        if cand and cand in b.nodes:
            n = b.nodes[cand]
            n["tags"] = sorted(set(n["tags"]) | {"entrypoint"})
            n["metadata"].setdefault("entry_point_names", []).append(ep["name"])
            ev = b.ev(
                ep["file"],
                ep["line"],
                ep["line"],
                symbol=ep["name"],
                detail=f"entry point ({ep['kind']})",
                source="manifest",
            )
            n["evidence_ids"].append(ev)


def _node_file(b: ModelBuilder, n: dict[str, Any]) -> str | None:
    f = n.get("metadata", {}).get("file")
    if f:
        return f
    for eid in n.get("evidence_ids", []):
        ev = b.evidence.get(eid)
        if ev:
            return ev["file"]
    return None


def _subsystem_key(path: str, strip_depth: int, ros_pkgs: list[tuple[str, str]]) -> tuple[str, str]:
    for pkg_path, pkg_name in ros_pkgs:
        if pkg_path not in (".", "") and (path == pkg_path or path.startswith(pkg_path + "/")):
            return pkg_path, pkg_name
    parts = path.split("/")[:-1]
    i = 0
    while i < len(parts) - 1 and parts[i] in CONTAINER_DIRS:
        i += 1
    if i >= len(parts):
        return "", ""
    i = min(i + strip_depth, len(parts) - 1)
    return "/".join(parts[: i + 1]), parts[i]


def assign_subsystems(b: ModelBuilder, inv: dict[str, Any], manifests: dict[str, Any]) -> None:
    src_files = [f["path"] for f in inv["files"] if f["analyzed"]]
    ros_pkgs = sorted(
        ((p["path"], p["name"]) for p in manifests["packages"] if p["kind"] == "ros2_package"),
        key=lambda x: -len(x[0]),
    )
    # Descend while one directory dominates (e.g. a single top-level Python package).
    depth = 0
    for depth in range(3):
        keys = Counter(_subsystem_key(p, depth, ros_pkgs)[0] for p in src_files)
        keys.pop("", None)
        if not keys:
            break
        top, count = keys.most_common(1)[0]
        if (
            count / max(sum(keys.values()), 1) < 0.8
            or len(
                {
                    _subsystem_key(p, depth + 1, ros_pkgs)[0]
                    for p in src_files
                    if p.startswith(top + "/")
                }
            )
            <= 1
        ):
            break
    file_sub: dict[str, str] = {}
    for p in src_files:
        key, name = _subsystem_key(p, depth, ros_pkgs)
        if not key:
            key, name = ".", inv.get("root_name") or "app"
            if name in ("", "."):
                name = "app"
        sid = f"sub:{slug(key if key != '.' else 'root')}"
        b.subsystem(
            sid, name.replace("_", " ").replace("-", " ").strip().title() or "App", paths=[key]
        )
        file_sub[p] = sid
    for n in b.nodes.values():
        if n.get("subsystem"):
            continue
        f = _node_file(b, n)
        if f in file_sub and n["kind"] not in (
            "topic",
            "event_channel",
            "datastore",
            "external_service",
            "tf_frame",
            "db_entity",
            "ros_service",
            "ros_action",
            "deployment_unit",
        ):
            n["subsystem"] = file_sub[f]
    # Endpoints and parameters inherit from what handles/owns them.
    owners: dict[str, Counter] = defaultdict(Counter)
    for e in b.edges.values():
        src, dst = b.nodes.get(e["from"]), b.nodes.get(e["to"])
        if not src or not dst:
            continue
        if e["kind"] == "handles" and dst.get("subsystem"):
            owners[src["id"]][dst["subsystem"]] += 1
        if e["kind"] == "config_dependency" and src.get("subsystem"):
            owners[dst["id"]][src["subsystem"]] += 1
    for nid, counts in owners.items():
        if not b.nodes[nid].get("subsystem") and len(counts) == 1:
            b.nodes[nid]["subsystem"] = next(iter(counts))


def analysis_brief(
    inventory: dict[str, Any], fragment: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Compact summary to orient Claude's analysis (printed by `dc.py scan`)."""
    nodes = fragment["nodes"]
    degree: Counter = Counter()
    for e in fragment["edges"]:
        if e["kind"] != "imports":
            degree[e["from"]] += 1
            degree[e["to"]] += 1
    by_sub: Counter = Counter(n.get("subsystem") for n in nodes if n["kind"] == "module")
    entry = [n["id"] for n in nodes if "entrypoint" in n.get("tags", [])]
    threshold = config.get("analysis", {}).get("parallel_file_threshold", 150)
    return {
        "repo": inventory["repo"]["name"],
        "revision": inventory["repo"]["git"].get("short"),
        "dirty": inventory["repo"]["git"].get("dirty"),
        "languages": inventory["languages"],
        "unsupported_languages": inventory["unsupported_languages"],
        "frameworks": inventory["manifests"]["frameworks"],
        "build_systems": inventory["manifests"]["build_systems"],
        "source_files": inventory["source_file_count"],
        "analyzed_files": inventory["analyzed_file_count"],
        "subsystems": [
            {
                "id": s["id"],
                "name": s["name"],
                "paths": s["paths"],
                "modules": by_sub.get(s["id"], 0),
            }
            for s in fragment["subsystems"]
        ],
        "entry_points": entry[:30],
        "hubs": [nid for nid, _ in degree.most_common(15)],
        "node_kinds": dict(Counter(n["kind"] for n in nodes)),
        "edge_kinds": dict(Counter(e["kind"] for e in fragment["edges"])),
        "parse_errors": len(inventory["parse_errors"]),
        "use_parallel_agents": inventory["source_file_count"] > threshold,
        "parallel_threshold": threshold,
    }
