"""Build/package manifest and deployment-definition parsing (static, read-only)."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    tomllib = None  # type: ignore[assignment]

# dependency name (lowercase, normalized) -> framework tag
FRAMEWORK_DEPS = {
    "fastapi": "fastapi",
    "flask": "flask",
    "django": "django",
    "starlette": "starlette",
    "aiohttp": "aiohttp",
    "tornado": "tornado",
    "celery": "celery",
    "kafka-python": "kafka",
    "confluent-kafka": "kafka",
    "aiokafka": "kafka",
    "pika": "rabbitmq",
    "aio-pika": "rabbitmq",
    "paho-mqtt": "mqtt",
    "redis": "redis",
    "sqlalchemy": "sqlalchemy",
    "psycopg2": "postgres",
    "psycopg2-binary": "postgres",
    "psycopg": "postgres",
    "pymongo": "mongodb",
    "torch": "pytorch",
    "tensorflow": "tensorflow",
    "scikit-learn": "sklearn",
    "jax": "jax",
    "numpy": "numpy",
    "opencv-python": "opencv",
    "rclpy": "ros2",
    "rclcpp": "ros2",
    "express": "express",
    "koa": "koa",
    "fastify": "fastify",
    "@nestjs/core": "nestjs",
    "next": "nextjs",
    "react": "react",
    "vue": "vue",
    "svelte": "svelte",
    "@angular/core": "angular",
    "redux": "redux",
    "@reduxjs/toolkit": "redux",
    "zustand": "zustand",
    "mongoose": "mongodb",
    "prisma": "prisma",
    "@prisma/client": "prisma",
    "pg": "postgres",
    "kafkajs": "kafka",
    "amqplib": "rabbitmq",
    "socket.io": "websocket",
    "ws": "websocket",
    "spring-boot-starter-web": "spring",
    "spring-boot-starter": "spring",
}


def _norm(name: str) -> str:
    return re.split(r"[\s<>=!~;\[\(]", name.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _line_of_key(text: str, key: str) -> int:
    """Line of a TOML-style `key = ...` declaration (falls back to the first mention)."""
    m = re.search(r"^\s*[\"']?" + re.escape(key) + r"[\"']?\s*=", text, re.M)
    return text.count("\n", 0, m.start()) + 1 if m else _line_of(text, key)


def _line_of(text: str, needle: str) -> int:
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 1


def parse_manifests(root: Path, files: list[dict[str, Any]]) -> dict[str, Any]:
    """Return build systems, package managers, deps, entry points, deployments."""
    out: dict[str, Any] = {
        "build_systems": [],
        "package_managers": [],
        "dependencies": {},  # ecosystem -> sorted names
        "frameworks": [],
        "entry_points": [],  # {name, target, file, line, kind}
        "deployments": [],  # {name, kind, file, line, image, ports, depends_on}
        "packages": [],  # internal packages/targets {name, path, kind}
        "manifest_files": [],
    }
    deps: dict[str, set[str]] = {}

    def add(lst: str, value: str) -> None:
        if value not in out[lst]:
            out[lst].append(value)

    for f in files:
        rel = f["path"]
        name = rel.rsplit("/", 1)[-1]
        path = root / rel
        if name == "pyproject.toml":
            out["manifest_files"].append(rel)
            add("build_systems", "pyproject")
            text = _read(path)
            data = _load_toml(text)
            proj = data.get("project", {}) if isinstance(data, dict) else {}
            poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data, dict) else {}
            if poetry:
                add("package_managers", "poetry")
            else:
                add("package_managers", "pip")
            names = [_norm(d) for d in proj.get("dependencies", []) if isinstance(d, str)]
            names += [_norm(k) for k in (poetry.get("dependencies") or {}) if k != "python"]
            deps.setdefault("python", set()).update(names)
            scripts = {**(proj.get("scripts") or {}), **(poetry.get("scripts") or {})}
            for script, target in scripts.items():
                out["entry_points"].append(
                    {
                        "name": script,
                        "target": str(target),
                        "file": rel,
                        "line": _line_of_key(text, script),
                        "kind": "console_script",
                    }
                )
            if proj.get("name"):
                out["packages"].append(
                    {
                        "name": proj["name"],
                        "path": rel.rsplit("/", 1)[0] if "/" in rel else ".",
                        "kind": "python",
                    }
                )
        elif name in ("requirements.txt", "requirements-dev.txt") or (
            name.startswith("requirements") and name.endswith(".txt")
        ):
            out["manifest_files"].append(rel)
            add("package_managers", "pip")
            for line in _read(path).splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith(("-", "git+", "http")):
                    deps.setdefault("python", set()).add(_norm(line))
        elif name == "setup.py":
            out["manifest_files"].append(rel)
            add("build_systems", "setuptools")
            text = _read(path)
            for m in re.finditer(r"['\"]([\w.-]+)\s*=\s*([\w.]+):(\w+)['\"]", text):
                out["entry_points"].append(
                    {"name": m.group(1), "target": f"{m.group(2)}:{m.group(3)}", "file": rel,
                     "line": text.count("\n", 0, m.start()) + 1, "kind": "console_script"}
                )  # fmt: skip
            for m in re.finditer(r"install_requires\s*=\s*\[([^\]]*)\]", text, re.S):
                deps.setdefault("python", set()).update(
                    _norm(x) for x in re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
                )
        elif name == "package.json" and "node_modules" not in rel:
            out["manifest_files"].append(rel)
            text = _read(path)
            try:
                pkg = json.loads(text)
            except json.JSONDecodeError:
                continue
            add("package_managers", "npm")
            d = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
            deps.setdefault("javascript", set()).update(k.lower() for k in d)
            base = rel.rsplit("/", 1)[0] if "/" in rel else "."
            if pkg.get("name"):
                out["packages"].append({"name": pkg["name"], "path": base, "kind": "node"})
            for field in ("main", "module"):
                if isinstance(pkg.get(field), str):
                    out["entry_points"].append(
                        {
                            "name": field,
                            "target": _join(base, pkg[field]),
                            "file": rel,
                            "line": _line_of(text, f'"{field}"'),
                            "kind": "node_main",
                        }
                    )
            bins = pkg.get("bin")
            if isinstance(bins, str):
                bins = {pkg.get("name", "bin"): bins}
            for bname, target in (bins or {}).items():
                out["entry_points"].append(
                    {
                        "name": bname,
                        "target": _join(base, target),
                        "file": rel,
                        "line": _line_of(text, '"bin"'),
                        "kind": "node_bin",
                    }
                )
            for sname in ("start", "dev", "serve"):
                cmd = (pkg.get("scripts") or {}).get(sname)
                if cmd:
                    out["entry_points"].append(
                        {
                            "name": f"npm run {sname}",
                            "target": cmd,
                            "file": rel,
                            "line": _line_of(text, f'"{sname}"'),
                            "kind": "npm_script",
                        }
                    )
        elif name == "CMakeLists.txt":
            out["manifest_files"].append(rel)
            add("build_systems", "cmake")
            text = _read(path)
            base = rel.rsplit("/", 1)[0] if "/" in rel else "."
            for m in re.finditer(r"add_executable\s*\(\s*([\w${}.-]+)\s+([^)]*)\)", text):
                srcs = [s for s in m.group(2).split() if re.search(r"\.(c|cc|cpp|cxx)$", s)]
                out["entry_points"].append(
                    {"name": m.group(1), "target": " ".join(_join(base, s) for s in srcs), "file": rel,
                     "line": text.count("\n", 0, m.start()) + 1, "kind": "cmake_executable"}
                )  # fmt: skip
            for m in re.finditer(r"add_library\s*\(\s*([\w${}.-]+)", text):
                out["packages"].append({"name": m.group(1), "path": base, "kind": "cmake_library"})
            for m in re.finditer(r"find_package\s*\(\s*([\w-]+)", text):
                deps.setdefault("cmake", set()).add(m.group(1).lower())
        elif name == "package.xml":
            out["manifest_files"].append(rel)
            text = _read(path)
            try:
                tree = ET.fromstring(text)
            except ET.ParseError:
                continue
            pkg_name = (tree.findtext("name") or "").strip()
            build_type = (tree.findtext("export/build_type") or "").strip()
            add(
                "build_systems",
                "ament" if build_type.startswith("ament") or not build_type else build_type,
            )
            add("package_managers", "rosdep")
            base = rel.rsplit("/", 1)[0] if "/" in rel else "."
            if pkg_name:
                out["packages"].append(
                    {
                        "name": pkg_name,
                        "path": base,
                        "kind": "ros2_package",
                        "build_type": build_type,
                    }
                )
            for tag in ("depend", "exec_depend", "build_depend"):
                for el in tree.findall(tag):
                    if el.text:
                        deps.setdefault("ros", set()).add(el.text.strip().lower())
        elif name == "go.mod":
            out["manifest_files"].append(rel)
            add("build_systems", "go")
            for m in re.finditer(r"^\s*([\w./-]+)\s+v[\w.+-]+", _read(path), re.M):
                deps.setdefault("go", set()).add(m.group(1).lower())
        elif name in ("pom.xml", "build.gradle", "build.gradle.kts"):
            out["manifest_files"].append(rel)
            add("build_systems", "maven" if name == "pom.xml" else "gradle")
            text = _read(path)
            for m in re.finditer(
                r"<artifactId>([\w.-]+)</artifactId>|['\"][\w.-]+:([\w.-]+):", text
            ):
                deps.setdefault("java", set()).add((m.group(1) or m.group(2)).lower())
        elif name == "Cargo.toml":
            out["manifest_files"].append(rel)
            add("build_systems", "cargo")
            data = _load_toml(_read(path))
            deps.setdefault("rust", set()).update(
                k.lower() for k in (data.get("dependencies") or {})
            )
        elif name == "Dockerfile" or name.endswith(".Dockerfile") or name.startswith("Dockerfile."):
            out["manifest_files"].append(rel)
            text = _read(path)
            base_image = re.search(r"^\s*FROM\s+(\S+)", text, re.M | re.I)
            ports = re.findall(r"^[ \t]*EXPOSE[ \t]+([\d \t/a-z]+)", text, re.M | re.I)
            cmd = re.search(r"^\s*(?:CMD|ENTRYPOINT)\s+(.+)$", text, re.M | re.I)
            out["deployments"].append(
                {"name": rel.rsplit("/", 2)[-2] if "/" in rel else "app", "kind": "dockerfile", "file": rel, "line": 1,
                 "image": base_image.group(1) if base_image else None, "ports": " ".join(ports).split(),
                 "command": cmd.group(1).strip() if cmd else None, "depends_on": []}
            )  # fmt: skip
        elif re.fullmatch(r"(docker-)?compose(\.[\w-]+)?\.ya?ml", name):
            out["manifest_files"].append(rel)
            out["deployments"].extend(_parse_compose(_read(path), rel))
        elif name.endswith((".yaml", ".yml")) and (
            "k8s" in rel or "kubernetes" in rel or "deploy" in rel or "helm" in rel
        ):
            text = _read(path)
            for m in re.finditer(
                r"^kind:\s*(Deployment|StatefulSet|DaemonSet|CronJob|Job|Service)\s*$", text, re.M
            ):
                nm = re.search(r"^\s{2}name:\s*([\w.-]+)", text[m.end() :], re.M)
                out["deployments"].append(
                    {"name": nm.group(1) if nm else rel, "kind": f"k8s_{m.group(1).lower()}", "file": rel,
                     "line": text.count("\n", 0, m.start()) + 1, "image": None, "ports": [], "depends_on": []}
                )  # fmt: skip

    out["dependencies"] = {k: sorted(v) for k, v in sorted(deps.items())}
    frameworks = set()
    for names in deps.values():
        for n in names:
            if n in FRAMEWORK_DEPS:
                frameworks.add(FRAMEWORK_DEPS[n])
    if any(p.get("kind") == "ros2_package" for p in out["packages"]):
        frameworks.add("ros2")
    out["frameworks"] = sorted(frameworks)
    return out


def _join(base: str, rel: str) -> str:
    rel = rel.lstrip("./") if rel.startswith("./") else rel
    return rel if base in (".", "") else f"{base}/{rel}"


def _load_toml(text: str) -> dict:
    if tomllib is None:
        return _toml_fallback(text)
    try:
        return tomllib.loads(text)
    except Exception:
        return {}


def _toml_fallback(text: str) -> dict:
    """Very small TOML subset for Python 3.10 (no tomllib).

    Covers [project] name/dependencies/scripts and the keys of a [dependencies]
    table (Cargo). Quoted strings may contain brackets, e.g. "pkg[extra]".
    """
    data: dict = {"project": {}}
    m = re.search(r"^name\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
    if m:
        data["project"]["name"] = m.group(1)
    table = re.search(r"^\[dependencies\]\s*\n((?:[^\[\n].*\n?)*)", text, re.M)
    if table:
        data["dependencies"] = dict.fromkeys(
            re.findall(r"^([\w.-]+)\s*=", table.group(1), re.M), ""
        )
    m = re.search(r"""^dependencies\s*=\s*\[((?:[^\]"']|"[^"]*"|'[^']*')*)\]""", text, re.M | re.S)
    if m:
        data["project"]["dependencies"] = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    m = re.search(r"^\[project\.scripts\]\s*\n((?:[^\[\n].*\n?)*)", text, re.M)
    if m:
        data["project"]["scripts"] = dict(
            re.findall(r"^([\w.-]+)\s*=\s*['\"]([^'\"]+)['\"]", m.group(1), re.M)
        )
    return data


def _parse_compose(text: str, rel: str) -> list[dict[str, Any]]:
    """Indentation-based extraction of compose services (no YAML dependency)."""
    services: list[dict[str, Any]] = []
    lines = text.splitlines()
    in_services = False
    svc_indent = None
    current: dict[str, Any] | None = None
    key_ctx = None
    key_indent = 0  # indentation of the `ports:` / `depends_on:` key being collected
    depends_indent: int | None = None
    for i, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0:
            in_services = stripped.startswith("services:")
            current = None
            continue
        if not in_services:
            continue
        if svc_indent is None:
            svc_indent = indent
        if indent == svc_indent and stripped.endswith(":"):
            current = {"name": stripped[:-1].strip("'\""), "kind": "compose_service", "file": rel, "line": i,
                       "image": None, "ports": [], "depends_on": [], "build": None}  # fmt: skip
            services.append(current)
            key_ctx = None
            continue
        if current is None:
            continue
        if key_ctx and indent <= key_indent:
            key_ctx = None  # a sibling key ends the list/mapping being collected
        if stripped.startswith("image:"):
            current["image"] = stripped.split(":", 1)[1].strip().strip("'\"")
        elif stripped.startswith("build:"):
            current["build"] = stripped.split(":", 1)[1].strip().strip("'\"") or "."
        elif stripped.startswith(("ports:", "depends_on:")):
            key_ctx = stripped[:-1] if stripped.endswith(":") else None
            key_indent = indent
            depends_indent = None
            inline = stripped.split(":", 1)[1].strip()
            if inline.startswith("["):
                vals = [v.strip().strip("'\"") for v in inline.strip("[]").split(",") if v.strip()]
                current["ports" if stripped.startswith("ports") else "depends_on"].extend(vals)
                key_ctx = None
        elif stripped.startswith("- ") and key_ctx in ("ports", "depends_on"):
            current[key_ctx].append(stripped[2:].strip().strip("'\""))
        elif key_ctx == "depends_on" and stripped.endswith(":"):
            # Long form: `db:` then `condition: ...` nested deeper; only the first level names services.
            if depends_indent is None or indent == depends_indent:
                depends_indent = indent
                current["depends_on"].append(stripped[:-1].strip().strip("'\""))
        elif stripped.endswith(":") and not stripped.startswith("-"):
            key_ctx = None
    return services
