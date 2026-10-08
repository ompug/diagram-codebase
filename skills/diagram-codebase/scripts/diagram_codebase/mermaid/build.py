"""DiagramSpec -> Mermaid text for Figma's `generate_diagram`.

One renderer per `spec["renderer"]`: flowchart, architecture, sequence, erd,
state. Output follows the official figma-generate-diagram rules (see
docs/DESIGN.md "Mermaid rules"): camelCase ids with no underscores or reserved
words, every label sanitized (`sanitize.sanitize_label`) and quoted, FigJam
palette colors on flowcharts only, no styling on state/ER diagrams, and only the
sequence constructs Figma renders.

Ids are derived deterministically from spec ids. `id_map(spec)` exposes the
spec-id -> Mermaid-id mapping so the Figma driver can verify what was drawn.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any

from ..sanitize import Redactor, sanitize_label

MAX_ID = 32
MAX_LABEL = 80
MAX_PARTICIPANT = 24
MAX_NESTING = 2

# FigJam built-in light palette: category -> (fill, stroke).
PALETTE: dict[str, tuple[str, str]] = {
    "app": ("#C2E5FF", "#3DADFF"),
    "data": ("#CDF4D3", "#66D575"),
    "processing": ("#DCCCFF", "#874FFF"),
    "external": ("#FFE0C2", "#FF9E42"),
    "infra": ("#D9D9D9", "#B3B3B3"),
    "error": ("#FFCDC2", "#FF7556"),
    "messaging": ("#C6FAF6", "#5AD8CC"),
    "context": ("#F5F5F5", "#B3B3B3"),
}
GROUP_DEFAULT_TINT = PALETTE["context"]

SHAPES: dict[str, tuple[str, str]] = {
    "rect": ('["', '"]'),
    "rounded": ('("', '")'),
    "stadium": ('(["', '"])'),
    "circle": ('(("', '"))'),
    "diamond": ('{"', '"}'),
    "hexagon": ('{{"', '"}}'),
    "subroutine": ('[["', '"]]'),
    "cylinder": ('[("', '")]'),
    "lean_r": ('[/"', '"/]'),
    "lean_l": ('[\\"', '"\\]'),
    "odd": ('>"', '"]'),
}

LANES = ("client", "gateway", "service", "datastore", "external", "async")
LANE_LABELS = {
    "client": "Clients",
    "gateway": "Gateway",
    "service": "Services",
    "datastore": "Data stores",
    "external": "External services",
    "async": "Messaging",
}

# Lowercased words that must not be used as ids (Mermaid keywords + Figma's reserved list).
FLOW_RESERVED = frozenset(
    [
        "end",
        "subgraph",
        "graph",
        "flowchart",
        "flowchart-elk",
        "style",
        "classdef",
        "class",
        "click",
        "default",
        "direction",
        "linkstyle",
        "call",
        "href",
        "callback",
        "interpolate",
        "acctitle",
        "accdescr",
        "tb",
        "td",
        "bt",
        "rl",
        "lr",
    ]
)
SEQ_RESERVED = frozenset(
    [
        "participant",
        "actor",
        "end",
        "loop",
        "alt",
        "else",
        "opt",
        "par",
        "and",
        "rect",
        "note",
        "activate",
        "deactivate",
        "autonumber",
        "title",
        "box",
        "critical",
        "break",
        "option",
        "link",
        "links",
        "create",
        "destroy",
        "as",
        "left",
        "right",
        "of",
        "over",
        "sequencediagram",
        "properties",
        "details",
        "accdescr",
        "acctitle",
    ]
)
STATE_RESERVED = frozenset(
    [
        "state",
        "note",
        "end",
        "direction",
        "classdef",
        "class",
        "style",
        "as",
        "hide",
        "scale",
        "statediagram",
        "statediagram-v2",
        "accdescr",
        "acctitle",
    ]
)
# ER lexer keywords, incl. word cardinalities (`one`, `many`, `to`) that break entity names.
ERD_RESERVED = frozenset(
    [
        "erdiagram",
        "direction",
        "style",
        "classdef",
        "class",
        "accdescr",
        "acctitle",
        "end",
        "subgraph",
        "one",
        "many",
        "to",
    ]
)

_ENTITY_RE = re.compile(r"[#&][A-Za-z0-9]+;")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_SOURCE_EXT_RE = re.compile(
    r"\.(?:py|pyi|js|mjs|cjs|ts|tsx|jsx|cpp|cc|cxx|c|h|hpp|hh|hxx|go|rs|java|kt|rb|php|cs|swift|"
    r"scala|sh|yaml|yml|json|toml|xml|launch\.py|launch\.xml)$",
    re.IGNORECASE,
)

Labeler = Callable[[Any], str]


# --- text ---------------------------------------------------------------------


def display_label(
    text: Any,
    max_len: int = MAX_LABEL,
    fallback: str = "unnamed",
    redactor: Redactor | None = None,
) -> str:
    """The exact text a label renders as (sanitized, Mermaid-safe, length-capped)."""
    clean = redactor.sanitize(text) if redactor else sanitize_label(text)[0]
    clean = _ENTITY_RE.sub("", clean)
    for old, new in (
        ('"', "'"),
        (";", " "),
        ("|", "/"),
        ("\\", "/"),
        ("<", "‹"),
        (">", "›"),
        ("%", "\uff05"),  # `%%` starts comments; a bare `%` breaks ER aliases
    ):
        clean = clean.replace(old, new)
    clean = _WS_RE.sub(" ", clean).strip()
    if len(clean) > max_len:
        clean = clean[: max_len - 3].rstrip() + "..."
    return clean or fallback


def _plain_text(label: str) -> str:
    """Unquoted message text (sequence/state): no `#` entities, no extra colons."""
    return _WS_RE.sub(" ", label.replace("#", "").replace(":", " -")).strip()


def _ascii(text: Any) -> str:
    return unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


# --- ids ----------------------------------------------------------------------


def camel_id(raw: Any, max_len: int = MAX_ID) -> str:
    """camelCase ascii id from any string; starts with a letter, no underscores."""
    words = _WORD_RE.findall(_ascii(raw)) or ["n"]
    first = words[0]
    out = first[0].lower() + first[1:] + "".join(w[0].upper() + w[1:] for w in words[1:])
    if not out[0].isalpha():
        out = "n" + out
    if len(out) > max_len:
        out = out[: max_len - 6] + _short_hash(str(raw))
    return out


def pascal_name(raw: Any, max_len: int = MAX_PARTICIPANT, strip_ext: bool = True) -> str:
    """Readable PascalCase name (`service.py` -> `Service`, `auth server` -> `AuthServer`)."""
    text = _ascii(raw).strip()
    if strip_ext:
        stripped = _SOURCE_EXT_RE.sub("", text)
        if _WORD_RE.search(stripped):
            text = stripped
    words = [w[0].upper() + w[1:] for w in _WORD_RE.findall(text)]
    out = ""
    for w in words:
        if out and len(out) + len(w) > max_len:
            break
        out += w
    out = out[:max_len]
    if out and not out[0].isalpha():
        out = "P" + out
    return out


class _Allocator:
    """Hands out unique ids (case-insensitively), avoiding reserved words."""

    def __init__(self, reserved: Iterable[str], suffix: str, sep: str = "") -> None:
        self.reserved = frozenset(r.lower() for r in reserved)
        self.used: set[str] = set()
        self.suffix = suffix
        self.sep = sep

    def take(self, base: str) -> str:
        if base.lower() in self.reserved:
            base = base + self.sep + self.suffix
        cand, n = base, 2
        while cand.lower() in self.used or cand.lower() in self.reserved:
            cand = f"{base}{self.sep}{n}"
            n += 1
        self.used.add(cand.lower())
        return cand


def _flow_ids(spec: dict[str, Any], extra_reserved: Iterable[str] = ()) -> dict[str, str]:
    alloc = _Allocator(set(FLOW_RESERVED) | set(extra_reserved), "Node")
    ids = [str(n["id"]) for n in spec.get("nodes", [])]
    return {sid: alloc.take(camel_id(sid)) for sid in sorted(set(ids))}


def _group_ids(spec: dict[str, Any], node_ids: dict[str, str]) -> dict[str, str]:
    alloc = _Allocator(set(FLOW_RESERVED), "Group")
    alloc.used.update(v.lower() for v in node_ids.values())
    gids = sorted({str(g["id"]) for g in spec.get("groups", [])})
    return {gid: alloc.take(camel_id("grp " + gid)) for gid in gids}


def _participants(spec: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [dict(p) for p in spec.get("participants", [])]
    known = {str(p["id"]) for p in parts}
    for m in spec.get("messages", []):
        for key in ("from", "to"):
            pid = str(m[key])
            if pid not in known:
                known.add(pid)
                parts.append({"id": pid, "label": pid})
    return parts


def _seq_ids(spec: dict[str, Any]) -> dict[str, str]:
    alloc = _Allocator(SEQ_RESERVED, "Actor")
    out: dict[str, str] = {}
    for p in _participants(spec):
        pid = str(p["id"])
        if pid in out:
            continue
        label = display_label(p.get("label") or pid, fallback=pid)
        out[pid] = alloc.take(pascal_name(label) or pascal_name(pid) or "Participant")
    return out


def _entities(spec: dict[str, Any]) -> list[dict[str, Any]]:
    ents = [dict(e) for e in spec.get("entities", [])]
    known = {str(e["id"]) for e in ents}
    for r in spec.get("relations", []):
        for key in ("from", "to"):
            eid = str(r[key])
            if eid not in known:
                known.add(eid)
                ents.append({"id": eid, "attributes": []})
    return ents


def _erd_name(raw: Any) -> str:
    text = re.sub(r"^[a-z]+:", "", str(raw))
    name = re.sub(r"[^A-Za-z0-9_]+", "_", _ascii(text)).strip("_")
    name = re.sub(r"_+", "_", name)
    if not name:
        name = "Entity"
    if not name[0].isalpha():
        name = "E_" + name
    if len(name) > MAX_ID:
        name = name[: MAX_ID - 7] + "_" + _short_hash(str(raw))
    return name


def _erd_ids(spec: dict[str, Any]) -> dict[str, str]:
    alloc = _Allocator(ERD_RESERVED, "entity", sep="_")
    return {
        eid: alloc.take(_erd_name(eid)) for eid in sorted({str(e["id"]) for e in _entities(spec)})
    }


def _states(spec: dict[str, Any]) -> list[dict[str, Any]]:
    states = [dict(s) for s in spec.get("states", [])]
    known = {str(s["id"]) for s in states}
    refs = [spec.get("initial"), *spec.get("finals", [])]
    for t in spec.get("transitions", []):
        refs += [t.get("from"), t.get("to")]
    for sid in refs:
        if sid and sid != "[*]" and str(sid) not in known:
            known.add(str(sid))
            states.append({"id": str(sid)})
    return states


def _state_ids(spec: dict[str, Any]) -> dict[str, str]:
    alloc = _Allocator(STATE_RESERVED, "State")
    out = {}
    for sid in sorted({str(s["id"]) for s in _states(spec)}):
        base = pascal_name(sid, max_len=MAX_ID, strip_ext=False) or "State"
        out[sid] = alloc.take(base)
    return out


def id_map(spec: dict[str, Any]) -> dict[str, str]:
    """Spec id -> Mermaid id (nodes, participants, entities or states by renderer)."""
    renderer = spec.get("renderer", "flowchart")
    if renderer == "architecture":
        return _flow_ids(spec, LANES)
    if renderer == "sequence":
        return _seq_ids(spec)
    if renderer == "erd":
        return _erd_ids(spec)
    if renderer == "state":
        return _state_ids(spec)
    return _flow_ids(spec)


# --- flowchart ------------------------------------------------------------------

_LINKS = {
    ("solid", "arrow"): "-->",
    ("solid", "cross"): "--x",
    ("solid", "circle"): "--o",
    ("solid", "none"): "---",
    ("dotted", "arrow"): "-.->",
    ("dotted", "cross"): "-.-x",
    ("dotted", "circle"): "-.-o",
    ("dotted", "none"): "-.-",
    ("thick", "arrow"): "==>",
    ("thick", "cross"): "==x",
    ("thick", "circle"): "==o",
    ("thick", "none"): "===",
}
_BIDIR_LINKS = {
    ("solid", "arrow"): "<-->",
    ("dotted", "arrow"): "<-.->",
    ("thick", "arrow"): "<==>",
    ("solid", "cross"): "x--x",
    ("dotted", "cross"): "x-.-x",
    ("thick", "cross"): "x==x",
    ("solid", "circle"): "o--o",
    ("dotted", "circle"): "o-.-o",
    ("thick", "circle"): "o==o",
}


def _link(style: str | None, end: str | None, bidir: bool) -> str:
    style = style if style in ("solid", "dotted", "thick") else "solid"
    end = end if end in ("arrow", "cross", "circle", "none") else "arrow"
    if bidir and end != "none":
        return _BIDIR_LINKS[(style, end)]
    return _LINKS[(style, end)]


def _edge_line(a: str, link: str, b: str, label: str) -> str:
    return f'{a} {link}|"{label}"| {b}' if label else f"{a} {link} {b}"


def _merge_labels(labels: Iterable[str]) -> str:
    seen: list[str] = []
    for lab in labels:
        if lab and lab not in seen:
            seen.append(lab)
    return ", ".join(seen)


def _node_line(mid: str, label: str, shape: str | None) -> str:
    opener, closer = SHAPES.get(shape or "rect", SHAPES["rect"])
    return f"{mid}{opener}{label}{closer}"


def _check_endpoint(spec: dict[str, Any], ids: dict[str, str], ref: Any) -> str:
    key = str(ref)
    if key not in ids:
        raise ValueError(
            f"diagram {spec.get('id')!r}: edge endpoint {key!r} is not a node of this diagram"
        )
    return ids[key]


def _group_tree(spec: dict[str, Any]) -> tuple[dict[str, str | None], dict[str, int]]:
    """Effective parent per group (nesting capped at MAX_NESTING) and depth."""
    groups = {str(g["id"]): g for g in spec.get("groups", [])}
    parent: dict[str, str | None] = {}
    depth: dict[str, int] = {}

    def chain(gid: str) -> list[str]:
        out, cur, seen = [], gid, set()
        while cur is not None and cur in groups and cur not in seen:
            seen.add(cur)
            out.append(cur)
            p = groups[cur].get("parent")
            cur = str(p) if p is not None else None
        return list(reversed(out))  # root ... gid

    for gid in groups:
        path = chain(gid)
        if len(path) > MAX_NESTING:
            path = path[: MAX_NESTING - 1] + [gid]
        parent[gid] = path[-2] if len(path) > 1 else None
        depth[gid] = len(path)
    return parent, depth


def _render_flowchart(spec: dict[str, Any], lab: Labeler) -> str:
    direction = str(spec.get("direction") or "LR").upper()
    if direction not in ("LR", "TD", "TB", "RL", "BT"):
        direction = "LR"
    ids = _flow_ids(spec)
    gids = _group_ids(spec, ids)
    parent, _depth = _group_tree(spec)
    groups = {str(g["id"]): g for g in spec.get("groups", [])}

    members: dict[str | None, list[dict[str, Any]]] = {}
    for n in spec.get("nodes", []):
        g = n.get("group")
        key = str(g) if g is not None and str(g) in groups else None
        members.setdefault(key, []).append(n)
    children: dict[str | None, list[str]] = {}
    for gid in groups:
        children.setdefault(parent[gid], []).append(gid)

    def has_content(gid: str) -> bool:
        return bool(members.get(gid)) or any(has_content(c) for c in children.get(gid, []))

    lines = [f"flowchart {direction}"]
    emitted_groups: list[str] = []
    emitted_nodes: set[str] = set()

    def emit_node(n: dict[str, Any], indent: str) -> None:
        sid = str(n["id"])
        if sid in emitted_nodes:
            return
        emitted_nodes.add(sid)
        lines.append(indent + _node_line(ids[sid], lab(n.get("label") or sid), n.get("shape")))

    def emit_group(gid: str, indent: str) -> None:
        if not has_content(gid):
            return
        label = lab(groups[gid].get("label") or gid)
        lines.append(f'{indent}subgraph {gids[gid]} ["{label}"]')
        emitted_groups.append(gid)
        for n in members.get(gid, []):
            emit_node(n, indent + "    ")
        for child in children.get(gid, []):
            emit_group(child, indent + "    ")
        lines.append(f"{indent}end")

    for gid in children.get(None, []):
        emit_group(gid, "    ")
    for n in members.get(None, []):
        emit_node(n, "    ")

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for e in spec.get("edges", []):
        a = _check_endpoint(spec, ids, e["from"])
        b = _check_endpoint(spec, ids, e["to"])
        key = (a, b)
        if key in merged:
            merged[key]["labels"].append(lab(e.get("label") or "") if e.get("label") else "")
            continue
        merged[key] = {
            "labels": [lab(e["label"]) if e.get("label") else ""],
            "link": _link(e.get("style"), e.get("end"), bool(e.get("bidir"))),
        }
    for (a, b), info in merged.items():
        lines.append("    " + _edge_line(a, info["link"], b, _merge_labels(info["labels"])))

    by_cat: dict[str, list[str]] = {}
    for n in spec.get("nodes", []):
        cat = n.get("category")
        if cat in PALETTE:
            by_cat.setdefault(cat, []).append(ids[str(n["id"])])
    for cat in PALETTE:
        if cat in by_cat:
            fill, stroke = PALETTE[cat]
            lines.append(f"    classDef {cat} fill:{fill},stroke:{stroke}")
    for cat in PALETTE:
        if cat in by_cat:
            lines.append(f"    class {','.join(sorted(set(by_cat[cat])))} {cat}")
    for gid in emitted_groups:
        fill, stroke = PALETTE.get(groups[gid].get("category") or "", GROUP_DEFAULT_TINT)
        lines.append(f"    style {gids[gid]} fill:{fill},stroke:{stroke}")
    return "\n".join(lines) + "\n"


# --- architecture -----------------------------------------------------------------


def _reaches(graph: dict[str, set[str]], start: str, goal: str) -> bool:
    stack, seen = [start], set()
    while stack:
        cur = stack.pop()
        if cur == goal:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, ()))
    return False


def _render_architecture(spec: dict[str, Any], lab: Labeler) -> str:
    ids = _flow_ids(spec, LANES)
    lanes_in = spec.get("lanes") or {}
    lane_of: dict[str, str] = {}
    for n in spec.get("nodes", []):
        lane = lanes_in.get(str(n["id"]))
        if lane not in LANES:
            raise ValueError(
                f"diagram {spec.get('id')!r}: node {n['id']!r} has no valid architecture lane ({lane!r})"
            )
        lane_of[ids[str(n["id"])]] = lane
    custom = spec.get("lane_labels") or {}

    lines = ["flowchart LR"]
    for lane in LANES:
        nodes = [n for n in spec.get("nodes", []) if lanes_in.get(str(n["id"])) == lane]
        if not nodes:
            continue
        lines.append(f'    subgraph {lane} ["{lab(custom.get(lane) or LANE_LABELS[lane])}"]')
        for n in nodes:
            lines.append(f'        {ids[str(n["id"])]}["{lab(n.get("label") or n["id"])}"]')
        lines.append("    end")

    # Merge edges per unordered pair: one edge per pair (Figma rule 10/11).
    pairs: dict[frozenset[str], dict[str, Any]] = {}
    for e in spec.get("edges", []):
        a = _check_endpoint(spec, ids, e["from"])
        b = _check_endpoint(spec, ids, e["to"])
        key = frozenset((a, b))
        info = pairs.setdefault(key, {"from": a, "to": b, "dirs": set(), "labels": []})
        info["dirs"].add((a, b))
        if e.get("bidir"):
            info["dirs"].add((b, a))
        if e.get("label"):
            info["labels"].append(lab(e["label"]))

    order = {lane: i for i, lane in enumerate(LANES)}
    forward: dict[str, set[str]] = {}
    for info in pairs.values():
        a, b = info["from"], info["to"]
        label = _merge_labels(info["labels"])
        bidir = len(info["dirs"]) == 2 and a != b
        if {lane_of[a], lane_of[b]} & {"async", "external"}:
            lines.append("    " + _edge_line(a, "-.->", b, label))
            if bidir:  # `<-.->` is unsupported: two dotted edges instead.
                lines.append("    " + _edge_line(b, "-.->", a, label))
            continue
        if bidir:
            if order[lane_of[b]] < order[lane_of[a]]:
                a, b = b, a
            forward.setdefault(a, set()).add(b)
            lines.append("    " + _edge_line(a, "<-->", b, label))
        elif _reaches(forward, b, a):
            # Would close a cycle among forward edges: draw as a backward edge.
            lines.append("    " + _edge_line(b, "<---", a, label))
        else:
            forward.setdefault(a, set()).add(b)
            lines.append("    " + _edge_line(a, "-->", b, label))
    return "\n".join(lines) + "\n"


# --- sequence ----------------------------------------------------------------------

_SEQ_ARROWS = {"sync": "->>", "reply": "-->>", "async": "-)"}
_SEQ_FALLBACK = {"sync": "call", "reply": "return", "async": "event"}


def _render_sequence(spec: dict[str, Any], lab: Labeler) -> str:
    ids = _seq_ids(spec)
    lines = ["sequenceDiagram"]
    title = _plain_text(lab(spec.get("title") or "")) if spec.get("title") else ""
    if title and title != "unnamed":
        lines.append(f"    title {title}")
    for p in _participants(spec):
        line = f"    participant {ids[str(p['id'])]}"
        if line not in lines:
            lines.append(line)
    for m in spec.get("messages", []):
        kind = m.get("kind") if m.get("kind") in _SEQ_ARROWS else "sync"
        text = _plain_text(lab(m.get("label") or "")) if m.get("label") else ""
        text = text or _SEQ_FALLBACK[kind]
        lines.append(f"    {ids[str(m['from'])]}{_SEQ_ARROWS[kind]}{ids[str(m['to'])]}: {text}")
    return "\n".join(lines) + "\n"


# --- ERD ----------------------------------------------------------------------------

_CARDINALITY = {
    "one-to-one": "||--||",
    "1:1": "||--||",
    "one-to-zero-or-one": "||--o|",
    "zero-or-one": "||--o|",
    "one-to-many": "||--o{",
    "1:n": "||--o{",
    "one-to-one-or-more": "||--|{",
    "many-to-one": "}o--||",
    "n:1": "}o--||",
    "many-to-many": "}o--o{",
    "n:m": "}o--o{",
    "m:n": "}o--o{",
}
_ATTR_KEYS = ("PK", "FK", "UK")


def _erd_token(raw: Any, fallback: str) -> str:
    tok = re.sub(r"[^A-Za-z0-9_]+", "_", _ascii(raw)).strip("_")
    tok = re.sub(r"_+", "_", tok)[:MAX_ID]
    if not tok:
        return fallback
    return tok if tok[0].isalpha() else f"{fallback[0]}_{tok}"


def _attr_keys(raw: Any) -> str:
    if not raw:
        return ""
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[\s,|/]+", str(raw))
    keys = [k for k in _ATTR_KEYS if k in {str(i).strip().upper() for i in items}]
    return ", ".join(keys)


def _cardinality(raw: Any, identifying: bool) -> str:
    key = re.sub(r"[\s_]+", "-", str(raw or "").strip().lower())
    card = _CARDINALITY.get(key, "}o--o{")
    return card if identifying else card.replace("--", "..")


def _render_erd(spec: dict[str, Any], lab: Labeler) -> str:
    ids = _erd_ids(spec)
    lines = ["erDiagram"]
    direction = str(spec.get("direction") or "").upper()
    if direction in ("LR", "TB", "TD", "RL", "BT"):
        lines.append(f"    direction {'TB' if direction == 'TD' else direction}")
    for e in _entities(spec):
        name = ids[str(e["id"])]
        label = lab(e["label"]) if e.get("label") else ""
        head = f'{name}["{label}"]' if label and label != name else name
        attrs = e.get("attributes") or []
        if not attrs:
            lines.append(f"    {head}")
            continue
        lines.append(f"    {head} {{")
        for a in attrs:
            parts = [_erd_token(a.get("type"), "string"), _erd_token(a.get("name"), "field")]
            keys = _attr_keys(a.get("key"))
            if keys:
                parts.append(keys)
            lines.append("        " + " ".join(parts))
        lines.append("    }")
    for r in spec.get("relations", []):
        card = _cardinality(r.get("cardinality"), bool(r.get("identifying", False)))
        label = lab(r["label"]) if r.get("label") else ""
        lines.append(f'    {ids[str(r["from"])]} {card} {ids[str(r["to"])]} : "{label}"')
    return "\n".join(lines) + "\n"


# --- state ---------------------------------------------------------------------------


def _render_state(spec: dict[str, Any], lab: Labeler) -> str:
    ids = _state_ids(spec)
    lines = ["stateDiagram-v2"]
    direction = str(spec.get("direction") or "").upper()
    if direction in ("LR", "TB", "TD", "RL", "BT"):
        lines.append(f"    direction {'TB' if direction == 'TD' else direction}")
    referenced = {spec.get("initial"), *spec.get("finals", [])}
    for t in spec.get("transitions", []):
        referenced |= {t.get("from"), t.get("to")}
    for s in _states(spec):
        sid = ids[str(s["id"])]
        label = _plain_text(lab(s.get("label") or s["id"])) or sid
        if label != sid:
            lines.append(f'    state "{label}" as {sid}')
        elif s["id"] not in referenced:
            lines.append(f"    {sid}")

    def ref(x: Any) -> str:
        return "[*]" if x == "[*]" else ids[str(x)]

    if spec.get("initial"):
        lines.append(f"    [*] --> {ref(spec['initial'])}")
    for t in spec.get("transitions", []):
        label = _plain_text(lab(t["label"])) if t.get("label") else ""
        line = f"    {ref(t['from'])} --> {ref(t['to'])}"
        lines.append(f"{line} : {label}" if label else line)
    for f in spec.get("finals", []):
        lines.append(f"    {ref(f)} --> [*]")
    return "\n".join(lines) + "\n"


# --- entry point ------------------------------------------------------------------------

_RENDERERS = {
    "flowchart": _render_flowchart,
    "architecture": _render_architecture,
    "sequence": _render_sequence,
    "erd": _render_erd,
    "state": _render_state,
}


def render(spec: dict[str, Any], redactor: Redactor | None = None) -> str:
    """Mermaid text for one DiagramSpec. Pass a Redactor to accumulate redaction counts."""
    renderer = spec.get("renderer", "flowchart")
    if renderer not in _RENDERERS:
        raise ValueError(f"diagram {spec.get('id')!r}: unknown renderer {renderer!r}")

    def lab(text: Any) -> str:
        return display_label(text, redactor=redactor)

    return _RENDERERS[renderer](spec, lab)
