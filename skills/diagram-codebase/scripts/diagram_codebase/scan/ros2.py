"""ROS 2 computation-graph extraction without a ROS installation.

Recognizes rclpy (via the Python AST call records), rclcpp (regex over C++ with
comments/strings handled), Python and XML launch files, setup.py console
scripts, and CMake executables. Produces ros_node, topic, ros_service,
ros_action, tf_frame and parameter (config) nodes. Topic names from launch
remappings are applied to the declaring node's edges.
"""

from __future__ import annotations

import ast
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..model.builder import ModelBuilder
from .cpp_text import line_of, strip_comments
from .lang_python import PyFile, PyIndex, dotted, literal

MSG_TYPE_RE = r"[\w:]+"


def norm_topic(name: str, ns: str = "") -> str:
    if not name:
        return name
    if name.startswith(("/", "~")):
        return name
    ns = ns.strip("/")
    return f"/{ns}/{name}" if ns else f"/{name}"


class Ros2Extractor:
    def __init__(self, root: Path, b: ModelBuilder) -> None:
        self.root = root
        self.b = b
        self.node_by_file: dict[str, list[dict[str, Any]]] = {}  # file -> [{id, line, class}]
        self.found = False

    # ---------------------------------------------------------------- common
    def ros_node(self, name: str, file: str, line: int, cls_id: str | None, lang: str) -> str:
        nid = f"ros:{name}"
        ev = self.b.ev(
            file,
            line,
            line,
            symbol=name,
            detail="ROS 2 node declaration",
            source="ast" if lang == "python" else "regex",
        )
        self.b.node(
            nid,
            name,
            "ros_node",
            evidence_ids=[ev],
            implementation=cls_id,
            file=file,
            language=lang,
        )
        self.node_by_file.setdefault(file, []).append({"id": nid, "line": line, "class": cls_id})
        self.found = True
        return nid

    def owner(self, file: str, line: int) -> str | None:
        nodes = sorted(self.node_by_file.get(file, []), key=lambda n: n["line"])
        if not nodes:
            return None
        best = nodes[0]
        for n in nodes:
            if n["line"] <= line:
                best = n
        return best["id"]

    def _iface(
        self, kind: str, name: str, file: str, line: int, msg: str | None, src: str
    ) -> tuple[str, str]:
        node_kind, prefix = {
            "topic": ("topic", "topic"),
            "service": ("ros_service", "srv"),
            "action": ("ros_action", "action"),
        }[kind]
        tname = norm_topic(name)
        ev = self.b.ev(file, line, line, symbol=f"{name}", detail=f"{kind} declaration", source=src)
        nid = f"{prefix}:{tname}"
        meta = {"interface_types": [msg]} if msg else {}
        n = self.b.node(nid, tname, node_kind, evidence_ids=[ev], **meta)
        if msg and msg not in n["metadata"].setdefault("interface_types", []):
            n["metadata"]["interface_types"].append(msg)
        return nid, ev

    # ---------------------------------------------------------------- python
    def from_python(self, idx: PyIndex) -> None:
        for pf in idx.files.values():
            if pf.tree is None or not any(v.startswith("rclpy") for v in pf.imports.values()):
                continue
            self._py_nodes(pf)
            pub_attrs: dict[str, str] = {}
            for rec in pf.calls:
                owner = self.owner(pf.path, rec.line)
                if owner is None:
                    continue
                cls_scope = rec.__dict__.get("_class_scope")
                a, kw = rec.args, rec.kwargs
                msg = _type_name(a[0]) if a else None
                if rec.attr == "create_publisher" and len(a) >= 2 and isinstance(a[1], str):
                    tid, ev = self._iface("topic", a[1], pf.path, rec.line, msg, "ast")
                    self.b.edge(
                        owner,
                        tid,
                        "publishes",
                        evidence_ids=[ev],
                        label=msg or "",
                        payload=[msg] if msg else [],
                        phase="runtime",
                    )
                    var = next((v for v, f, ln in pf.assigned_calls if ln == rec.line), None)
                    if var:
                        pub_attrs[var] = tid
                elif rec.attr == "create_subscription" and len(a) >= 2 and isinstance(a[1], str):
                    tid, ev = self._iface("topic", a[1], pf.path, rec.line, msg, "ast")
                    self.b.edge(
                        tid,
                        owner,
                        "subscribes",
                        evidence_ids=[ev],
                        label=msg or "",
                        payload=[msg] if msg else [],
                        phase="runtime",
                    )
                    cb = idx.resolve_callable(
                        pf, a[2] if len(a) > 2 else kw.get("callback"), cls_scope
                    )
                    if cb:
                        self.b.edge(
                            tid,
                            cb,
                            "triggers",
                            evidence_ids=[ev],
                            label="callback",
                            phase="runtime",
                        )
                elif rec.attr == "create_service" and len(a) >= 2 and isinstance(a[1], str):
                    sid, ev = self._iface("service", a[1], pf.path, rec.line, msg, "ast")
                    self.b.edge(
                        owner,
                        sid,
                        "service_provide",
                        evidence_ids=[ev],
                        label="serves",
                        phase="runtime",
                    )
                    cb = idx.resolve_callable(
                        pf, a[2] if len(a) > 2 else kw.get("callback"), cls_scope
                    )
                    if cb:
                        self.b.edge(
                            sid,
                            cb,
                            "triggers",
                            evidence_ids=[ev],
                            label="request handler",
                            phase="runtime",
                        )
                elif rec.attr == "create_client" and len(a) >= 2 and isinstance(a[1], str):
                    sid, ev = self._iface("service", a[1], pf.path, rec.line, msg, "ast")
                    self.b.edge(
                        owner,
                        sid,
                        "service_call",
                        evidence_ids=[ev],
                        label="calls",
                        phase="runtime",
                    )
                elif (
                    rec.attr in ("ActionServer", "ActionClient")
                    and len(a) >= 3
                    and isinstance(a[2], str)
                ):
                    msg = _type_name(a[1])
                    aid, ev = self._iface("action", a[2], pf.path, rec.line, msg, "ast")
                    if rec.attr == "ActionServer":
                        self.b.edge(
                            owner,
                            aid,
                            "action_provide",
                            evidence_ids=[ev],
                            label="serves",
                            phase="runtime",
                        )
                        cb = idx.resolve_callable(
                            pf, a[3] if len(a) > 3 else kw.get("execute_callback"), cls_scope
                        )
                        if cb:
                            self.b.edge(
                                aid,
                                cb,
                                "triggers",
                                evidence_ids=[ev],
                                label="execute",
                                phase="runtime",
                            )
                    else:
                        self.b.edge(
                            owner,
                            aid,
                            "action_call",
                            evidence_ids=[ev],
                            label="sends goal",
                            phase="runtime",
                        )
                elif rec.attr == "create_timer" and len(a) >= 2:
                    cb = idx.resolve_callable(pf, a[1], cls_scope)
                    if cb:
                        period = a[0] if isinstance(a[0], (int, float)) else None
                        ev = self.b.ev(
                            pf.path,
                            rec.line,
                            rec.line,
                            symbol="create_timer",
                            detail="timer",
                            source="ast",
                        )
                        label = f"timer {period}s" if period is not None else "timer"
                        self.b.edge(
                            owner, cb, "triggers", evidence_ids=[ev], label=label, phase="runtime"
                        )
                elif rec.attr == "declare_parameter" and a and isinstance(a[0], str):
                    self._param(owner, a[0], pf.path, rec.line, a[1] if len(a) > 1 else None, "ast")
                elif rec.attr in ("TransformBroadcaster", "StaticTransformBroadcaster"):
                    tid, ev = self._iface(
                        "topic",
                        "/tf_static" if "Static" in rec.attr else "/tf",
                        pf.path,
                        rec.line,
                        "tf2_msgs/TFMessage",
                        "ast",
                    )
                    self.b.edge(
                        owner,
                        tid,
                        "publishes",
                        evidence_ids=[ev],
                        label="transforms",
                        phase="runtime",
                    )
                elif (
                    rec.attr == "lookup_transform"
                    and len(a) >= 2
                    and isinstance(a[0], str)
                    and isinstance(a[1], str)
                ):
                    self._tf(a[1], a[0], pf.path, rec.line, "lookup", "ast", owner)
                elif rec.attr == "publish" and rec.func.rsplit(".", 1)[0] in pub_attrs:
                    tid = pub_attrs[rec.func.rsplit(".", 1)[0]]
                    ev = self.b.ev(
                        pf.path,
                        rec.line,
                        rec.line,
                        symbol=rec.func,
                        detail="publish call",
                        source="ast",
                    )
                    if rec.caller.startswith("fn:"):
                        self.b.edge(
                            rec.caller,
                            tid,
                            "publishes",
                            evidence_ids=[ev],
                            label="publish",
                            phase="runtime",
                        )
            self._py_tf_assignments(pf)

    def _py_nodes(self, pf: PyFile) -> None:
        for qual, info in pf.classes.items():
            if not any(bs.split(".")[-1] in ("Node", "LifecycleNode") for bs in info["bases"]):
                continue
            name = None
            line = info["line"]
            for rec in pf.calls:
                if (
                    rec.func in ("super().__init__", "Node.__init__", "super.__init__")
                    and rec.__dict__.get("_class_scope") == qual
                ):
                    name = (
                        rec.args[0]
                        if rec.args and isinstance(rec.args[0], str)
                        else rec.kwargs.get("node_name")
                    )
                    line = rec.line
                    break
            self.ros_node(
                name if isinstance(name, str) else qual.split(".")[-1],
                pf.path,
                line,
                info["id"],
                "python",
            )
        for rec in pf.calls:
            if (
                rec.func in ("rclpy.create_node", "create_node")
                and rec.args
                and isinstance(rec.args[0], str)
            ):
                self.ros_node(rec.args[0], pf.path, rec.line, None, "python")

    def _py_tf_assignments(self, pf: PyFile) -> None:
        frames: list[tuple[str, str, int]] = []
        for target, value, line, _caller in pf.str_assigns:
            if target.endswith("header.frame_id"):
                frames.append(("parent", value, line))
            elif target.endswith("child_frame_id"):
                frames.append(("child", value, line))
        self._pair_frames(frames, pf.path, "ast")

    def _pair_frames(self, frames: list[tuple[str, str, int]], file: str, src: str) -> None:
        pending_parent: tuple[str, int] | None = None
        pending_child: tuple[str, int] | None = None
        for role, value, line in frames:
            if role == "parent":
                pending_parent = (value, line)
            else:
                pending_child = (value, line)
            if pending_parent and pending_child and abs(pending_parent[1] - pending_child[1]) <= 15:
                self._tf(
                    pending_parent[0],
                    pending_child[0],
                    file,
                    min(pending_parent[1], pending_child[1]),
                    "broadcast",
                    src,
                    self.owner(file, line),
                )
                pending_parent = pending_child = None

    def _tf(
        self, parent: str, child: str, file: str, line: int, how: str, src: str, owner: str | None
    ) -> None:
        ev = self.b.ev(
            file, line, line, symbol=f"{parent}->{child}", detail=f"tf {how}", source=src
        )
        p = f"tf:{parent.lstrip('/')}"
        c = f"tf:{child.lstrip('/')}"
        self.b.node(p, parent.lstrip("/"), "tf_frame", evidence_ids=[ev])
        self.b.node(c, child.lstrip("/"), "tf_frame", evidence_ids=[ev])
        if how == "broadcast":
            self.b.edge(
                p,
                c,
                "tf_transform",
                evidence_ids=[ev],
                label=f"by {owner.split(':', 1)[1]}" if owner else "",
                phase="runtime",
                broadcaster=owner,
            )
        else:
            self.b.edge(
                p,
                c,
                "tf_transform",
                evidence_ids=[ev],
                label="looked up",
                phase="runtime",
                consumer=owner,
            )

    def _param(self, owner: str, name: str, file: str, line: int, default: Any, src: str) -> None:
        ev = self.b.ev(file, line, line, symbol=name, detail="parameter declaration", source=src)
        pid = f"param:{owner.split(':', 1)[1]}/{name}"
        self.b.node(
            pid,
            name,
            "config",
            evidence_ids=[ev],
            source="ros_parameter",
            default=default if isinstance(default, (str, int, float, bool)) else None,
        )
        self.b.edge(
            owner, pid, "config_dependency", evidence_ids=[ev], label="parameter", phase="init"
        )

    # ---------------------------------------------------------------- C++
    def from_cpp(self, files: list[dict[str, Any]]) -> None:
        for f in files:
            if f["language"] not in ("cpp", "c") or not f["analyzed"]:
                continue
            try:
                raw = (self.root / f["path"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "rclcpp" not in raw:
                continue
            code = strip_comments(raw, keep_strings=True)
            path = f["path"]
            # Node declarations: `: Node("name")`, `rclcpp::Node("name")`, make_shared<rclcpp::Node>("name")
            for m in re.finditer(r"(?:rclcpp::)?Node\s*(?:>\s*)?\(\s*\"([\w/~-]+)\"", code):
                cls = None
                before = code[: m.start()]
                cm = list(
                    re.finditer(
                        r"class\s+(\w+)\s*(?:final\s*)?:\s*public\s+rclcpp(?:_lifecycle)?::\w*Node",
                        before,
                    )
                )
                if cm:
                    cls = f"cls:{path}:{cm[-1].group(1)}"
                self.ros_node(
                    m.group(1),
                    path,
                    line_of(code, m.start()),
                    cls if cls in self.b.nodes else None,
                    "cpp",
                )
            if not self.node_by_file.get(path):
                continue
            for m in re.finditer(
                r"create_publisher\s*<\s*(" + MSG_TYPE_RE + r")\s*>\s*\(\s*\"([^\"]+)\"", code
            ):
                ln = line_of(code, m.start())
                tid, ev = self._iface("topic", m.group(2), path, ln, _cpp_type(m.group(1)), "regex")
                self.b.edge(
                    self.owner(path, ln),
                    tid,
                    "publishes",
                    evidence_ids=[ev],
                    label=_cpp_type(m.group(1)),
                    payload=[_cpp_type(m.group(1))],
                    phase="runtime",
                )
            for m in re.finditer(
                r"create_subscription\s*<\s*(" + MSG_TYPE_RE + r")\s*>\s*\(\s*\"([^\"]+)\"([^;]*)",
                code,
            ):
                ln = line_of(code, m.start())
                tid, ev = self._iface("topic", m.group(2), path, ln, _cpp_type(m.group(1)), "regex")
                owner = self.owner(path, ln)
                self.b.edge(
                    tid,
                    owner,
                    "subscribes",
                    evidence_ids=[ev],
                    label=_cpp_type(m.group(1)),
                    payload=[_cpp_type(m.group(1))],
                    phase="runtime",
                )
                self._cpp_callback(m.group(3), path, tid, ev, "callback")
            for m in re.finditer(
                r"create_service\s*<\s*(" + MSG_TYPE_RE + r")\s*>\s*\(\s*\"([^\"]+)\"([^;]*)", code
            ):
                ln = line_of(code, m.start())
                sid, ev = self._iface(
                    "service", m.group(2), path, ln, _cpp_type(m.group(1)), "regex"
                )
                self.b.edge(
                    self.owner(path, ln),
                    sid,
                    "service_provide",
                    evidence_ids=[ev],
                    label="serves",
                    phase="runtime",
                )
                self._cpp_callback(m.group(3), path, sid, ev, "request handler")
            for m in re.finditer(
                r"create_client\s*<\s*(" + MSG_TYPE_RE + r")\s*>\s*\(\s*\"([^\"]+)\"", code
            ):
                ln = line_of(code, m.start())
                sid, ev = self._iface(
                    "service", m.group(2), path, ln, _cpp_type(m.group(1)), "regex"
                )
                self.b.edge(
                    self.owner(path, ln),
                    sid,
                    "service_call",
                    evidence_ids=[ev],
                    label="calls",
                    phase="runtime",
                )
            for m in re.finditer(
                r"rclcpp_action::create_(server|client)\s*<\s*("
                + MSG_TYPE_RE
                + r")\s*>\s*\([^\"]*\"([^\"]+)\"",
                code,
            ):
                ln = line_of(code, m.start())
                aid, ev = self._iface(
                    "action", m.group(3), path, ln, _cpp_type(m.group(2)), "regex"
                )
                kind = "action_provide" if m.group(1) == "server" else "action_call"
                self.b.edge(
                    self.owner(path, ln),
                    aid,
                    kind,
                    evidence_ids=[ev],
                    label="serves" if kind == "action_provide" else "sends goal",
                    phase="runtime",
                )
            for m in re.finditer(r"create_wall_timer\s*\(\s*([^,]+),([^;]*)", code):
                ln = line_of(code, m.start())
                cb = re.search(r"&\w+::(\w+)", m.group(2))
                if cb:
                    fid = self._cpp_method(path, cb.group(1))
                    if fid:
                        ev = self.b.ev(
                            path, ln, ln, symbol="create_wall_timer", detail="timer", source="regex"
                        )
                        self.b.edge(
                            self.owner(path, ln),
                            fid,
                            "triggers",
                            evidence_ids=[ev],
                            label=f"timer {m.group(1).strip()}",
                            phase="runtime",
                        )
            for m in re.finditer(r"declare_parameter\s*(?:<[^>]+>)?\s*\(\s*\"([\w.]+)\"", code):
                ln = line_of(code, m.start())
                self._param(self.owner(path, ln), m.group(1), path, ln, None, "regex")
            for m in re.finditer(r"lookup_transform\s*\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"", code):
                ln = line_of(code, m.start())
                self._tf(m.group(2), m.group(1), path, ln, "lookup", "regex", self.owner(path, ln))
            for m in re.finditer(r"(Static)?TransformBroadcaster", code):
                ln = line_of(code, m.start())
                tid, ev = self._iface(
                    "topic",
                    "/tf_static" if m.group(1) else "/tf",
                    path,
                    ln,
                    "tf2_msgs/TFMessage",
                    "regex",
                )
                self.b.edge(
                    self.owner(path, ln),
                    tid,
                    "publishes",
                    evidence_ids=[ev],
                    label="transforms",
                    phase="runtime",
                )
                break
            frames = [
                ("parent", m.group(1), line_of(code, m.start()))
                for m in re.finditer(r"header\.frame_id\s*=\s*\"([^\"]+)\"", code)
            ]
            frames += [
                ("child", m.group(1), line_of(code, m.start()))
                for m in re.finditer(r"child_frame_id\s*=\s*\"([^\"]+)\"", code)
            ]
            self._pair_frames(sorted(frames, key=lambda x: x[2]), path, "regex")

    def _cpp_method(self, path: str, method: str) -> str | None:
        stem = path.rsplit(".", 1)[0]
        for nid, n in self.b.nodes.items():
            if (
                n["kind"] == "function"
                and nid.endswith(f".{method}")
                and n["metadata"].get("file", "").rsplit(".", 1)[0] == stem
            ):
                return nid
        for nid, n in self.b.nodes.items():
            if n["kind"] == "function" and nid.endswith(f".{method}"):
                return nid
        return None

    def _cpp_callback(self, rest: str, path: str, src_id: str, ev: str, label: str) -> None:
        cb = re.search(r"&\w+::(\w+)", rest)
        if cb:
            fid = self._cpp_method(path, cb.group(1))
            if fid:
                self.b.edge(
                    src_id, fid, "triggers", evidence_ids=[ev], label=label, phase="runtime"
                )

    # ---------------------------------------------------------------- launch
    def from_launch(
        self, files: list[dict[str, Any]], manifests: dict[str, Any], idx: PyIndex | None
    ) -> None:
        exe_to_nodes = self._executable_map(manifests, idx)
        for f in files:
            path = f["path"]
            name = path.rsplit("/", 1)[-1]
            if name.endswith((".launch.py", "_launch.py")) or (
                path.split("/")[-2:-1] == ["launch"] and name.endswith(".py")
            ):
                self._launch_py(path, exe_to_nodes)
            elif name.endswith((".launch.xml", ".launch")) or (
                path.split("/")[-2:-1] == ["launch"] and name.endswith(".xml")
            ):
                self._launch_xml(path, exe_to_nodes)

    def _executable_map(
        self, manifests: dict[str, Any], idx: PyIndex | None
    ) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for ep in manifests.get("entry_points", []):
            targets: list[str] = []
            if ep["kind"] == "console_script" and idx is not None:
                mod = ep["target"].split(":")[0]
                pf = idx.by_module.get(mod)
                if pf is None:
                    pf = next(
                        (
                            p
                            for m, p in idx.by_module.items()
                            if m.endswith("." + mod.split(".")[-1])
                        ),
                        None,
                    )
                if pf is not None:
                    targets = [n["id"] for n in self.node_by_file.get(pf.path, [])]
            elif ep["kind"] == "cmake_executable":
                for src in ep["target"].split():
                    targets += [n["id"] for n in self.node_by_file.get(src, [])]
            if targets:
                out.setdefault(ep["name"], []).extend(targets)
        return out

    def _launch_unit(self, path: str) -> str:
        ev = self.b.ev(
            path,
            1,
            1,
            symbol=path.rsplit("/", 1)[-1],
            detail="launch file",
            source="ast" if path.endswith(".py") else "regex",
        )
        lid = f"launch:{path}"
        self.b.node(
            lid, path.rsplit("/", 1)[-1], "deployment_unit", evidence_ids=[ev], launch_file=True
        )
        self.found = True
        return lid

    def _apply_launch(self, path: str, line: int, exe: str | None, name: str | None, ns: str, remaps: list[tuple[str, str]],
                      exe_to_nodes: dict[str, list[str]], src: str) -> None:  # fmt: skip
        targets = list(exe_to_nodes.get(exe or "", []))
        if name and f"ros:{name}" in self.b.nodes:
            targets = [f"ros:{name}"]
        lid = self._launch_unit(path)
        ev = self.b.ev(path, line, line, symbol=exe or name or "", detail="launch node", source=src)
        if not targets:
            self.b.nodes[lid]["metadata"].setdefault("unresolved_executables", []).append(
                exe or name
            )
            return
        for nid in targets:
            self.b.edge(
                lid,
                nid,
                "launches",
                evidence_ids=[ev],
                label=f"ns {ns}" if ns else "",
                phase="init",
            )
            if name:
                self.b.nodes[nid]["metadata"].setdefault("launch_names", []).append(name)
            for old, new in remaps:
                self._remap(nid, old, new, ns, ev)

    def _remap(self, nid: str, old: str, new: str, ns: str, ev: str) -> None:
        old_id = None
        for prefix in ("topic", "srv", "action"):
            cand = f"{prefix}:{norm_topic(old)}"
            if cand in self.b.nodes:
                old_id = cand
                break
        if old_id is None:
            return
        prefix = old_id.split(":", 1)[0]
        new_id = f"{prefix}:{norm_topic(new, ns)}"
        old_node = self.b.nodes[old_id]
        self.b.node(
            new_id,
            norm_topic(new, ns),
            old_node["kind"],
            evidence_ids=[ev],
            **{k: v for k, v in old_node["metadata"].items()},
        )
        for e in list(self.b.edges.values()):
            if nid not in (e["from"], e["to"]) or old_id not in (e["from"], e["to"]):
                continue
            del self.b.edges[e["id"]]
            src = new_id if e["from"] == old_id else e["from"]
            dst = new_id if e["to"] == old_id else e["to"]
            self.b.edge(src, dst, e["kind"], label=e["label"], payload=e["payload"], phase=e["phase"],
                        evidence_ids=[*e["evidence_ids"], ev], remapped_from=old_id)  # fmt: skip
        # Callback edges hang off the topic; move those whose callback belongs to this node.
        impl = self.b.nodes[nid]["metadata"].get("implementation") or ""
        cls_prefix = impl.replace("cls:", "fn:", 1) + "."
        for e in list(self.b.edges.values()):
            if e["from"] == old_id and e["kind"] == "triggers" and e["to"].startswith(cls_prefix):
                del self.b.edges[e["id"]]
                self.b.edge(
                    new_id,
                    e["to"],
                    "triggers",
                    label=e["label"],
                    evidence_ids=[*e["evidence_ids"], ev],
                    phase=e["phase"],
                )
        still_used = any(old_id in (e["from"], e["to"]) for e in self.b.edges.values())
        if not still_used:
            self.b.nodes.pop(old_id, None)

    def _launch_py(self, path: str, exe_to_nodes: dict[str, list[str]]) -> None:
        try:
            tree = ast.parse((self.root / path).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError, ValueError):
            return
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or dotted(call.func).split(".")[-1] not in (
                "Node",
                "LifecycleNode",
                "ComposableNode",
            ):
                continue
            kw = {k.arg: literal(k.value) for k in call.keywords if k.arg}
            exe = kw.get("executable") or kw.get("plugin")
            name = kw.get("name")
            ns = kw.get("namespace") if isinstance(kw.get("namespace"), str) else ""
            remaps = []
            for item in kw.get("remappings") or []:
                if (
                    isinstance(item, list)
                    and len(item) == 2
                    and all(isinstance(x, str) for x in item)
                ):
                    remaps.append((item[0], item[1]))
            self._apply_launch(path, call.lineno, exe if isinstance(exe, str) else None,
                               name if isinstance(name, str) else None, ns or "", remaps, exe_to_nodes, "ast")  # fmt: skip

    def _launch_xml(self, path: str, exe_to_nodes: dict[str, list[str]]) -> None:
        text = (self.root / path).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ET.fromstring(text)
        except ET.ParseError:
            return
        for el in tree.iter("node"):
            exe = el.get("exec") or el.get("type")
            name = el.get("name")
            ns = el.get("namespace") or el.get("ns") or ""
            remaps = [
                (r.get("from"), r.get("to"))
                for r in el.iter("remap")
                if r.get("from") and r.get("to")
            ]
            needle = f'exec="{exe}"' if exe else (f'name="{name}"' if name else "<node")
            idx = text.find(needle)
            line = text.count("\n", 0, idx) + 1 if idx >= 0 else 1
            self._apply_launch(path, line, exe, name, ns, remaps, exe_to_nodes, "regex")


def _type_name(v: Any) -> str | None:
    if isinstance(v, dict) and "name" in v:
        return v["name"].split(".")[-1]
    return None


def _cpp_type(t: str) -> str:
    parts = [p for p in t.split("::") if p]
    if len(parts) >= 3 and parts[-2] in ("msg", "srv", "action"):
        return f"{parts[0]}/{parts[-1]}"
    return parts[-1] if parts else t
