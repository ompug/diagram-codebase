"""Python structural analysis with the standard-library `ast` module.

Extracts modules, classes, functions/methods, imports, inheritance, and
statically resolvable call sites. Call resolution is deliberately
conservative: a call becomes an edge only when the target can be bound to a
repository symbol through imports, local definitions, `self`, or a simple
`x = Cls(...)` type hint. Imports are recorded as `imports` edges and are never
treated as calls. Raw call records are kept for framework pattern matchers.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model.builder import ModelBuilder


@dataclass
class CallRecord:
    caller: str  # model id of enclosing function/module
    func: str  # dotted text of the callee expression, e.g. "self.create_publisher"
    attr: str  # final attribute / name, e.g. "create_publisher"
    args: list[Any]
    kwargs: dict[str, Any]
    line: int
    end_line: int
    file: str
    target: str | None = None  # resolved model id, when resolvable
    receiver_type: str | None = None  # resolved class id of the receiver, if known


@dataclass
class PyFile:
    path: str
    module: str
    module_id: str
    tree: ast.Module | None = None
    error: str | None = None
    imports: dict[str, str] = field(default_factory=dict)  # local alias -> dotted target
    external_imports: set[str] = field(default_factory=set)
    defs: dict[str, str] = field(default_factory=dict)  # qualname -> model id
    classes: dict[str, dict[str, Any]] = field(default_factory=dict)  # qualname -> info
    calls: list[CallRecord] = field(default_factory=list)
    decorators: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # fn id -> decos
    main_block: tuple[int, int] | None = None
    strings: list[tuple[str, int, str]] = field(default_factory=list)  # (text, line, caller)
    assigned_calls: list[tuple[str, str, int]] = field(
        default_factory=list
    )  # (var, func text, line)
    str_assigns: list[tuple[str, str, int, str]] = field(
        default_factory=list
    )  # (target, value, line, caller)
    env_reads: list[tuple[str, int, str]] = field(default_factory=list)  # (VAR, line, caller)


def literal(node: ast.AST | None) -> Any:
    """Best-effort literal rendering of an argument expression."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return (
            node.value
            if isinstance(node.value, (str, int, float, bool)) or node.value is None
            else None
        )
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return {"name": dotted(node)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [literal(e) for e in node.elts]
    if isinstance(node, ast.Dict):
        return {
            "dict": {
                str(literal(k)): literal(v) for k, v in zip(node.keys, node.values) if k is not None
            }
        }
    if isinstance(node, ast.Call):
        return {"call": dotted(node.func), "args": [literal(a) for a in node.args],
                "kwargs": {k.arg: literal(k.value) for k in node.keywords if k.arg}}  # fmt: skip
    if isinstance(node, ast.Subscript):
        return {"name": dotted(node)}
    return None


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func) + "()"
    if isinstance(node, ast.Subscript):
        return dotted(node.value)
    return ""


def module_name_for(rel: str, root: Path) -> str:
    """Dotted module name using __init__.py package boundaries."""
    parts = rel[:-3].split("/") if rel.endswith(".py") else rel.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1] or ["__init__"]
    # Walk up while the containing directory is a package.
    dirs = rel.split("/")[:-1]
    start = len(dirs)
    while start > 0 and (root / "/".join(dirs[:start]) / "__init__.py").exists():
        start -= 1
    return ".".join(parts[start:]) or parts[-1]


class _Visitor(ast.NodeVisitor):
    def __init__(self, pf: PyFile, b: ModelBuilder) -> None:
        self.pf = pf
        self.b = b
        self.scope: list[tuple[str, str]] = []  # (kind, qualname)
        self.local_types: list[dict[str, str]] = [{}]

    # helpers ----------------------------------------------------------------
    @property
    def caller(self) -> str:
        for kind, qual in reversed(self.scope):
            if kind == "function":
                return self.pf.defs[qual]
        return self.pf.module_id

    def _qual(self, name: str) -> str:
        quals = [q for k, q in self.scope if k in ("class", "function")]
        return f"{quals[-1]}.{name}" if quals else name

    # definitions --------------------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if any(k == "function" for k, _ in self.scope):
            return  # nested classes inside functions are implementation detail
        qual = self._qual(node.name)
        cid = f"cls:{self.pf.path}:{qual}"
        self.pf.defs[qual] = cid
        self.pf.classes[qual] = {
            "id": cid,
            "bases": [dotted(bs) for bs in node.bases],
            "attr_types": {},
            "line": node.lineno,
            "methods": [],
        }
        doc = ast.get_docstring(node) or ""
        ev = self.b.ev(
            self.pf.path, node.lineno, node.end_lineno, symbol=qual, detail="class definition"
        )
        parent = self.pf.defs.get(qual.rsplit(".", 1)[0]) if "." in qual else self.pf.module_id
        self.b.node(cid, node.name, "class", parent=parent or self.pf.module_id, evidence_ids=[ev],
                    description=doc.split("\n\n")[0][:300], symbols=[qual],
                    bases=[dotted(bs) for bs in node.bases], file=self.pf.path)  # fmt: skip
        self.scope.append(("class", qual))
        self.generic_visit(node)
        self.scope.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if any(k == "function" for k, _ in self.scope):
            # Nested function: attribute its calls to the enclosing function.
            self.generic_visit(node)
            return
        qual = self._qual(node.name)
        fid = f"fn:{self.pf.path}:{qual}"
        self.pf.defs[qual] = fid
        cls_scope = self.scope[-1][1] if self.scope and self.scope[-1][0] == "class" else None
        parent = self.pf.defs[cls_scope] if cls_scope else self.pf.module_id
        if cls_scope:
            self.pf.classes[cls_scope]["methods"].append(node.name)
        doc = ast.get_docstring(node) or ""
        args = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
        ev = self.b.ev(
            self.pf.path, node.lineno, node.end_lineno, symbol=qual, detail="function definition"
        )
        tags = ["async"] if isinstance(node, ast.AsyncFunctionDef) else []
        ret = ast.unparse(node.returns) if node.returns is not None else None
        self.b.node(fid, f"{qual}()", "function", parent=parent, evidence_ids=[ev], tags=tags,
                    description=doc.split("\n\n")[0][:300], symbols=[qual], inputs=args,
                    outputs=[ret] if ret else [], file=self.pf.path, line=node.lineno)  # fmt: skip
        decos = []
        for d in node.decorator_list:
            target = d.func if isinstance(d, ast.Call) else d
            decos.append({
                "name": dotted(target),
                "args": [literal(a) for a in d.args] if isinstance(d, ast.Call) else [],
                "kwargs": {k.arg: literal(k.value) for k in d.keywords if k.arg} if isinstance(d, ast.Call) else {},
                "line": d.lineno,
            })  # fmt: skip
        self.pf.decorators[fid] = decos
        self.scope.append(("function", qual))
        # Annotated parameters give receiver types (dependency-injection style).
        hinted = {}
        for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if a.annotation is not None and isinstance(a.annotation, (ast.Name, ast.Attribute)):
                hinted[a.arg] = dotted(a.annotation)
        self.local_types.append(hinted)
        self.generic_visit(node)
        self.local_types.pop()
        self.scope.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    # imports ----------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            target = alias.name if alias.asname else alias.name.split(".")[0]
            self.pf.imports[local] = target
            self.pf.imports.setdefault(f"__import__:{alias.name}", alias.name)
            self.pf.imports.setdefault(f"__line__:{alias.name}", str(node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            pkg_parts = self.pf.module.split(".")
            is_pkg = self.pf.path.endswith("__init__.py")
            keep = len(pkg_parts) - node.level + (1 if is_pkg else 0)
            prefix = ".".join(pkg_parts[: max(keep, 0)])
            base = ".".join(p for p in (prefix, base) if p)
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            self.pf.imports[local] = f"{base}.{alias.name}" if base else alias.name
        self.pf.imports.setdefault(f"__import__:{base}", base)
        self.pf.imports.setdefault(f"__line__:{base}", str(node.lineno))

    # calls ------------------------------------------------------------------
    def visit_Subscript(self, node: ast.Subscript) -> None:
        if dotted(node.value) == "os.environ":
            key = literal(node.slice)
            if isinstance(key, str):
                self.pf.env_reads.append((key, node.lineno, self.caller))
        self.generic_visit(node)

    def _record_attr_type(self, name: str, type_text: str) -> None:
        if name.startswith("self.") and name.count(".") == 1:
            cls = next((q for k, q in reversed(self.scope) if k == "class"), None)
            if cls:
                self.pf.classes[cls]["attr_types"].setdefault(name[5:], type_text)
        else:
            self.local_types[-1].setdefault(name, type_text)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.annotation, (ast.Name, ast.Attribute)):
            name = dotted(node.target)
            if name:
                self._record_attr_type(name, dotted(node.annotation))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in self.local_types[-1]:
            for tgt in node.targets:
                name = dotted(tgt)
                if name.startswith("self."):
                    self._record_attr_type(name, self.local_types[-1][node.value.id])
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for tgt in node.targets:
                name = dotted(tgt)
                if name:
                    self.pf.str_assigns.append((name, node.value.value, node.lineno, self.caller))
        if isinstance(node.value, ast.Call):
            func = dotted(node.value.func)
            for tgt in node.targets:
                name = dotted(tgt)
                if not name:
                    continue
                self.pf.assigned_calls.append((name, func, node.lineno))
                if name.startswith("self.") and name.count(".") == 1:
                    cls = next((q for k, q in reversed(self.scope) if k == "class"), None)
                    if cls:
                        self.pf.classes[cls]["attr_types"][name[5:]] = func
                else:
                    self.local_types[-1][name] = func
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = dotted(node.func)
        if func:
            attr = func.rsplit(".", 1)[-1].rstrip("()")
            cls = next((q for k, q in reversed(self.scope) if k == "class"), None)
            rec = CallRecord(
                caller=self.caller,
                func=func,
                attr=attr,
                args=[literal(a) for a in node.args],
                kwargs={k.arg: literal(k.value) for k in node.keywords if k.arg},
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno) or node.lineno,
                file=self.pf.path,
            )
            rec.__dict__["_class_scope"] = cls
            rec.__dict__["_local_types"] = dict(self.local_types[-1])
            self.pf.calls.append(rec)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        t = node.test
        if (
            not self.scope
            and isinstance(t, ast.Compare)
            and isinstance(t.left, ast.Name)
            and t.left.id == "__name__"
            and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in t.comparators)
        ):
            self.pf.main_block = (node.lineno, node.end_lineno or node.lineno)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and 6 <= len(node.value) <= 2000:
            self.pf.strings.append((node.value, node.lineno, self.caller))


@dataclass
class PyIndex:
    files: dict[str, PyFile]
    by_module: dict[str, PyFile]

    def resolve_callable(self, pf: PyFile, value: Any, class_scope: str | None) -> str | None:
        """Resolve a callback argument (e.g. `self.on_scan`, `handler`) to a function id."""
        if isinstance(value, dict) and "name" in value:
            text = value["name"]
        elif isinstance(value, dict) and value.get("call") == "partial" and value.get("args"):
            return self.resolve_callable(pf, value["args"][0], class_scope)
        else:
            return None
        if text.startswith("self.") and class_scope and "." not in text[5:]:
            return _method_of(pf.classes[class_scope]["id"], text[5:], self.by_module)
        target = _resolve_name(text, pf, self.by_module)
        return target if target and target.startswith("fn:") else None


def analyze_python(root: Path, files: list[dict[str, Any]], b: ModelBuilder) -> PyIndex:
    """Parse all analyzable Python files, add nodes/edges to `b`, return per-file facts."""
    pyfiles: dict[str, PyFile] = {}
    for f in files:
        if f["language"] != "python" or not f["analyzed"]:
            continue
        rel = f["path"]
        mod = module_name_for(rel, root)
        pf = PyFile(path=rel, module=mod, module_id=f"mod:{rel}")
        try:
            src = (root / rel).read_text(encoding="utf-8", errors="replace")
            pf.tree = ast.parse(src, filename=rel)
        except (SyntaxError, ValueError) as exc:
            pf.error = f"{type(exc).__name__}: {exc}"
        pyfiles[rel] = pf

    by_module: dict[str, PyFile] = {pf.module: pf for pf in pyfiles.values()}
    # Also index by full dotted path so namespace packages / scripts resolve.
    for pf in list(pyfiles.values()):
        full = pf.path[:-3].replace("/", ".")
        by_module.setdefault(full, pf)

    for pf in pyfiles.values():
        lines = 1
        if pf.tree is not None and pf.tree.body:
            lines = getattr(pf.tree.body[-1], "end_lineno", 1) or 1
        ev = b.ev(pf.path, 1, lines, symbol=pf.module, detail="module", source="ast")
        doc = ast.get_docstring(pf.tree) if pf.tree is not None else ""
        b.node(pf.module_id, pf.module, "module", evidence_ids=[ev], description=(doc or "").split("\n\n")[0][:300],
               file=pf.path, language="python", parse_error=pf.error)  # fmt: skip
        if pf.tree is not None:
            _Visitor(pf, b).visit(pf.tree)

    for pf in pyfiles.values():
        if pf.tree is None:
            continue
        _resolve_imports(pf, by_module, b)
        _resolve_inheritance(pf, by_module, b)
        _resolve_calls(pf, by_module, b)
        if pf.main_block:
            b.nodes[pf.module_id]["tags"] = sorted(
                set(b.nodes[pf.module_id]["tags"]) | {"entrypoint"}
            )
            b.nodes[pf.module_id]["metadata"]["main_block"] = list(pf.main_block)
    return PyIndex(pyfiles, by_module)


def _lookup_module(name: str, by_module: dict[str, PyFile]) -> tuple[PyFile | None, str]:
    """Longest internal-module prefix of `name`; returns (file, remainder)."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        pf = by_module.get(".".join(parts[:i]))
        if pf is not None:
            return pf, ".".join(parts[i:])
    return None, name


def _resolve_imports(pf: PyFile, by_module: dict[str, PyFile], b: ModelBuilder) -> None:
    for key, target in pf.imports.items():
        if not key.startswith("__import__:"):
            continue
        line = int(pf.imports.get(f"__line__:{target}", "1"))
        dst, rest = _lookup_module(target, by_module)
        # `from pkg import submodule` imports the submodule, not just the package.
        if dst is not None:
            ev = b.ev(pf.path, line, line, symbol=target, detail=f"import {target}")
            b.edge(pf.module_id, dst.module_id, "imports", evidence_ids=[ev], phase="init")
        elif target:
            pf.external_imports.add(target.split(".")[0])
    for local, target in pf.imports.items():
        if local.startswith("__"):
            continue
        dst = by_module.get(target)
        if dst is not None and dst is not pf:
            line = int(pf.imports.get(f"__line__:{target.rsplit('.', 1)[0]}", "1"))
            ev = b.ev(pf.path, line, line, symbol=target, detail=f"import {target}")
            b.edge(pf.module_id, dst.module_id, "imports", evidence_ids=[ev], phase="init")


def _symbol_in(pf: PyFile, qual: str) -> str | None:
    return pf.defs.get(qual)


def _resolve_name(name: str, pf: PyFile, by_module: dict[str, PyFile]) -> str | None:
    """Resolve a dotted name used in `pf` to a model id (function or class)."""
    if not name:
        return None
    head, _, rest = name.partition(".")
    # Local definition (function or class, or Class.method).
    local = _symbol_in(pf, name)
    if local:
        return local
    target = pf.imports.get(head)
    if target is None:
        return None
    full = f"{target}.{rest}" if rest else target
    mod, remainder = _lookup_module(full, by_module)
    if mod is None or not remainder:
        return None
    return _symbol_in(mod, remainder)


def _class_info(
    cid: str, pyfiles_by_id: dict[str, tuple[PyFile, str]]
) -> tuple[PyFile, str] | None:
    return pyfiles_by_id.get(cid)


def _resolve_inheritance(pf: PyFile, by_module: dict[str, PyFile], b: ModelBuilder) -> None:
    for qual, info in pf.classes.items():
        for base in info["bases"]:
            target = _resolve_name(base, pf, by_module)
            if target and target.startswith("cls:"):
                ev = b.ev(
                    pf.path, info["line"], info["line"], symbol=qual, detail=f"inherits {base}"
                )
                b.edge(info["id"], target, "inherits", evidence_ids=[ev])
                info.setdefault("resolved_bases", []).append(target)


def _method_of(
    class_id: str, method: str, by_module: dict[str, PyFile], depth: int = 0
) -> str | None:
    """Find `method` on class `class_id` or its internal bases."""
    if depth > 5 or not class_id.startswith("cls:"):
        return None
    _, path, qual = class_id.split(":", 2)
    owner = next((p for p in by_module.values() if p.path == path), None)
    if owner is None:
        return None
    hit = owner.defs.get(f"{qual}.{method}")
    if hit:
        return hit
    for base in owner.classes.get(qual, {}).get("resolved_bases", []):
        hit = _method_of(base, method, by_module, depth + 1)
        if hit:
            return hit
    return None


def _type_of(
    text: str | None, pf: PyFile, by_module: dict[str, PyFile], b: ModelBuilder
) -> str | None:
    """Class id for a constructor/annotation text, following a factory's return annotation."""
    if not text:
        return None
    target = _resolve_name(text, pf, by_module)
    if target and target.startswith("fn:"):
        outs = b.nodes.get(target, {}).get("outputs") or []
        owner_path = target.split(":", 2)[1]
        owner = next((p for p in by_module.values() if p.path == owner_path), None)
        if outs and owner is not None:
            target = _resolve_name(outs[0], owner, by_module)
    return target if target and target.startswith("cls:") else None


def _resolve_calls(pf: PyFile, by_module: dict[str, PyFile], b: ModelBuilder) -> None:
    for rec in pf.calls:
        func = rec.func
        if func.endswith("()"):
            continue  # chained call on a call result: receiver unknown
        cls_scope = rec.__dict__.get("_class_scope")
        local_types = rec.__dict__.get("_local_types", {})
        target: str | None = None
        status = "confirmed"
        if func.startswith("self.") and cls_scope:
            rest = func[5:]
            cls_id = pf.classes[cls_scope]["id"]
            if "." not in rest:
                target = _method_of(cls_id, rest, by_module)
            else:
                attr, _, meth = rest.partition(".")
                if "." not in meth:
                    typ = _type_of(pf.classes[cls_scope]["attr_types"].get(attr), pf, by_module, b)
                    if typ:
                        rec.receiver_type = typ
                        target = _method_of(typ, meth, by_module)
                        status = "static_inferred"
        elif func.startswith("super.") or func.startswith("super()."):
            continue
        else:
            head, _, meth = func.rpartition(".")
            if head and head in local_types and "." not in meth:
                typ = _type_of(local_types[head], pf, by_module, b)
                if typ:
                    rec.receiver_type = typ
                    target = _method_of(typ, meth, by_module)
                    status = "static_inferred"
            if target is None:
                target = _resolve_name(func, pf, by_module)
        if not target:
            continue
        rec.target = target
        ev = b.ev(
            pf.path, rec.line, rec.end_line, symbol=func, detail=f"call {func}", status=status
        )
        caller_is_init = rec.caller.endswith(".__init__") or rec.caller.startswith("mod:")
        phase = "init" if caller_is_init else "unknown"
        if target.startswith("cls:"):
            b.edge(rec.caller, target, "instantiates", evidence_ids=[ev], phase=phase)
        else:
            b.edge(rec.caller, target, "calls", evidence_ids=[ev], phase=phase)
