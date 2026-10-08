"""Merge scan output and Claude/Explore findings into one validated model.

Claude findings use inline evidence (file + lines + symbol + status). Every
piece of evidence is checked against the repository before it is accepted:
the file must exist, the lines must exist, and a cited symbol must appear in
or near the cited lines. Items left without evidence are rejected, so a
hallucinated relationship cannot reach a diagram. `documented_only` items may
cite documentation files instead of code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..common import read_json, utc_now
from . import schema
from .builder import ModelBuilder

DOC_EXTS = (".md", ".rst", ".txt", ".adoc")
SYMBOL_WINDOW = 3


class EvidenceChecker:
    def __init__(self, root: Path, known_files: set[str]) -> None:
        self.root = root
        self.known = known_files
        self._lines: dict[str, list[str]] = {}

    def lines(self, rel: str) -> list[str] | None:
        if rel not in self._lines:
            path = (self.root / rel).resolve()
            try:
                path.relative_to(self.root.resolve())
            except ValueError:
                return None  # outside the repository
            try:
                self._lines[rel] = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return None
        return self._lines[rel]

    def normalize(self, raw: str) -> str | None:
        """Repo-relative posix path; strips a leading `./` (not dots of `.github/`)."""
        raw = raw.strip().replace("\\", "/")
        if raw.startswith("/"):
            try:
                return Path(raw).resolve().relative_to(self.root.resolve()).as_posix()
            except ValueError:
                return None
        while raw.startswith("./"):
            raw = raw[2:]
        return raw

    def check(self, ev: dict[str, Any]) -> str | None:
        """Return None if acceptable, else a rejection reason."""
        rel = self.normalize(str(ev.get("file") or ""))
        if rel is None:
            return f"file outside the repository: {ev.get('file')}"
        ev["file"] = rel
        if not rel:
            return "missing file"
        is_doc = rel.lower().endswith(DOC_EXTS)
        if rel not in self.known and not is_doc:
            return f"file not in analyzed inventory: {rel}"
        lines = self.lines(rel)
        if lines is None:
            return f"file not readable: {rel}"
        start = ev.get("line_start")
        end = ev.get("line_end") or start
        if start is None:
            return None if ev.get("status") == "documented_only" else "missing line numbers"
        if (
            not (isinstance(start, int) and isinstance(end, int))
            or start < 1
            or end < start
            or end > len(lines)
        ):
            return f"lines {start}-{end} out of range (file has {len(lines)} lines)"
        sym = (ev.get("symbol") or "").strip()
        if sym:
            window = "\n".join(lines[max(0, start - 1 - SYMBOL_WINDOW) : end + SYMBOL_WINDOW])
            tokens = [t for t in re.split(r"[.:/()\s]+|->", sym) if t]
            needle = tokens[-1] if tokens else sym
            if needle not in window:
                return f"symbol '{sym}' not found near {rel}:{start}-{end}"
        return None


def _parse_lines(ev: dict[str, Any]) -> None:
    if "lines" in ev and "line_start" not in ev:
        raw = str(ev.pop("lines"))
        m = re.match(r"\s*(\d+)\s*(?:[-:]\s*(\d+))?", raw)
        if m:
            ev["line_start"] = int(m.group(1))
            ev["line_end"] = int(m.group(2) or m.group(1))
    if "line" in ev and "line_start" not in ev:
        ev["line_start"] = ev["line_end"] = ev.pop("line")


class Merger:
    def __init__(self, root: Path, inventory: dict[str, Any]) -> None:
        self.root = root
        self.inventory = inventory
        self.b = ModelBuilder()
        self.checker = EvidenceChecker(root, {f["path"] for f in inventory["files"]})
        self.rejected: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.uncertainties: list[str] = []
        self.flows: dict[str, dict] = {}
        self.dataflows: dict[str, dict] = {}
        self.algorithms: dict[str, dict] = {}
        self.state_machines: dict[str, dict] = {}
        self.erd = {"entities": {}, "relations": []}
        self.sources: list[str] = []

    # ---------------------------------------------------------------- scan
    def add_scan(self, frag: dict[str, Any]) -> None:
        self.sources.append("scan")
        for e in frag.get("evidence", []):
            self.b.add_evidence_record(e)
        for s in frag.get("subsystems", []):
            self.b.subsystem(
                s["id"], s["name"], description=s.get("description", ""), paths=s.get("paths", [])
            )
        for n in frag.get("nodes", []):
            self.b.add_node(n)
        for e in frag.get("edges", []):
            self.b.add_edge(e)
        self.warnings.extend(f"scan conflict: {c}" for c in frag.get("conflicts", []))

    # ---------------------------------------------------------------- findings
    def _evidence_ids(
        self, items: list[dict[str, Any]] | None, where: str, kind: str = ""
    ) -> list[str]:
        ids: list[str] = []
        for raw in items or []:
            ev = dict(raw)
            _parse_lines(ev)
            status = ev.get("status", "static_inferred")
            if status not in schema.EVIDENCE_STATUSES:
                self.rejected.append(
                    {"where": where, "reason": f"invalid status {status!r}", "evidence": raw}
                )
                continue
            if status == "dynamic_observed" and not ev.get("detail"):
                self.rejected.append(
                    {
                        "where": where,
                        "reason": "dynamic_observed needs a detail describing the observation",
                        "evidence": raw,
                    }
                )
                continue
            reason = self.checker.check(ev)
            if reason:
                self.rejected.append({"where": where, "reason": reason, "evidence": raw})
                continue
            is_doc = ev["file"].lower().endswith(DOC_EXTS)
            if is_doc and status != "documented_only":
                status = "documented_only"
            if kind == "calls" and status == "confirmed" and not ev.get("symbol"):
                status = "static_inferred"
            ids.append(
                self.b.ev(
                    ev["file"],
                    ev.get("line_start"),
                    ev.get("line_end"),
                    symbol=ev.get("symbol", ""),
                    detail=str(ev.get("detail", ""))[:200],
                    source="doc" if is_doc else "claude",
                    status=status,
                )  # fmt: skip
            )
        return ids

    def add_findings(self, name: str, f: dict[str, Any]) -> None:
        self.sources.append(name)
        for s in f.get("subsystems", []) or []:
            if not s.get("id") or not s.get("name"):
                self.warnings.append(f"{name}: subsystem without id/name ignored")
                continue
            sid = s["id"] if s["id"].startswith("sub:") else f"sub:{s['id']}"
            cur = self.b.subsystems.get(sid)
            if cur is None:
                self.b.subsystem(
                    sid, s["name"], description=s.get("description", ""), paths=s.get("paths", [])
                )
            else:
                cur["name"] = s["name"]
                if s.get("description"):
                    cur["description"] = s["description"]
                for p in s.get("paths", []) or []:
                    if p not in cur["paths"]:
                        cur["paths"].append(p)
        for raw in f.get("nodes", []) or []:
            self._add_node(name, raw)
        for raw in f.get("edges", []) or []:
            self._add_edge(name, raw)
        for coll, store in (
            ("flows", self.flows),
            ("dataflows", self.dataflows),
            ("algorithms", self.algorithms),
            ("state_machines", self.state_machines),
        ):
            for raw in f.get(coll, []) or []:
                self._add_structure(name, coll, raw, store)
        erd = f.get("erd") or {}
        for ent in erd.get("entities", []) or []:
            ids = self._evidence_ids(ent.get("evidence"), f"{name} erd entity {ent.get('id')}")
            if ids:
                self.erd["entities"][ent["id"]] = {
                    **{k: v for k, v in ent.items() if k != "evidence"},
                    "evidence_ids": ids,
                }
            else:
                self.rejected.append(
                    {
                        "where": f"{name} erd entity {ent.get('id')}",
                        "reason": "no accepted evidence",
                    }
                )
        for rel in erd.get("relations", []) or []:
            self.erd["relations"].append({k: v for k, v in rel.items() if k != "evidence"})
        self.uncertainties.extend(str(u) for u in f.get("uncertainties", []) or [])

    def _add_node(self, src: str, raw: dict[str, Any]) -> None:
        nid = raw.get("id")
        where = f"{src} node {nid}"
        if not nid or not isinstance(nid, str):
            self.rejected.append({"where": where, "reason": "missing id"})
            return
        existing = self.b.nodes.get(nid)
        ev_ids = self._evidence_ids(raw.get("evidence"), where)
        if existing is None:
            if raw.get("kind") not in schema.NODE_KINDS:
                self.rejected.append(
                    {"where": where, "reason": f"invalid kind {raw.get('kind')!r}"}
                )
                return
            if not ev_ids:
                self.rejected.append(
                    {"where": where, "reason": "new node without accepted evidence"}
                )
                return
            if not raw.get("name"):
                self.rejected.append({"where": where, "reason": "missing name"})
                return
        sub = raw.get("subsystem")
        if sub is not None and not isinstance(sub, str):
            self.warnings.append(f"{where}: non-string subsystem ignored")
            sub = None
        if sub and not sub.startswith("sub:"):
            sub = f"sub:{sub}"
        if existing is not None and raw.get("kind") and raw["kind"] != existing["kind"]:
            self.warnings.append(
                f"conflict: {src} says {nid} is {raw['kind']!r}, scan says {existing['kind']!r} (kept scan)"
            )
        if (
            existing is not None
            and sub
            and existing.get("subsystem")
            and sub != existing["subsystem"]
        ):
            self.warnings.append(f"info: {src} moved {nid} from {existing['subsystem']} to {sub}")
        n = schema.node(
            nid, raw.get("name") or (existing or {}).get("name", nid), (existing or raw)["kind"],
            subsystem=sub, description=str(raw.get("description", ""))[:500], parent=raw.get("parent"),
            evidence_ids=ev_ids, tags=raw.get("tags") or [], symbols=raw.get("symbols") or [],
            inputs=raw.get("inputs") or [], outputs=raw.get("outputs") or [],
        )  # fmt: skip
        n["metadata"].update(raw.get("metadata") or {})
        self.b.add_node(n, override=True)

    def _add_edge(self, src: str, raw: dict[str, Any]) -> None:
        kind = raw.get("kind")
        where = f"{src} edge {raw.get('from')} -{kind}-> {raw.get('to')}"
        if kind not in schema.EDGE_KINDS:
            self.rejected.append({"where": where, "reason": f"invalid kind {kind!r}"})
            return
        if not all(isinstance(raw.get(k), str) and raw.get(k) for k in ("from", "to")):
            self.rejected.append({"where": where, "reason": "edge needs string 'from' and 'to'"})
            return
        ev_ids = self._evidence_ids(raw.get("evidence"), where, kind=kind)
        eid = schema.edge_id(kind, raw.get("from", ""), raw.get("to", ""))
        if not ev_ids and eid not in self.b.edges:
            self.rejected.append({"where": where, "reason": "edge without accepted evidence"})
            return
        phase = raw.get("phase", "unknown")
        if phase not in schema.PHASES:
            phase = "unknown"
        self.b.edge(raw["from"], raw["to"], kind, label=str(raw.get("label", ""))[:80], payload=raw.get("payload") or [],
                    phase=phase, evidence_ids=ev_ids, **(raw.get("metadata") or {}))  # fmt: skip
        # Claude may refine the label/phase of an existing scan edge.
        cur = self.b.edges[eid]
        if raw.get("label"):
            cur["label"] = str(raw["label"])[:80]
        if phase != "unknown":
            cur["phase"] = phase

    def _add_structure(
        self, src: str, coll: str, raw: dict[str, Any], store: dict[str, dict]
    ) -> None:
        sid = raw.get("id")
        where = f"{src} {coll} {sid}"
        if not sid:
            self.rejected.append({"where": where, "reason": "missing id"})
            return
        ev_ids = self._evidence_ids(raw.get("evidence"), where)
        item = {k: v for k, v in raw.items() if k != "evidence"}
        # Stage/step-level evidence is checked too; unsupported stages are dropped.
        for key in ("stages", "steps"):
            kept = []
            for i, part in enumerate(item.get(key, []) or []):
                part = dict(part)
                if "evidence" in part:
                    part_ids = self._evidence_ids(part.pop("evidence"), f"{where} {key}[{i}]")
                    if not part_ids:
                        self.warnings.append(f"{where}: {key}[{i}] dropped (no accepted evidence)")
                        continue
                    part["evidence_ids"] = part_ids
                kept.append(part)
            if key in item:
                item[key] = kept
        has_part_evidence = any(
            p.get("evidence_ids") for k in ("stages", "steps") for p in item.get(k, []) or []
        )
        if not ev_ids and not has_part_evidence:
            self.rejected.append({"where": where, "reason": "no accepted evidence"})
            return
        item["evidence_ids"] = ev_ids
        if sid in store:
            self.warnings.append(f"{where}: replaces earlier definition with the same id")
        store[sid] = item

    # ---------------------------------------------------------------- output
    def build(self, extra_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        model = schema.empty_model()
        # Prune references the model cannot satisfy instead of failing validation.
        for n in self.b.nodes.values():
            if n.get("subsystem") and n["subsystem"] not in self.b.subsystems:
                self.warnings.append(f"node {n['id']}: unknown subsystem {n['subsystem']} removed")
                n["subsystem"] = None
            if n.get("parent") and n["parent"] not in self.b.nodes:
                self.warnings.append(f"node {n['id']}: unknown parent {n['parent']} removed")
                n["parent"] = None
        # Nodes without subsystem: inherit from parent, else evidence path prefix.
        subs = list(self.b.subsystems.values())
        for n in self.b.nodes.values():
            if n.get("subsystem") or n["kind"] in (
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
                continue
            parent = self.b.nodes.get(n.get("parent") or "")
            if parent and parent.get("subsystem"):
                n["subsystem"] = parent["subsystem"]
                continue
            files = [self.b.evidence[e]["file"] for e in n["evidence_ids"] if e in self.b.evidence]
            best = None
            for s in subs:
                for p in s.get("paths", []):
                    if any(f == p or f.startswith(p.rstrip("/") + "/") for f in files) and (
                        best is None or len(p) > best[1]
                    ):
                        best = (s["id"], len(p))
            if best:
                n["subsystem"] = best[0]
        # Drop edges whose endpoints never materialized (e.g. rejected nodes).
        for eid, e in list(self.b.edges.items()):
            if e["from"] not in self.b.nodes or e["to"] not in self.b.nodes:
                self.rejected.append({"where": f"edge {eid}", "reason": "endpoint node missing"})
                del self.b.edges[eid]
        # Drop structure parts referencing missing nodes, rather than failing the model.
        nodes = self.b.nodes
        for f in self.flows.values():
            steps = f.get("steps", []) or []
            f["steps"] = [s for s in steps if s.get("from") in nodes and s.get("to") in nodes]
            if len(f["steps"]) < len(steps):
                self.warnings.append(
                    f"flow {f['id']}: {len(steps) - len(f['steps'])} step(s) referenced unknown nodes and were dropped"
                )
        for d in self.dataflows.values():
            stages = d.get("stages", []) or []
            d["stages"] = [s for s in stages if s.get("node") in nodes]
            keep = {s["node"] for s in d["stages"]}
            d["links"] = [
                lk
                for lk in d.get("links", []) or []
                if lk.get("from") in keep and lk.get("to") in keep
            ]
            if len(d["stages"]) < len(stages):
                self.warnings.append(f"dataflow {d['id']}: stages with unknown nodes dropped")
        for a in self.algorithms.values():
            if a.get("node") and a["node"] not in nodes:
                self.warnings.append(f"algorithm {a['id']}: node {a['node']} unknown; link removed")
                a["node"] = None
            ids = {s.get("id") for s in a.get("stages", []) or []}
            a["transitions"] = [
                t
                for t in a.get("transitions", []) or []
                if t.get("from") in ids and t.get("to") in ids
            ]
        for sm in self.state_machines.values():
            ids = {s.get("id") for s in sm.get("states", []) or []}
            sm["transitions"] = [
                t
                for t in sm.get("transitions", []) or []
                if t.get("from") in ids and t.get("to") in ids
            ]
        model.update(
            subsystems=sorted(self.b.subsystems.values(), key=lambda s: s["id"]),
            nodes=sorted(self.b.nodes.values(), key=lambda n: n["id"]),
            edges=sorted(self.b.edges.values(), key=lambda e: e["id"]),
            evidence=sorted(self.b.evidence.values(), key=lambda e: e["id"]),
            flows=list(self.flows.values()),
            dataflows=list(self.dataflows.values()),
            algorithms=list(self.algorithms.values()),
            state_machines=list(self.state_machines.values()),
        )
        model["erd"] = self._erd()
        schema.apply_derived(model)
        inv = self.inventory
        model["meta"] = {
            "repo": {"name": inv["repo"]["name"], "remote": inv["repo"]["git"].get("remote")},
            "revision": inv["repo"]["git"].get("revision"),
            "dirty": inv["repo"]["git"].get("dirty"),
            "analyzed_at": utc_now(),
            "languages": inv["languages"],
            "frameworks": inv["manifests"]["frameworks"],
            "build_systems": inv["manifests"]["build_systems"],
            "package_managers": inv["manifests"]["package_managers"],
            "entry_points": [n["id"] for n in model["nodes"] if "entrypoint" in n.get("tags", [])],
            "excluded": {
                "dirs": inv["excluded_dirs"],
                "skipped": inv["skipped"],
                "patterns": inv["extra_excludes"],
            },
            "coverage": {
                "files_listed": len(inv["files"]),
                "source_files": inv["source_file_count"],
                "deterministically_analyzed_files": inv["analyzed_file_count"],
                "test_files": inv["test_file_count"],
                "unsupported_language_files": inv["unsupported_languages"],
                "parse_errors": len(inv.get("parse_errors", [])),
                "findings_sources": self.sources,
            },
            "uncertainties": self.uncertainties,
            "warnings": self.warnings,
            "rejected_count": len(self.rejected),
            **(extra_meta or {}),
        }
        return model

    def _erd(self) -> dict[str, Any]:
        entities = dict(self.erd["entities"])
        relations = list(self.erd["relations"])
        # Scan-derived entities (ORM models / CREATE TABLE) become ERD entities too.
        for n in self.b.nodes.values():
            if n["kind"] != "db_entity":
                continue
            eid = n["name"]
            entities.setdefault(
                eid,
                {
                    "id": eid,
                    "name": n["name"],
                    "attributes": n["metadata"].get("attributes", []),
                    "evidence_ids": n["evidence_ids"],
                },
            )
            for attr in n["metadata"].get("attributes", []) or []:
                ref = attr.get("references")
                if ref:
                    target = next(
                        (
                            m["name"]
                            for m in self.b.nodes.values()
                            if m["kind"] == "db_entity"
                            and m["name"] in (ref, ref.lower(), ref + "s")
                        ),
                        None,
                    )
                    if target:
                        rel = {
                            "from": target,
                            "to": eid,
                            "cardinality": "one-to-many",
                            "label": attr["name"],
                            "identifying": False,
                        }
                        if rel not in relations:
                            relations.append(rel)
        kept = [r for r in relations if r.get("from") in entities and r.get("to") in entities]
        if len(kept) < len(relations):
            self.warnings.append(
                f"erd: {len(relations) - len(kept)} relation(s) referenced unknown entities and were dropped"
            )
        return {"entities": sorted(entities.values(), key=lambda e: e["id"]), "relations": kept}


def merge_all(root: Path, out_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    inventory = read_json(out_dir / "inventory.json")
    m = Merger(root, inventory)
    findings_dir = out_dir / "findings"
    scan = findings_dir / "scan.json"
    if scan.exists():
        m.add_scan(read_json(scan))
    for path in sorted(findings_dir.glob("*.json")):
        if path.name == "scan.json":
            continue
        data = read_json(path)
        if not isinstance(data, dict):
            m.warnings.append(f"{path.name}: not a JSON object; ignored")
            continue
        m.add_findings(path.stem, data)
    model = m.build()
    report = schema.validate_model(model)
    report["rejected"] = m.rejected
    report["warnings"] = m.warnings + report["warnings"]
    return model, report
