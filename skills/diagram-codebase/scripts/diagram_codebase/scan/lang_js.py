"""JavaScript/TypeScript structural analysis (regex-based, no Node required).

Extracts modules, relative/alias imports, exported functions and components,
Express-style routes, Next.js file routes, fetch/axios API requests (matched to
endpoints by normalized method+path across languages), EventEmitter/Kafka/AMQP
style messaging, and simple ORM/state-store declarations. Import-only usage is
never reported as a call; calls to imported bindings are `static_inferred`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..model.builder import ModelBuilder
from .cpp_text import line_of, strip_comments
from .patterns_py import normalize_route

EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts")
IMPORT_RE = re.compile(
    r"""(?:^|\n)\s*import\s+(?:type\s+)?(?:([\w$]+)\s*,?\s*)?(?:\{([^}]*)\}|\*\s+as\s+([\w$]+))?\s*(?:from\s+)?['"]([^'"]+)['"]"""
)
REQUIRE_RE = re.compile(
    r"""(?:(?:const|let|var)\s+(?:([\w$]+)|\{([^}]*)\})\s*=\s*)?require\(\s*['"]([^'"]+)['"]\s*\)"""
)
DYN_IMPORT_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")
FUNC_RE = re.compile(
    r"""^(export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([\w$]+)\s*\(""", re.M
)
ARROW_RE = re.compile(
    r"""^(export\s+)?(?:const|let)\s+([\w$]+)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[\w$]+)\s*(?::[^=]+)?=>""",
    re.M,
)
CLASS_RE = re.compile(
    r"""^(export\s+)?(?:default\s+)?class\s+([\w$]+)(?:\s+extends\s+([\w$.]+))?""", re.M
)
ROUTE_RE = re.compile(
    r"""\b([\w$]+)\.(get|post|put|delete|patch|all)\(\s*['"`](/[^'"`]*)['"`]\s*,([^;]*?)\)\s*;?\s*$""",
    re.M,
)
FETCH_RE = re.compile(r"""\bfetch\(\s*(['"`])([^'"`]+)\1([^)]*)""")
AXIOS_RE = re.compile(
    r"""\b(?:axios|api|client|http)\.(get|post|put|delete|patch)\(\s*(['"`])([^'"`]+)\2"""
)
EMIT_RE = re.compile(r"""\.(emit|publish|sendToQueue|produce)\(\s*['"`]([\w:./-]+)['"`]""")
ON_RE = re.compile(
    r"""\.(on|once|subscribe|addListener|consume)\(\s*['"`]([\w:./-]+)['"`]\s*(?:,\s*([\w$.]+))?"""
)
KAFKA_SEND_RE = re.compile(
    r"""\.(send|subscribe)\(\s*\{\s*topics?\s*:\s*\[?\s*['"`]([\w:./-]+)['"`]"""
)
BUILTIN_EVENTS = {"data", "end", "error", "close", "finish", "connect", "connection", "open", "drain", "readable",
                  "exit", "listening", "request", "upgrade", "uncaughtException", "unhandledRejection", "SIGINT", "SIGTERM"}  # fmt: skip
MONGOOSE_RE = re.compile(r"""mongoose\.model\(\s*['"`](\w+)['"`]""")
PRISMA_RE = re.compile(
    r"""\bprisma\.(\w+)\.(findMany|findUnique|findFirst|create|update|delete|upsert|count|aggregate|createMany|updateMany|deleteMany)\b"""
)
SLICE_RE = re.compile(r"""createSlice\(\s*\{\s*name\s*:\s*['"`](\w+)['"`]""")
CONTEXT_RE = re.compile(r"""(?:const|let)\s+([\w$]+)\s*=\s*(?:React\.)?createContext\(""")
SQL_IN_JS_RE = re.compile(
    r"""\.(?:query|execute)\(\s*['"`]\s*(SELECT|INSERT|UPDATE|DELETE)[\s\S]*?(?:FROM|INTO|UPDATE)\s+(\w+)""",
    re.I,
)
NEXT_METHOD_RE = re.compile(
    r"""export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|DELETE|PATCH)\b"""
)


def _resolve(src: str, spec: str, paths: set[str]) -> str | None:
    if spec.startswith("."):
        base = src.rsplit("/", 1)[0] if "/" in src else ""
        cand = f"{base}/{spec}" if base else spec
    elif spec.startswith(("@/", "~/")):
        cand = spec[2:]
    else:
        return None
    parts: list[str] = []
    for p in cand.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p and p != ".":
            parts.append(p)
    norm = "/".join(parts)
    candidates = [norm] + [norm + e for e in EXTS] + [f"{norm}/index{e}" for e in EXTS]
    if spec.startswith(("@/", "~/")):
        candidates += ["src/" + c for c in candidates]
    for c in candidates:
        if c in paths:
            return c
    return None


def analyze_js(root: Path, files: list[dict[str, Any]], b: ModelBuilder) -> dict[str, Any]:
    jsfiles = [f for f in files if f["language"] in ("javascript", "typescript") and f["analyzed"]]
    paths = {f["path"] for f in jsfiles}
    exports: dict[str, dict[str, str]] = {}  # path -> {name: node id}
    texts: dict[str, str] = {}
    external: set[str] = set()
    requests: list[tuple[str, str, str, int, str]] = []  # (mod, method, path, line, file)

    for f in jsfiles:
        path = f["path"]
        try:
            raw = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code = strip_comments(raw, keep_strings=True, js=True)
        texts[path] = code
        mid = f"mod:{path}"
        is_ui = path.endswith((".jsx", ".tsx")) or bool(re.search(r"return\s*\(\s*<[A-Za-z]", code))
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
            tags=["ui"] if is_ui else [],
        )
        exports[path] = {}
        for m in FUNC_RE.finditer(code):
            _def(b, path, code, m, exports, is_ui)
        for m in ARROW_RE.finditer(code):
            _def(b, path, code, m, exports, is_ui)
        for m in CLASS_RE.finditer(code):
            ln = line_of(code, m.start())
            cid = f"cls:{path}:{m.group(2)}"
            cev = b.ev(path, ln, ln, symbol=m.group(2), detail="class definition", source="regex")
            b.node(
                cid,
                m.group(2),
                "class",
                parent=mid,
                evidence_ids=[cev],
                bases=[m.group(3)] if m.group(3) else [],
                file=path,
            )
            exports[path][m.group(2)] = cid

    for path, code in texts.items():
        mid = f"mod:{path}"
        bindings: dict[str, tuple[str, str]] = {}  # local name -> (target path, exported name)
        specs: list[tuple[str, int, dict[str, str]]] = []
        for m in IMPORT_RE.finditer(code):
            names: dict[str, str] = {}
            if m.group(1):
                names[m.group(1)] = "default"
            for part in (m.group(2) or "").split(","):
                part = part.strip()
                if not part:
                    continue
                orig, _, alias = part.partition(" as ")
                names[(alias or orig).strip()] = orig.strip().replace("type ", "")
            specs.append((m.group(4), line_of(code, m.start(4)), names))
        for m in REQUIRE_RE.finditer(code):
            names = {}
            if m.group(1):
                names[m.group(1)] = "default"
            for part in (m.group(2) or "").split(","):
                part = part.strip()
                if part:
                    orig, _, alias = part.partition(":")
                    names[(alias or orig).strip()] = orig.strip()
            specs.append((m.group(3), line_of(code, m.start(3)), names))
        for m in DYN_IMPORT_RE.finditer(code):
            specs.append((m.group(1), line_of(code, m.start(1)), {}))
        for spec, ln, names in specs:
            target = _resolve(path, spec, paths)
            if target is None:
                if not spec.startswith("."):
                    external.add(
                        spec.split("/")[0]
                        if not spec.startswith("@")
                        else "/".join(spec.split("/")[:2])
                    )
                continue
            iev = b.ev(path, ln, ln, symbol=spec, detail=f"import {spec}", source="regex")
            b.edge(mid, f"mod:{target}", "imports", evidence_ids=[iev], phase="init")
            for local, orig in names.items():
                bindings[local] = (target, orig)

        # Calls to imported bindings (static_inferred; enclosing function unknown -> module).
        for local, (target, orig) in bindings.items():
            tid = exports.get(target, {}).get(orig) or (
                exports.get(target, {}).get("default") if orig == "default" else None
            )
            if not tid or not tid.startswith("fn:"):
                continue
            m = re.search(
                r"(?<![\w$.])" + re.escape(local) + r"\s*\(|<" + re.escape(local) + r"[\s/>]", code
            )
            if m:
                ln = line_of(code, m.start())
                caller = _enclosing(b, path, ln) or mid
                kind = "calls"
                detail = "renders" if code[m.start()] == "<" else "call"
                cev = b.ev(
                    path,
                    ln,
                    ln,
                    symbol=local,
                    detail=f"{detail} {local}",
                    source="regex",
                    status="static_inferred",
                )
                b.edge(
                    caller,
                    tid,
                    kind,
                    evidence_ids=[cev],
                    label="renders" if detail == "renders" else "",
                )

        _routes(b, path, code, exports, bindings)
        for m in FETCH_RE.finditer(code):
            method = re.search(r"method\s*:\s*['\"`](\w+)", m.group(3))
            requests.append(
                (
                    path,
                    (method.group(1) if method else "GET").upper(),
                    m.group(2),
                    line_of(code, m.start()),
                    path,
                )
            )
        for m in AXIOS_RE.finditer(code):
            requests.append((path, m.group(1).upper(), m.group(3), line_of(code, m.start()), path))
        _messaging(b, path, code, exports)
        _data(b, path, code)

    for path, method, url, ln, file in requests:
        caller = _enclosing(b, path, ln) or f"mod:{path}"
        ev = b.ev(file, ln, ln, symbol=f"{method} {url}", detail="HTTP request", source="regex")
        if "://" in url and not url.startswith("${"):
            host = urlparse(url).hostname
            if host and host not in ("localhost", "127.0.0.1"):
                b.node(f"ext:{host}", host, "external_service", evidence_ids=[ev], protocol="http")
                b.edge(
                    caller,
                    f"ext:{host}",
                    "external_call",
                    evidence_ids=[ev],
                    label=method,
                    phase="runtime",
                )
                continue
            url = urlparse(url).path
        url = re.sub(r"^\$\{[^}]*\}", "", url)
        if not url.startswith("/"):
            continue
        norm = normalize_route(url)
        api = f"api:{method} {norm}"
        b.node(api, f"{method} {norm}", "api_endpoint", evidence_ids=[ev], method=method, path=norm)
        b.edge(caller, api, "api_request", evidence_ids=[ev], label=method, phase="runtime")

    return {"files": len(jsfiles), "external_packages": sorted(external)}


def _def(b: ModelBuilder, path: str, code: str, m: re.Match, exports: dict, is_ui: bool) -> None:
    name = m.group(2)
    ln = line_of(code, m.start())
    fid = f"fn:{path}:{name}"
    exported = (
        bool(m.group(1))
        or re.search(r"export\s+default\s+" + re.escape(name) + r"\b", code) is not None
    )
    component = is_ui and name[:1].isupper()
    ev = b.ev(path, ln, ln, symbol=name, detail="function definition", source="regex")
    tags = (["exported"] if exported else []) + (["ui-component"] if component else [])
    b.node(fid, f"{name}()" if not component else name, "ui_component" if component else "function",
           parent=f"mod:{path}", evidence_ids=[ev], tags=tags, file=path, line=ln)  # fmt: skip
    exports[path][name] = fid
    if re.search(
        r"export\s+default\s+(?:async\s+)?(?:function\s+)?" + re.escape(name) + r"\b", code
    ):
        exports[path]["default"] = fid


def _enclosing(b: ModelBuilder, path: str, line: int) -> str | None:
    best, best_line = None, 0
    for nid, n in b.nodes.items():
        if n["kind"] in ("function", "ui_component") and n["metadata"].get("file") == path:
            ln = n["metadata"].get("line", 0)
            if best_line < ln <= line:
                best, best_line = nid, ln
    return best


def _routes(b: ModelBuilder, path: str, code: str, exports: dict, bindings: dict) -> None:
    for m in ROUTE_RE.finditer(code):
        method, route, rest = m.group(2).upper(), m.group(3), m.group(4)
        if m.group(1) in (
            "axios",
            "api",
            "client",
            "http",
            "fetch",
            "map",
            "headers",
            "params",
            "searchParams",
        ):
            continue
        ln = line_of(code, m.start())
        norm = normalize_route(route)
        nid = f"api:{method} {norm}"
        ev = b.ev(
            path, ln, ln, symbol=f"{method} {route}", detail="route declaration", source="regex"
        )
        b.node(
            nid,
            f"{method} {norm}",
            "api_endpoint",
            evidence_ids=[ev],
            method=method,
            path=norm,
            framework="express",
        )
        handler_name = rest.strip().split(",")[-1].strip().split(".")[-1]
        handler = exports.get(path, {}).get(handler_name)
        if handler is None and handler_name in bindings:
            tgt, orig = bindings[handler_name]
            handler = exports.get(tgt, {}).get(orig)
        if handler:
            b.edge(nid, handler, "handles", evidence_ids=[ev], label="handled by", phase="runtime")
        else:
            b.edge(
                nid,
                f"mod:{path}",
                "handles",
                evidence_ids=[ev],
                label="inline handler",
                phase="runtime",
            )
    # Next.js file-system routes.
    m = re.match(r"(?:src/)?pages/api/(.+)\.(?:js|ts)$", path)
    n = re.match(r"(?:src/)?app/(.*/)?route\.(?:js|ts)$", path)
    if m or n:
        route = "/api/" + m.group(1) if m else "/" + (n.group(1) or "").rstrip("/")
        route = re.sub(r"\[\.{3}(\w+)\]|\[(\w+)\]", "{}", route).replace("/index", "")
        methods = NEXT_METHOD_RE.findall(code) or ["ANY"]
        for method in methods:
            nid = f"api:{method} {normalize_route(route)}"
            ev = b.ev(path, 1, 1, symbol=route, detail="Next.js route file", source="regex")
            b.node(
                nid,
                f"{method} {normalize_route(route)}",
                "api_endpoint",
                evidence_ids=[ev],
                method=method,
                path=normalize_route(route),
                framework="nextjs",
            )
            b.edge(
                nid,
                f"mod:{path}",
                "handles",
                evidence_ids=[ev],
                label="handled by",
                phase="runtime",
            )


def _messaging(b: ModelBuilder, path: str, code: str, exports: dict) -> None:
    for regex, role in ((EMIT_RE, "pub"), (ON_RE, "sub"), (KAFKA_SEND_RE, "kafka")):
        for m in regex.finditer(code):
            verb, name = m.group(1), m.group(2)
            if name in BUILTIN_EVENTS:
                continue
            ln = line_of(code, m.start())
            caller = _enclosing(b, path, ln) or f"mod:{path}"
            ev = b.ev(
                path, ln, ln, symbol=f"{verb}('{name}')", detail=f"{verb} '{name}'", source="regex"
            )
            chan = f"chan:{name}"
            b.node(
                chan,
                name,
                "event_channel",
                evidence_ids=[ev],
                transport="kafka" if role == "kafka" else verb,
            )
            publish = role == "pub" or (role == "kafka" and verb == "send")
            if publish:
                b.edge(caller, chan, "publishes", evidence_ids=[ev], label=verb, phase="runtime")
            else:
                handler = None
                if role == "sub" and m.lastindex and m.lastindex >= 3 and m.group(3):
                    handler = exports.get(path, {}).get(m.group(3).split(".")[-1])
                b.edge(
                    chan,
                    handler or caller,
                    "subscribes",
                    evidence_ids=[ev],
                    label="delivers to",
                    phase="runtime",
                )


def _data(b: ModelBuilder, path: str, code: str) -> None:
    for m in MONGOOSE_RE.finditer(code):
        ln = line_of(code, m.start())
        ev = b.ev(path, ln, ln, symbol=m.group(1), detail="mongoose model", source="regex")
        b.node(f"ent:{m.group(1)}", m.group(1), "db_entity", evidence_ids=[ev])
        b.node("store:mongodb", "MongoDB", "datastore", evidence_ids=[ev])
    for m in PRISMA_RE.finditer(code):
        ln = line_of(code, m.start())
        caller = _enclosing(b, path, ln) or f"mod:{path}"
        ev = b.ev(
            path,
            ln,
            ln,
            symbol=f"prisma.{m.group(1)}.{m.group(2)}",
            detail="Prisma query",
            source="regex",
        )
        b.node("store:prisma-db", "Database (Prisma)", "datastore", evidence_ids=[ev])
        write = m.group(2).startswith(("create", "update", "delete", "upsert"))
        b.edge(caller, "store:prisma-db", "db_write" if write else "db_read", evidence_ids=[ev], payload=[m.group(1)],
               label=("writes " if write else "reads ") + m.group(1), phase="runtime")  # fmt: skip
    for m in SQL_IN_JS_RE.finditer(code):
        ln = line_of(code, m.start())
        caller = _enclosing(b, path, ln) or f"mod:{path}"
        ev = b.ev(
            path, ln, ln, symbol=m.group(2), detail=f"SQL {m.group(1).upper()}", source="regex"
        )
        b.node("store:sql-database", "SQL database", "datastore", evidence_ids=[ev])
        write = m.group(1).upper() != "SELECT"
        b.edge(caller, "store:sql-database", "db_write" if write else "db_read", evidence_ids=[ev], payload=[m.group(2)],
               label=("writes " if write else "reads ") + m.group(2), phase="runtime")  # fmt: skip
    for m in SLICE_RE.finditer(code):
        ln = line_of(code, m.start())
        ev = b.ev(path, ln, ln, symbol=m.group(1), detail="Redux slice", source="regex")
        b.node(
            f"state:{m.group(1)}",
            f"{m.group(1)} store",
            "datastore",
            evidence_ids=[ev],
            tags=["client-state"],
            file=path,
        )
    for m in CONTEXT_RE.finditer(code):
        ln = line_of(code, m.start())
        ev = b.ev(path, ln, ln, symbol=m.group(1), detail="React context", source="regex")
        b.node(
            f"state:{m.group(1)}",
            m.group(1),
            "datastore",
            evidence_ids=[ev],
            tags=["client-state"],
            file=path,
        )
