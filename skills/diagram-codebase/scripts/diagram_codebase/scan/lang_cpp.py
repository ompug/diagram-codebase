"""C/C++ structural analysis (regex-based, no compiler required).

Extracts file modules, `#include` dependencies between repository files,
classes/structs with base classes, function and method definitions, `main`,
and call sites whose callee name is unique among repository definitions.
Call edges from this analyzer are `static_inferred` (name-based), never
`confirmed`, because overloads, templates and macros defeat regex resolution.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..model.builder import ModelBuilder
from .cpp_text import line_of, match_brace, strip_comments

INCLUDE_RE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]', re.M)
# The optional token before the name is an export macro (`class API_EXPORT Foo`), never `final`.
CLASS_RE = re.compile(
    r"\b(class|struct)\s+(?:[A-Z_][A-Z0-9_]*\s+)?(\w+)\s*(?:final\s*)?(?::\s*([^{;]+))?\{"
)
FUNC_RE = re.compile(
    r"^[ \t]*(?:template\s*<[^>]*>\s*)?(?:[\w:<>,\*&~\s]+?\s+[\*&]*)?((?:\w+::)*~?\w+)\s*\(([^;{}]*)\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?:->\s*[\w:<>]+\s*)?(?::[^{;]*)?\{",
    re.M,
)
CALL_RE = re.compile(r"(?:(\w+)\s*(\.|->|::)\s*)?\b(\w+)\s*\(")
KEYWORDS = {"if", "for", "while", "switch", "return", "catch", "sizeof", "decltype", "static_cast",
            "dynamic_cast", "reinterpret_cast", "const_cast", "defined", "else", "do", "new", "delete"}  # fmt: skip


def analyze_cpp(root: Path, files: list[dict[str, Any]], b: ModelBuilder) -> dict[str, Any]:
    cfiles = [f for f in files if f["language"] in ("c", "cpp") and f["analyzed"]]
    by_name: dict[str, list[str]] = defaultdict(list)
    for f in cfiles:
        by_name[f["path"].rsplit("/", 1)[-1]].append(f["path"])
    paths = {f["path"] for f in cfiles}
    texts: dict[str, str] = {}
    system_includes: set[str] = set()
    funcs_by_name: dict[str, list[str]] = defaultdict(list)
    bodies: list[tuple[str, str, int, int]] = []  # (fn id, path, start, end)

    for f in cfiles:
        path = f["path"]
        try:
            raw = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code = strip_comments(raw, keep_strings=False)
        texts[path] = code
        mid = f"mod:{path}"
        ev = b.ev(
            path,
            1,
            raw.count("\n") + 1,
            symbol=path.rsplit("/", 1)[-1],
            detail="source file",
            source="regex",
        )
        b.node(
            mid,
            path.rsplit("/", 1)[-1],
            "module",
            evidence_ids=[ev],
            file=path,
            language=f["language"],
        )

        for m in INCLUDE_RE.finditer(raw):
            inc = m.group(2)
            if m.group(1) == "<":
                system_includes.add(inc.split("/")[0])
                continue
            target = _resolve_include(path, inc, paths, by_name)
            if target and target != path:
                ln = line_of(raw, m.start())
                iev = b.ev(path, ln, ln, symbol=inc, detail=f'#include "{inc}"', source="regex")
                b.edge(mid, f"mod:{target}", "imports", evidence_ids=[iev], phase="build")
            elif target is None:
                system_includes.add(inc.split("/")[0])

        spans: list[tuple[int, int, str]] = []
        for m in CLASS_RE.finditer(code):
            name = m.group(2)
            spans.append((m.end() - 1, match_brace(code, m.end() - 1), name))
            ln = line_of(code, m.start())
            cid = f"cls:{path}:{name}"
            cev = b.ev(path, ln, ln, symbol=name, detail=f"{m.group(1)} definition", source="regex")
            bases = [
                re.sub(r"\b(public|private|protected|virtual)\b", "", x).strip()
                for x in (m.group(3) or "").split(",")
                if x.strip()
            ]
            b.node(cid, name, "class", parent=mid, evidence_ids=[cev], bases=bases, file=path)

        for m in FUNC_RE.finditer(code):
            qual = m.group(1)
            short = qual.split("::")[-1]
            if short in KEYWORDS:
                continue
            if "::" not in qual:
                inside = [sp for sp in spans if sp[0] < m.start(1) < sp[1]]
                if inside:
                    qual = f"{inside[-1][2]}::{qual}"
            ln = line_of(code, m.start(1))
            brace = code.find("{", m.end(2))
            end = match_brace(code, brace)
            dotted = qual.replace("::", ".")
            fid = f"fn:{path}:{dotted}"
            fev = b.ev(
                path,
                ln,
                line_of(code, end),
                symbol=qual,
                detail="function definition",
                source="regex",
            )
            parent = mid
            if "::" in qual:
                owner = qual.rsplit("::", 1)[0].split("::")[-1]
                cands = [
                    nid for nid in b.nodes if nid.startswith("cls:") and nid.endswith(f":{owner}")
                ]
                if cands:
                    parent = cands[0]
            tags = ["entrypoint"] if short == "main" else []
            b.node(
                fid,
                f"{qual}()",
                "function",
                parent=parent,
                evidence_ids=[fev],
                tags=tags,
                file=path,
                line=ln,
            )
            if short == "main":
                b.nodes[mid]["tags"] = sorted(set(b.nodes[mid]["tags"]) | {"entrypoint"})
            funcs_by_name[short].append(fid)
            bodies.append((fid, path, brace, end))

    # Inheritance between repository classes.
    for nid, n in list(b.nodes.items()):
        if n["kind"] != "class" or not n["metadata"].get("bases") or not nid.startswith("cls:"):
            continue
        for base in n["metadata"]["bases"]:
            short = base.split("::")[-1].split("<")[0].strip()
            targets = [
                x for x in b.nodes if x.startswith("cls:") and x.endswith(f":{short}") and x != nid
            ]
            if len(targets) == 1:
                b.edge(nid, targets[0], "inherits", evidence_ids=n["evidence_ids"][:1])

    # Name-based call resolution, only for names defined exactly once, and only when the
    # call shape fits the target: `obj.f()` needs a method, `X::f()` needs class/namespace X
    # (never std::), and a bare `f()` needs a free function or a method of the caller's class.
    for fid, path, start, end in bodies:
        body = texts[path][start:end]
        caller_cls = _owner_class(fid)
        for m in CALL_RE.finditer(body):
            qual, sep, name = m.group(1), m.group(2), m.group(3)
            if name in KEYWORDS:
                continue
            targets = funcs_by_name.get(name, [])
            if len(targets) != 1 or targets[0] == fid:
                continue
            target_cls = _owner_class(targets[0])
            if sep in (".", "->"):
                if target_cls is None:
                    continue
            elif sep == "::":
                if qual == "std" or (target_cls is not None and target_cls != qual):
                    continue
            elif target_cls is not None and target_cls != caller_cls:
                continue
            ln = line_of(texts[path], start + m.start())
            cev = b.ev(
                path,
                ln,
                ln,
                symbol=name,
                detail=f"call {name}()",
                source="regex",
                status="static_inferred",
            )
            b.edge(fid, targets[0], "calls", evidence_ids=[cev])

    return {"system_includes": sorted(system_includes), "files": len(cfiles)}


def _owner_class(fid: str) -> str | None:
    """`fn:path:Ns.Class.method` -> `Class`; free functions -> None."""
    qual = fid.split(":", 2)[2]
    parts = qual.split(".")
    return parts[-2] if len(parts) >= 2 else None


def _resolve_include(
    src: str, inc: str, paths: set[str], by_name: dict[str, list[str]]
) -> str | None:
    base = src.rsplit("/", 1)[0] if "/" in src else ""
    cand = f"{base}/{inc}" if base else inc
    parts: list[str] = []
    for p in cand.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p and p != ".":
            parts.append(p)
    norm = "/".join(parts)
    if norm in paths:
        return norm
    suffix = [p for p in paths if p.endswith("/" + inc) or p == inc]
    if len(suffix) == 1:
        return suffix[0]
    named = by_name.get(inc.rsplit("/", 1)[-1], [])
    return named[0] if len(named) == 1 else None
