"""Lint Mermaid text against Figma `generate_diagram` rules.

Independent of `build.py` on purpose: it parses the text itself (tolerantly) so
it also catches hand-written or hostile Mermaid. Rules come from the official
figma-generate-diagram skill and its references (flowchart, architecture,
sequence, state, erd); docs/DESIGN.md "Mermaid rules" summarizes them.

`lint(text, renderer, config)` returns issues
`{"severity": "error"|"warning", "rule": str, "line": int, "message": str}`
sorted by line. Line numbers are 1-based; 0 means the whole diagram. Any error
means the text must not be sent to Figma.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config import DEFAULTS
from ..sanitize import EMOJI_RE

RENDERERS = ("flowchart", "architecture", "sequence", "erd", "state")
LANES = ("client", "gateway", "service", "datastore", "external", "async")
# Allowed (source lane, target lane) per edge kind, as written in the Mermaid source
# (references/architecture.md "Allowed edges"; plan/archmode.py uses the same table).
ARCH_FORWARD = {
    ("client", "gateway"),
    ("gateway", "service"),
    ("service", "service"),
    ("service", "datastore"),
}
ARCH_DOTTED = {("service", "async"), ("async", "service"), ("service", "external")}
ARCH_BIDIR = {("client", "gateway"), ("service", "service")}
ARCH_BACKWARD = {("service", "service")}

FIGMA_RESERVED = {"end", "subgraph", "graph"}
FLOW_KEYWORDS = {
    "style",
    "class",
    "classdef",
    "click",
    "default",
    "flowchart",
    "direction",
    "linkstyle",
}
SEQ_FORBIDDEN = {
    "note": "Notes are silently stripped",
    "loop": "loop blocks are dropped",
    "alt": "alt/else blocks are dropped",
    "else": "alt/else blocks are dropped",
    "opt": "opt blocks are dropped",
    "par": "par blocks are dropped",
    "and": "par blocks are dropped",
    "critical": "critical blocks are dropped",
    "option": "critical blocks are dropped",
    "break": "break blocks are dropped",
    "rect": "colored rect blocks are dropped",
    "box": "box groupings are dropped",
    "activate": "activation boxes do not render",
    "deactivate": "activation boxes do not render",
    "autonumber": "autonumber is dropped",
    "link": "links are not supported",
    "links": "links are not supported",
    "end": "block constructs are dropped",
}
ERD_RESERVED = {
    "end",
    "subgraph",
    "class",
    "classdef",
    "style",
    "one",
    "many",
    "to",
    "acctitle",
    "accdescr",
}

_HTML_RE = re.compile(r"(?<!<)<\s*/?\s*[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?\s*>(?!>)")
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
_UNQUOTED_SPECIAL_RE = re.compile(r"[()\[\]{}<>|\"#;:&@%/\\`]")
_ENTITY_CODE_RE = re.compile(r"#\w+;")


@dataclass
class _Issues:
    items: list[dict[str, Any]] = field(default_factory=list)
    seen: set[tuple[str, str, int, str]] = field(default_factory=set)

    def add(self, severity: str, rule: str, line: int, message: str) -> None:
        key = (severity, rule, line, message)
        if key not in self.seen:
            self.seen.add(key)
            self.items.append(
                {"severity": severity, "rule": rule, "line": line, "message": message}
            )

    def error(self, rule: str, line: int, message: str) -> None:
        self.add("error", rule, line, message)

    def warn(self, rule: str, line: int, message: str) -> None:
        self.add("warning", rule, line, message)


def _plan_config(config: dict[str, Any] | None) -> dict[str, Any]:
    plan = dict(DEFAULTS["plan"])
    if config:
        plan.update(config.get("plan", config) if isinstance(config.get("plan"), dict) else config)
    return plan


def _split_outside_quotes(text: str, sep: str = ";") -> list[str]:
    out, buf, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _statements(lines: list[str], start: int, split: bool = True) -> list[tuple[int, str]]:
    """(1-based line, statement) for every non-comment statement after the header.

    Sequence messages are not split on `;` (Figma rewrites them inside labels).
    """
    out = []
    for idx in range(start, len(lines)):
        raw = lines[idx].strip()
        if not raw or raw.startswith("%%"):
            continue
        for stmt in _split_outside_quotes(raw) if split else [raw]:
            stmt = stmt.strip()
            if stmt:
                out.append((idx + 1, stmt))
    return out


# --- universal checks -----------------------------------------------------------------


def _universal(lines: list[str], issues: _Issues) -> None:
    for i, line in enumerate(lines, start=1):
        if EMOJI_RE.search(line):
            issues.error("emoji", i, "emoji are rejected by Figma; remove them")
        if "\\n" in line:
            issues.error("literal-newline", i, "literal \\n in source; Figma does not render it")
        m = _HTML_RE.search(line)
        if m:
            issues.error(
                "html-tag", i, f"HTML tag {m.group(0)!r}; Figma labels cannot contain HTML"
            )
        if _CONTROL_RE.search(line):
            issues.error("control-char", i, "control or invisible formatting character")
        stripped = line.strip()
        if stripped.startswith("%%{"):
            issues.warn("directive", i, "init directives are ignored by Figma")
        elif not stripped.startswith("%%") and stripped.count('"') % 2:
            issues.error("unbalanced-quote", i, "odd number of double quotes")
        if "`" in line and not stripped.startswith("%%"):
            issues.warn("backtick", i, "backticks (markdown strings) may not render in Figma")


_HEADERS = {
    "flowchart": "flowchart",
    "architecture": "flowchart",
    "sequence": "sequenceDiagram",
    "erd": "erDiagram",
    "state": "stateDiagram-v2",
}


def _header(lines: list[str], renderer: str, issues: _Issues) -> int | None:
    """Validate the header; return the index of the first line after it."""
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("%%"):
            continue
        if s == "---":
            issues.error(
                "front-matter",
                idx + 1,
                "front matter is not supported; start with the diagram keyword",
            )
            return None
        words = s.split()
        kw = words[0]
        expected = _HEADERS[renderer]
        if kw == "graph":
            issues.error("header", idx + 1, "use `flowchart`, not `graph`")
            return idx + 1
        if kw == "stateDiagram" and renderer == "state":
            issues.warn("header", idx + 1, "prefer `stateDiagram-v2` over legacy `stateDiagram`")
            return idx + 1
        if kw not in set(_HEADERS.values()):
            issues.error("header", idx + 1, f"unsupported diagram type {kw!r}")
            return None
        if kw != expected:
            issues.error(
                "header", idx + 1, f"renderer {renderer!r} needs `{expected}`, found `{kw}`"
            )
            return None
        if kw == "flowchart":
            direction = words[1] if len(words) > 1 else ""
            if len(words) > 2 or direction not in ("", "LR", "TD", "TB", "RL", "BT"):
                issues.error("header", idx + 1, f"bad flowchart header {s!r}")
            if renderer == "architecture" and direction != "LR":
                issues.error(
                    "arch-direction", idx + 1, "architecture diagrams must be `flowchart LR`"
                )
        elif len(words) > 1:
            issues.error("header", idx + 1, f"unexpected text after `{kw}`")
        return idx + 1
    issues.error("header", 0, "empty diagram")
    return None


# --- flowchart parsing -----------------------------------------------------------------

_ID_CH = r"[^\s\[\](){}<>|&;:\"'=\-.,@%`~]"
_ID_RE = re.compile(rf"{_ID_CH}+(?:[.\-](?![-.=>])(?:{_ID_CH})+)*")
_OPENERS = [
    ("(((", (")))",)),
    ("((", ("))",)),
    ("([", ("])",)),
    ("[[", ("]]",)),
    ("[(", (")]",)),
    ("[/", ("/]", "\\]")),
    ("[\\", ("\\]", "/]")),
    ("{{", ("}}",)),
    ("(", (")",)),
    ("[", ("]",)),
    ("{", ("}",)),
    (">", ("]",)),
]
_LINK_RE = re.compile(r"(?P<left>[<xo])?(?P<body>-{2,}|={2,}|-\.+-|~{3,})(?P<right>[>xo])?")
_TEXT_LINK_OPEN_RE = re.compile(r"(--|==|-\.)\s+(?![-=>])")
_TEXT_LINK_CLOSE_RE = re.compile(
    r"\s*(-{2,}[>xo]|-{3,}|={2,}>|={3,}|\.-+>|\.-+)(?=\s|$|[A-Za-z0-9_])"
)


@dataclass
class _Node:
    id: str
    line: int
    group: str | None
    label: str | None = None


@dataclass
class _Edge:
    src: str
    dst: str
    line: int
    link: str
    label: str
    dotted: bool
    thick: bool
    bidir: bool
    backward: bool


@dataclass
class _Flow:
    nodes: dict[str, _Node] = field(default_factory=dict)
    edges: list[_Edge] = field(default_factory=list)
    subgraphs: dict[str, dict[str, Any]] = field(default_factory=dict)
    styled: list[tuple[int, str]] = field(default_factory=list)  # (line, kind)
    style_targets: list[tuple[int, str]] = field(default_factory=list)


class _ParseError(Exception):
    pass


def _check_label(text: str, quoted: bool, line: int, issues: _Issues, what: str) -> None:
    if not quoted and _UNQUOTED_SPECIAL_RE.search(text):
        issues.error(
            "unquoted-label",
            line,
            f"{what} {text.strip()!r} has special characters; wrap it in quotes",
        )
    if _ENTITY_CODE_RE.search(text):
        issues.warn("entity-code", line, f"{what} {text.strip()!r} contains an entity code")


def _parse_node(s: str, i: int, line: int, issues: _Issues) -> tuple[str, str | None, int]:
    m = _ID_RE.match(s, i)
    if not m:
        raise _ParseError(f"expected a node id at {s[i : i + 20]!r}")
    nid, i = m.group(0), m.end()
    label: str | None = None
    if s.startswith("@{", i):
        close = s.find("}", i)
        if close < 0:
            raise _ParseError("unterminated @{...} shape")
        body = s[i + 2 : close]
        lm = re.search(r"label\s*:\s*\"([^\"]*)\"", body)
        label = lm.group(1) if lm else None
        i = close + 1
    else:
        for opener, closers in _OPENERS:
            if not s.startswith(opener, i):
                continue
            j = i + len(opener)
            k = j
            while k < len(s) and s[k] == " ":
                k += 1
            if k < len(s) and s[k] == '"':
                end_q = s.find('"', k + 1)
                if end_q < 0:
                    raise _ParseError("unterminated quoted label")
                label = s[k + 1 : end_q]
                k = end_q + 1
                while k < len(s) and s[k] == " ":
                    k += 1
                closer = next((c for c in closers if s.startswith(c, k)), None)
                if closer is None:
                    raise _ParseError(f"label of {nid!r} is not closed with {closers[0]!r}")
                i = k + len(closer)
                _check_label(label, True, line, issues, "label")
            else:
                hits = [(s.find(c, j), c) for c in closers if s.find(c, j) >= 0]
                if not hits:
                    raise _ParseError(f"shape of {nid!r} is not closed")
                pos, closer = min(hits)
                label = s[j:pos]
                _check_label(label, False, line, issues, "label")
                i = pos + len(closer)
            break
    if s.startswith(":::", i):
        cm = re.compile(r":::[\w-]+").match(s, i)
        i = cm.end() if cm else i + 3
    return nid, label, i


def _parse_group(
    s: str, i: int, line: int, issues: _Issues
) -> tuple[list[tuple[str, str | None]], int]:
    nodes = []
    while True:
        while i < len(s) and s[i] == " ":
            i += 1
        nid, label, i = _parse_node(s, i, line, issues)
        nodes.append((nid, label))
        m = re.compile(r"\s*&\s*").match(s, i)
        if not m:
            return nodes, i
        i = m.end()


def _parse_link(s: str, i: int, line: int, issues: _Issues) -> tuple[str, str, int]:
    """Return (link token, label, new index)."""
    tm = _TEXT_LINK_OPEN_RE.match(s, i)
    if tm:
        cm = _TEXT_LINK_CLOSE_RE.search(s, tm.end())
        if cm:
            text = s[tm.end() : cm.start()].strip()
            quoted = len(text) >= 2 and text[0] == text[-1] == '"'
            _check_label(text, quoted, line, issues, "edge label")
            close = cm.group(1)
            return tm.group(1) + close, text.strip('"'), cm.end()
    m = _LINK_RE.match(s, i)
    if not m:
        raise _ParseError(f"expected a link at {s[i : i + 20]!r}")
    link, i = m.group(0), m.end()
    label = ""
    lm = re.compile(r"\s*\|").match(s, i)
    if lm:
        j = lm.end()
        k = j
        while k < len(s) and s[k] == " ":
            k += 1
        if k < len(s) and s[k] == '"':
            end_q = s.find('"', k + 1)
            if end_q < 0:
                raise _ParseError("unterminated quoted edge label")
            label = s[k + 1 : end_q]
            close = s.find("|", end_q)
            if close < 0:
                raise _ParseError("edge label is not closed with |")
            _check_label(label, True, line, issues, "edge label")
        else:
            close = s.find("|", j)
            if close < 0:
                raise _ParseError("edge label is not closed with |")
            label = s[j:close]
            _check_label(label, False, line, issues, "edge label")
        i = close + 1
    return link, label, i


def _link_kind(link: str) -> dict[str, bool]:
    left = link[0] if link[0] in "<xo" and len(link) > 2 and link[1] in "-=." else ""
    right = link[-1] if link[-1] in ">xo" else ""
    return {
        "dotted": "." in link,
        "thick": "=" in link,
        "bidir": bool(left and right),
        "backward": left == "<" and not right,
    }


def _parse_flow(lines: list[str], start: int, plan: dict[str, Any], issues: _Issues) -> _Flow:
    flow = _Flow()
    stack: list[str] = []
    max_nesting = int(plan.get("max_nesting", 2))

    def touch(nid: str, label: str | None, line: int) -> None:
        group = stack[-1] if stack else None
        node = flow.nodes.get(nid)
        if node is None:
            flow.nodes[nid] = _Node(nid, line, group, label)
        else:
            if node.group is None and group is not None:
                node.group = group
            if label is not None:
                node.label = label

    for line, stmt in _statements(lines, start):
        word = stmt.split()[0]
        if word == "subgraph":
            rest = stmt[len("subgraph") :].strip()
            m = re.match(r'^([^\s\["]+)\s*\[\s*"?(.*?)"?\s*\]$', rest)
            if m:
                sid, label = m.group(1), m.group(2)
                if '"' not in rest:
                    _check_label(label, False, line, issues, "subgraph label")
            elif rest.startswith('"'):
                sid = label = rest.strip('"')
            else:
                sid = label = rest
            if not sid:
                issues.error("subgraph", line, "subgraph without an id")
                sid = f"<anonymous@{line}>"
            if sid in flow.subgraphs:
                issues.warn("subgraph-duplicate", line, f"subgraph {sid!r} declared twice")
            flow.subgraphs.setdefault(
                sid, {"line": line, "depth": len(stack) + 1, "parent": stack[-1] if stack else None}
            )
            stack.append(sid)
            if len(stack) > max_nesting:
                issues.warn("nesting", line, f"subgraph nesting depth {len(stack)} > {max_nesting}")
            continue
        if stmt == "end":
            if not stack:
                issues.error("unbalanced-end", line, "`end` without an open subgraph")
            else:
                stack.pop()
            continue
        if word == "direction":
            continue
        if word in ("classDef", "style"):
            flow.styled.append((line, word))
            parts = stmt.split(None, 2)
            if word == "style" and len(parts) > 1:
                flow.style_targets.append((line, parts[1]))
            props = parts[2] if len(parts) > 2 else ""
            for prop in props.split(","):
                key = prop.split(":", 1)[0].strip()
                if key and key not in ("fill", "stroke"):
                    issues.warn(
                        "style-props",
                        line,
                        f"only fill/stroke are applied by Figma; {key!r} is ignored",
                    )
            continue
        if word == "class":
            flow.styled.append((line, word))
            parts = stmt.split()
            if len(parts) >= 2:
                for target in parts[1].split(","):
                    flow.style_targets.append((line, target))
            continue
        if word == "linkStyle":
            flow.styled.append((line, word))
            issues.warn("link-style", line, "linkStyle is ignored by Figma")
            continue
        if word == "click":
            issues.error("click", line, "click/href interactions are not supported")
            continue
        try:
            i = 0
            groups, i = _parse_group(stmt, i, line, issues)
            for nid, label in groups:
                touch(nid, label, line)
            if ":::" in stmt:
                flow.styled.append((line, ":::"))
            while True:
                while i < len(stmt) and stmt[i] == " ":
                    i += 1
                if i >= len(stmt):
                    break
                link, label, i = _parse_link(stmt, i, line, issues)
                nxt, i = _parse_group(stmt, i, line, issues)
                for nid, nlabel in nxt:
                    touch(nid, nlabel, line)
                kind = _link_kind(link)
                for a, _ in groups:
                    for b, _ in nxt:
                        flow.edges.append(_Edge(a, b, line, link, label, **kind))
                groups = nxt
        except _ParseError as exc:
            issues.error("syntax", line, f"cannot parse statement: {exc}")
    if stack:
        issues.error("unbalanced-subgraph", 0, f"{len(stack)} subgraph(s) not closed with `end`")
    return flow


def _flow_checks(flow: _Flow, renderer: str, plan: dict[str, Any], issues: _Issues) -> None:
    arch = renderer == "architecture"
    for nid, node in flow.nodes.items():
        low = nid.lower()
        if low in FIGMA_RESERVED:
            issues.error("reserved-id", node.line, f"node id {nid!r} is reserved")
        elif low in FLOW_KEYWORDS:
            issues.error("reserved-id", node.line, f"node id {nid!r} is a Mermaid keyword")
        if "_" in nid:
            issues.error(
                "underscore-id",
                node.line,
                f"node id {nid!r} contains '_' (breaks Figma edge routing)",
            )
        elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", nid):
            issues.warn("id-format", node.line, f"node id {nid!r} is not camelCase ascii")
        if nid in flow.subgraphs:
            issues.error("id-collision", node.line, f"{nid!r} is both a node and a subgraph id")
    for sid, sg in flow.subgraphs.items():
        if sid.lower() in FIGMA_RESERVED:
            issues.error("reserved-id", sg["line"], f"subgraph id {sid!r} is reserved")
        if "_" in sid:
            issues.error("underscore-id", sg["line"], f"subgraph id {sid!r} contains '_'")
    seen: dict[tuple[str, str], int] = {}
    for e in flow.edges:
        for end in (e.src, e.dst):
            if end in flow.subgraphs:
                (issues.error if arch else issues.warn)(
                    "edge-to-subgraph",
                    e.line,
                    f"edge connects to subgraph {end!r}; connect to a node inside it",
                )
        key = (e.src, e.dst)
        if key in seen and not arch:
            issues.warn(
                "duplicate-edge",
                e.line,
                f"duplicate edge {e.src} -> {e.dst} (first on line {seen[key]})",
            )
        seen.setdefault(key, e.line)
    real_nodes = [n for n in flow.nodes if n not in flow.subgraphs]
    if len(real_nodes) > int(plan["max_nodes"]):
        issues.warn(
            "density", 0, f"{len(real_nodes)} nodes > {plan['max_nodes']}; consider splitting"
        )
    if len(flow.edges) > int(plan["max_edges"]):
        issues.warn(
            "density", 0, f"{len(flow.edges)} edges > {plan['max_edges']}; consider splitting"
        )
    known = set(flow.nodes) | set(flow.subgraphs)
    for line, target in flow.style_targets:
        if target not in known:
            issues.warn(
                "unknown-style-target", line, f"style/class target {target!r} is not defined"
            )
    if arch:
        _arch_checks(flow, plan, issues)


def _has_cycle(nodes: list[str], adj: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}
    for root in nodes:
        if state.get(root):
            continue
        stack = [(root, iter(adj.get(root, [])))]
        state[root] = 1
        while stack:
            cur, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[cur] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                return True
            elif not state.get(nxt):
                state[nxt] = 1
                stack.append((nxt, iter(adj.get(nxt, []))))
    return False


def _arch_checks(flow: _Flow, plan: dict[str, Any], issues: _Issues) -> None:
    for sid, sg in flow.subgraphs.items():
        if sid not in LANES:
            issues.error(
                "arch-lane-id", sg["line"], f"subgraph id {sid!r} must be one of {', '.join(LANES)}"
            )
        if sg["parent"] is not None:
            issues.error(
                "arch-nested", sg["line"], f"lane {sid!r} is nested; lanes must be top-level"
            )
    lane: dict[str, str] = {}
    for nid, node in flow.nodes.items():
        if nid in flow.subgraphs:
            continue
        if node.group in LANES:
            lane[nid] = node.group
        else:
            issues.error(
                "arch-node-outside-lane", node.line, f"node {nid!r} is not inside a lane subgraph"
            )
    if flow.styled:
        issues.warn(
            "arch-styling",
            flow.styled[0][0],
            "architecture colors are auto-assigned; drop classDef/class/style",
        )
    pairs: dict[frozenset[str], list[_Edge]] = {}
    forward: dict[str, list[str]] = {}
    degree_in: dict[str, int] = {}
    degree_out: dict[str, int] = {}
    for e in flow.edges:
        pairs.setdefault(frozenset((e.src, e.dst)), []).append(e)
        a, b = lane.get(e.src), lane.get(e.dst)
        src, dst = (e.dst, e.src) if e.backward else (e.src, e.dst)
        degree_out[src] = degree_out.get(src, 0) + 1
        degree_in[dst] = degree_in.get(dst, 0) + 1
        if e.bidir:
            degree_out[dst] = degree_out.get(dst, 0) + 1
            degree_in[src] = degree_in.get(src, 0) + 1
        if a is None or b is None:
            continue
        soft = {a, b} & {"async", "external"}
        desc = f"{e.src} ({a}) -> {e.dst} ({b})"
        if e.dotted and e.bidir:
            issues.error(
                "arch-bidir-async",
                e.line,
                f"`<-.->` is not supported ({desc}); use two `-.->` edges",
            )
            continue
        if soft and not e.dotted:
            issues.error(
                "arch-dotted",
                e.line,
                f"edges touching async/external must be dotted `-.->` ({desc})",
            )
            continue
        if e.dotted and not soft:
            issues.warn("arch-dotted-core", e.line, f"dotted edge between core lanes ({desc})")
        if e.thick:
            issues.warn(
                "arch-thick", e.line, f"thick edges have no meaning in architecture mode ({desc})"
            )
        if e.link[-1] in "xo":
            issues.warn("arch-end-cap", e.line, f"use plain arrows in architecture mode ({desc})")
        if e.backward:
            allowed, kind = ARCH_BACKWARD, "backward `<---`"
        elif e.bidir:
            allowed, kind = ARCH_BIDIR, "bidirectional `<-->`"
        elif e.dotted:
            allowed, kind = ARCH_DOTTED, "dotted"
        else:
            allowed, kind = ARCH_FORWARD, "forward"
        if (a, b) not in allowed:
            issues.error(
                "arch-lane-pair", e.line, f"{kind} edge {desc} is not an allowed lane pair"
            )
        if not e.dotted and not e.backward:
            forward.setdefault(e.src, []).append(e.dst)
    for edges in pairs.values():
        if len(edges) < 2:
            continue
        opposite_dotted = (
            len(edges) == 2
            and all(e.dotted for e in edges)
            and edges[0].src == edges[1].dst
            and edges[0].dst == edges[1].src
        )
        if not opposite_dotted:
            # A correctness rule, not a hard rule: Figma's own example has one such pair.
            e = edges[1]
            issues.warn(
                "arch-duplicate-edge",
                e.line,
                f"more than one edge between {e.src} and {e.dst}; merge them",
            )
    nodes = [n for n in flow.nodes if n not in flow.subgraphs]
    if _has_cycle(nodes, forward):
        issues.error(
            "arch-cycle", 0, "forward/bidirectional edges form a cycle; use a backward `<---` edge"
        )
    limit = int(plan.get("architecture_max_edges", 20))
    if len(flow.edges) > limit:
        issues.warn("arch-density", 0, f"{len(flow.edges)} edges > {limit}")
    for nid in nodes:
        if lane.get(nid) == "service" and not (degree_in.get(nid) and degree_out.get(nid)):
            issues.warn(
                "arch-service-io",
                flow.nodes[nid].line,
                f"service {nid!r} lacks an input or an output edge",
            )


# --- sequence -------------------------------------------------------------------------------

_SEQ_MSG_RE = re.compile(
    r"^(?P<a>[^\s:+<>()-][^:<>()]*?)\s*"
    r"(?P<arrow><<-->>|<<->>|-->>|->>|--x|-x|--\)|-\)|-->|->)"
    r"(?P<act>[+-]?)\s*(?P<b>[^:]+?)\s*(?::(?P<text>.*))?$"
)


def _sequence(lines: list[str], start: int, plan: dict[str, Any], issues: _Issues) -> None:
    participants: set[str] = set()
    messages = 0
    for line, stmt in _statements(lines, start, split=False):
        word = stmt.split()[0]
        low = word.lower()
        if low in SEQ_FORBIDDEN:
            issues.error("seq-forbidden", line, f"`{word}`: {SEQ_FORBIDDEN[low]} by Figma")
            continue
        if low in ("classdef", "class", "style", "linkstyle"):
            issues.error("styling-unsupported", line, "sequence diagrams do not support styling")
            continue
        if low in ("create", "destroy"):
            issues.warn("seq-lifecycle", line, f"`{word}` participants may not render in Figma")
            continue
        if low == "title" or low.startswith("acc"):
            continue
        if low in ("participant", "actor"):
            rest = stmt[len(word) :].strip()
            if re.search(r"\sas\s", f" {rest} "):
                issues.error(
                    "seq-alias",
                    line,
                    "`as` aliases are dropped; the participant id is what renders",
                )
                rest = rest.split(" as ")[0].strip()
            if "@{" in rest:
                issues.warn(
                    "seq-type-annotation", line, "participant type annotations render the same"
                )
                rest = rest.split("@{")[0]
            pid = rest.strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", pid):
                issues.error(
                    "seq-participant-id",
                    line,
                    f"participant id {pid!r} must be a single readable word",
                )
            elif re.fullmatch(r"[a-z][a-z0-9]?", pid):
                issues.warn(
                    "seq-cryptic-id",
                    line,
                    f"participant id {pid!r} renders as-is; use readable PascalCase",
                )
            participants.add(pid)
            continue
        m = _SEQ_MSG_RE.match(stmt)
        if not m:
            issues.warn("seq-unparsed", line, f"unrecognized statement {stmt[:40]!r}")
            continue
        messages += 1
        participants.update((m.group("a").strip(), m.group("b").strip()))
        if m.group("act"):
            issues.error("seq-activation", line, "`+`/`-` activation shorthand is not rendered")
        text = (m.group("text") or "").strip()
        if not text:
            issues.warn("seq-unlabeled", line, "message has no label")
        if ";" in (m.group("text") or "").rstrip().rstrip(";"):
            issues.warn("seq-semicolon", line, "semicolons in messages are rewritten to periods")
        if _ENTITY_CODE_RE.search(text):
            issues.warn("entity-code", line, "message contains an entity code")
    if len(participants) > int(plan["max_nodes"]):
        issues.warn("density", 0, f"{len(participants)} participants > {plan['max_nodes']}")
    if messages > int(plan["max_edges"]):
        issues.warn("density", 0, f"{messages} messages > {plan['max_edges']}; split the flow")
    if messages == 0:
        issues.error("seq-empty", 0, "sequence diagram has no messages")


# --- state ------------------------------------------------------------------------------------

_STATE_TRANSITION_RE = re.compile(r"^(?P<a>\S+?)\s*-->\s*(?P<b>[^\s:]+)\s*(?::(?P<text>.*))?$")


def _state(lines: list[str], start: int, plan: dict[str, Any], issues: _Issues) -> None:
    depth = 0
    states: set[str] = set()
    transitions = 0
    max_nesting = int(plan.get("max_nesting", 2))
    for line, stmt in _statements(lines, start):
        word = stmt.split()[0]
        low = word.lower()
        if low in ("classdef", "class", "style") or ":::" in stmt:
            issues.error(
                "state-styling", line, "styling in state diagrams creates phantom states in Figma"
            )
            continue
        if low == "note" or stmt.lower().startswith("end note"):
            issues.error("state-note", line, "notes create phantom states in Figma")
            continue
        if low == "direction" or stmt == "--":
            continue
        if stmt == "}":
            depth -= 1
            if depth < 0:
                issues.error("unbalanced-brace", line, "`}` without an open composite state")
                depth = 0
            continue
        if low == "state":
            m = re.match(r'^state\s+(?:"[^"]*"\s+as\s+)?(?P<id>[^\s{<]+)\s*(?P<rest>.*)$', stmt)
            if not m:
                issues.error("state-syntax", line, f"cannot parse {stmt[:40]!r}")
                continue
            states.add(m.group("id"))
            if m.group("rest").endswith("{"):
                depth += 1
                if depth > max_nesting:
                    issues.warn("nesting", line, f"composite nesting depth {depth} > {max_nesting}")
            continue
        if stmt == "[*]":
            issues.error("state-bare-marker", line, "`[*]` must be one side of a transition")
            continue
        m = _STATE_TRANSITION_RE.match(stmt)
        if m:
            transitions += 1
            for sid in (m.group("a"), m.group("b")):
                if sid != "[*]":
                    states.add(sid)
                    if "." in sid:
                        issues.error(
                            "state-cross-composite",
                            line,
                            f"{sid!r}: transition into another composite's child",
                        )
            continue
        if re.search(r"\s->\s|[^-]->[^>]", stmt):
            issues.error("state-arrow", line, "state transitions use `-->`")
            continue
        m = re.match(r"^(?P<id>[^\s:]+)\s*:", stmt)
        if m:
            states.add(m.group("id"))
            continue
        if re.fullmatch(r"[A-Za-z][\w]*", stmt):
            states.add(stmt)
            continue
        issues.warn("state-unparsed", line, f"unrecognized statement {stmt[:40]!r}")
    if depth > 0:
        issues.error("unbalanced-brace", 0, f"{depth} composite state(s) not closed")
    if len(states) > int(plan["max_nodes"]):
        issues.warn("density", 0, f"{len(states)} states > {plan['max_nodes']}")
    if transitions > int(plan["max_edges"]):
        issues.warn("density", 0, f"{transitions} transitions > {plan['max_edges']}")
    if transitions == 0:
        issues.error("state-empty", 0, "state diagram has no transitions")


# --- ERD -----------------------------------------------------------------------------------------

_ERD_LEFT = {"||", "|o", "}o", "}|"}
_ERD_RIGHT = {"||", "o|", "o{", "|{"}
_ERD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_ERD_ENTITY_RE = re.compile(
    r'^(?P<name>[^\s{\["]+)\s*(?P<alias>\[\s*"[^"]*"\s*\])?\s*(?P<open>\{)?\s*(?P<close>\})?$'
)
_ERD_REL_RE = re.compile(r"^(?P<a>\S+)\s+(?P<card>\S+)\s+(?P<b>\S+)\s*(?::\s*(?P<label>.*))?$")
_ERD_ATTR_RE = re.compile(
    r"^[A-Za-z_][\w\-\[\]()]*\s+\*?[A-Za-z_][\w\-\[\]().]*"
    r"(?:\s+(?:PK|FK|UK)(?:\s*,\s*(?:PK|FK|UK))*)?(?:\s+\"[^\"]*\")?$"
)


def _erd(lines: list[str], start: int, plan: dict[str, Any], issues: _Issues) -> None:
    entities: set[str] = set()
    relations = 0
    in_block = False

    def entity(name: str, line: int) -> None:
        entities.add(name)
        if not _ERD_NAME_RE.fullmatch(name):
            issues.error(
                "erd-entity-name", line, f"entity name {name!r} must match [A-Za-z_][A-Za-z0-9_-]*"
            )
        elif name.lower() in ERD_RESERVED:
            issues.error("reserved-id", line, f"entity name {name!r} is an ER keyword")

    for line, stmt in _statements(lines, start):
        word = stmt.split()[0]
        low = word.lower()
        if in_block:
            if stmt == "}":
                in_block = False
            elif not _ERD_ATTR_RE.match(stmt):
                issues.error(
                    "erd-attribute",
                    line,
                    f"bad attribute {stmt[:40]!r}; use `type name [PK|FK|UK]`",
                )
            continue
        if low in ("classdef", "class", "style") or ":::" in stmt:
            issues.warn("erd-styling", line, "ER styling is dropped by Figma")
            continue
        if low == "note":
            issues.error("erd-note", line, "ER diagrams have no notes")
            continue
        if low == "direction" or low == "title" or low.startswith("acc"):
            continue
        em = _ERD_ENTITY_RE.match(stmt)
        if em:
            entity(em.group("name"), line)
            if em.group("open") and not em.group("close"):
                in_block = True
            continue
        rm = _ERD_REL_RE.match(stmt)
        if rm and re.search(r"--|\.\.", rm.group("card")):
            relations += 1
            card = rm.group("card")
            a, b = rm.group("a"), rm.group("b")
            if "[" in a or "[" in b:
                issues.error(
                    "erd-alias-in-relation", line, "aliases are not allowed in relationship lines"
                )
            else:
                entity(a, line)
                entity(b, line)
            cm = re.fullmatch(r"(.{2})(--|\.\.)(.{2})", card)
            if not cm or cm.group(1) not in _ERD_LEFT or cm.group(3) not in _ERD_RIGHT:
                issues.error("erd-cardinality", line, f"invalid cardinality {card!r}")
            if rm.group("label") is None:
                issues.error("erd-relation-label", line, "relationship needs `: label`")
            elif not rm.group("label").strip():
                issues.warn("erd-relation-label", line, "relationship label is empty")
            continue
        issues.error("erd-syntax", line, f"cannot parse {stmt[:40]!r}")
    if in_block:
        issues.error("unbalanced-brace", 0, "entity block not closed with `}`")
    if len(entities) > int(plan["max_nodes"]):
        issues.warn("density", 0, f"{len(entities)} entities > {plan['max_nodes']}")
    if relations > int(plan["max_edges"]):
        issues.warn("density", 0, f"{relations} relationships > {plan['max_edges']}")
    if not entities:
        issues.error("erd-empty", 0, "ER diagram has no entities")


# --- entry point -------------------------------------------------------------------------------


def lint(text: str, renderer: str, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Check Mermaid text for `renderer`; see the module docstring for the issue format."""
    issues = _Issues()
    if renderer not in RENDERERS:
        issues.error("renderer", 0, f"unknown renderer {renderer!r}")
        return issues.items
    plan = _plan_config(config)
    lines = text.splitlines()
    _universal(lines, issues)
    start = _header(lines, renderer, issues)
    if start is not None:
        if renderer in ("flowchart", "architecture"):
            _flow_checks(_parse_flow(lines, start, plan, issues), renderer, plan, issues)
        elif renderer == "sequence":
            _sequence(lines, start, plan, issues)
        elif renderer == "state":
            _state(lines, start, plan, issues)
        else:
            _erd(lines, start, plan, issues)
    return sorted(issues.items, key=lambda x: (x["line"], x["severity"] != "error", x["rule"]))


def errors(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in issues if i["severity"] == "error"]
