"""Tests for build/package manifest and deployment parsing."""

from __future__ import annotations

import pytest
from diagram_codebase.scan import manifests
from diagram_codebase.scan.manifests import _parse_compose, parse_manifests


def parse(make_repo, files):
    root = make_repo(files)
    return parse_manifests(root, [{"path": p} for p in sorted(files)])


def test_pyproject_dependencies_scripts_and_package(make_repo):
    m = parse(
        make_repo,
        {
            "pyproject.toml": '[project]\nname = "svc"\ndependencies = ["FastAPI>=0.1", "SQLAlchemy[asyncio]"]\n'
            '[project.scripts]\nsvc = "svc.cli:main"\n'
        },
    )
    assert m["build_systems"] == ["pyproject"] and m["package_managers"] == ["pip"]
    assert m["dependencies"]["python"] == ["fastapi", "sqlalchemy"]
    assert set(m["frameworks"]) == {"fastapi", "sqlalchemy"}
    ep = m["entry_points"][0]
    assert (ep["name"], ep["target"], ep["kind"], ep["line"]) == (
        "svc",
        "svc.cli:main",
        "console_script",
        5,
    )
    assert m["packages"] == [{"name": "svc", "path": ".", "kind": "python"}]


def test_toml_fallback_used_without_tomllib(make_repo, monkeypatch):
    """Python 3.10 has no tomllib; the small fallback must still read [project]."""
    monkeypatch.setattr(manifests, "tomllib", None)
    m = parse(
        make_repo,
        {
            "pyproject.toml": '[project]\nname = "svc"\ndependencies = [\n  "fastapi>=0.1",\n  "redis",\n]\n'
            '\n[project.scripts]\nsvc = "svc.cli:main"\n'
        },
    )
    assert m["dependencies"]["python"] == ["fastapi", "redis"]
    assert m["packages"][0]["name"] == "svc"
    assert m["entry_points"][0]["target"] == "svc.cli:main"


def test_poetry_and_requirements(make_repo):
    pytest.importorskip("tomllib")  # poetry tables need a real TOML parser (3.11+)
    m = parse(
        make_repo,
        {
            "pyproject.toml": '[tool.poetry]\nname = "x"\n[tool.poetry.dependencies]\npython = "^3.10"\ncelery = "*"\n',
            "requirements.txt": "# comment\nredis==5.0\n-r other.txt\ngit+https://x/y.git\npsycopg2-binary\n",
            "svc/requirements-dev.txt": "pytest\n",
        },
    )
    assert "poetry" in m["package_managers"] and "pip" in m["package_managers"]
    assert m["dependencies"]["python"] == ["celery", "psycopg2-binary", "pytest", "redis"]
    assert {"celery", "redis", "postgres"} <= set(m["frameworks"])


def test_setup_py_console_scripts(make_repo):
    m = parse(
        make_repo,
        {
            "setup.py": 'setup(entry_points={"console_scripts": ["talker = demo.talker:main"]},\n'
            '      install_requires=["rclpy", "numpy"])\n'
        },
    )
    assert m["entry_points"] == [
        {
            "name": "talker",
            "target": "demo.talker:main",
            "file": "setup.py",
            "line": 1,
            "kind": "console_script",
        }
    ]
    assert m["dependencies"]["python"] == ["numpy", "rclpy"]


def test_package_json_entry_points_and_frameworks(make_repo):
    m = parse(
        make_repo,
        {
            "web/package.json": '{"name": "web", "main": "./src/index.js", "bin": "cli.js",\n'
            ' "scripts": {"start": "node src/index.js", "test": "jest"},\n'
            ' "dependencies": {"express": "^4", "React": "^18"}, "devDependencies": {"jest": "1"}}\n',
            "node_modules/x/package.json": '{"name": "x"}',
            "bad/package.json": "{not json",
        },
    )
    kinds = {(e["kind"], e["name"], e["target"]) for e in m["entry_points"]}
    assert ("node_main", "main", "web/src/index.js") in kinds
    assert ("node_bin", "web", "web/cli.js") in kinds
    assert ("npm_script", "npm run start", "node src/index.js") in kinds
    assert m["dependencies"]["javascript"] == ["express", "jest", "react"]
    assert {"express", "react"} <= set(m["frameworks"])
    assert m["packages"] == [{"name": "web", "path": "web", "kind": "node"}]


def test_cmake_and_ros_package_xml(make_repo):
    m = parse(
        make_repo,
        {
            "ctl/CMakeLists.txt": "find_package(rclcpp REQUIRED)\nadd_library(core src/core.cpp)\n"
            "add_executable(controller src/controller.cpp src/util.hpp)\n",
            "ctl/package.xml": "<package><name>ctl</name><depend>rclcpp</depend>"
            "<export><build_type>ament_cmake</build_type></export></package>",
            "broken/package.xml": "<package><name>",
        },
    )
    assert {"cmake", "ament"} <= set(m["build_systems"])
    exe = next(e for e in m["entry_points"] if e["kind"] == "cmake_executable")
    assert (exe["name"], exe["target"], exe["line"]) == ("controller", "ctl/src/controller.cpp", 3)
    assert {"name": "core", "path": "ctl", "kind": "cmake_library"} in m["packages"]
    ros = next(p for p in m["packages"] if p["kind"] == "ros2_package")
    assert (ros["name"], ros["path"], ros["build_type"]) == ("ctl", "ctl", "ament_cmake")
    assert "ros2" in m["frameworks"]
    assert m["dependencies"]["cmake"] == ["rclcpp"] and m["dependencies"]["ros"] == ["rclcpp"]


def test_other_ecosystems(make_repo):
    m = parse(
        make_repo,
        {
            "go.mod": "module x\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n",
            "Cargo.toml": '[package]\nname = "r"\n[dependencies]\nserde = "1"\n',
            "pom.xml": "<project><artifactId>spring-boot-starter-web</artifactId></project>",
        },
    )
    assert m["dependencies"]["go"] == ["github.com/gin-gonic/gin"]
    assert m["dependencies"]["rust"] == ["serde"]
    assert "spring" in m["frameworks"]


def test_dockerfile_and_k8s(make_repo):
    m = parse(
        make_repo,
        {
            "api/Dockerfile": 'FROM python:3.12-slim\nEXPOSE 8000\nCMD ["uvicorn", "app:app"]\n',
            "deploy/k8s/api.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n",
        },
    )
    d = {x["name"]: x for x in m["deployments"]}
    assert d["api"]["kind"] in ("dockerfile", "k8s_deployment")
    docker = next(x for x in m["deployments"] if x["kind"] == "dockerfile")
    assert docker["image"] == "python:3.12-slim" and docker["ports"] == ["8000"]
    assert docker["command"].startswith("[")
    k8s = next(x for x in m["deployments"] if x["kind"] == "k8s_deployment")
    assert (k8s["name"], k8s["line"]) == ("api", 2)


COMPOSE = """\
# top comment
version: "3.9"
services:
  web:
    build: ./web
    ports: ["3000:3000", "3001:3001"]
    depends_on:
      - api
    environment:
      API_URL: http://api:8000
  api:
    build:
      context: ./api
    depends_on:
      db:
        condition: service_healthy
      cache:
        condition: service_started
    healthcheck:
      test: ["CMD", "true"]
    ports:
      - "8000:8000"
  db:
    image: "postgres:16"
  cache:
    image: redis:7
volumes:
  data:
"""


def test_compose_parsing():
    services = {s["name"]: s for s in _parse_compose(COMPOSE, "docker-compose.yml")}
    assert list(services) == ["web", "api", "db", "cache"]
    assert services["web"]["depends_on"] == ["api"]
    assert services["web"]["ports"] == ["3000:3000", "3001:3001"]
    assert services["web"]["build"] == "./web"
    assert services["api"]["depends_on"] == ["db", "cache"]  # long form, nested keys ignored
    assert services["api"]["ports"] == ["8000:8000"]
    assert services["db"]["image"] == "postgres:16"
    assert services["cache"]["image"] == "redis:7"
    assert services["web"]["line"] == 4 and services["db"]["line"] == 23


def test_compose_list_followed_by_sibling_key_does_not_leak():
    text = "services:\n  a:\n    depends_on:\n      - b\n    environment:\n      X: 1\n    labels:\n      - x\n  b:\n    image: busybox\n"
    a = _parse_compose(text, "compose.yaml")[0]
    assert a["depends_on"] == ["b"]


def test_compose_file_name_variants(make_repo):
    m = parse(
        make_repo,
        {
            "compose.yaml": "services:\n  a:\n    image: x\n",
            "docker-compose.prod.yml": "services:\n  b:\n    image: y\n",
        },
    )
    assert sorted(d["name"] for d in m["deployments"]) == ["a", "b"]
