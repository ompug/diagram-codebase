"""Shared pytest fixtures: fixture repositories, scan+merge runner, generated trees."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def scan_and_merge(
    root: Path, out: Path, findings: list[Path] | tuple[Path, ...] = (), config: dict | None = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Run the deterministic scan, copy Claude findings in, merge. Returns (scan, model, report)."""
    from diagram_codebase.config import deep_merge, load_config
    from diagram_codebase.model.merge import merge_all
    from diagram_codebase.scan import run_scan

    cfg = deep_merge(load_config(None), config or {})
    result = run_scan(root, out, cfg)
    for f in findings:
        shutil.copy(f, out / "findings" / Path(f).name)
    model, report = merge_all(root, out)
    return result, model, report


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def scan_fixture(tmp_path: Path):
    """`scan_fixture("b_web_app")` scans a fixture in place (git ls-files path).

    `copy=True` first copies it outside any git work tree (filesystem-walk path).
    """

    def run(name: str, findings: tuple[str, ...] = (), copy: bool = False, config=None):
        root = FIXTURES / name
        if copy:
            dst = tmp_path / "repo" / Path(name).name
            shutil.copytree(root, dst)
            root = dst
        out = tmp_path / "out"
        return scan_and_merge(root, out, [FIXTURES / f for f in findings], config)

    return run


def write_files(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def make_repo(tmp_path: Path):
    """`make_repo({"a.py": "..."})` writes a small repository under tmp_path and returns it."""
    counter = {"n": 0}

    def make(files: dict[str, str], name: str | None = None) -> Path:
        counter["n"] += 1
        root = tmp_path / (name or f"repo{counter['n']}")
        root.mkdir(parents=True, exist_ok=True)
        return write_files(root, files)

    return make


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


@pytest.fixture
def git_init():
    """`git_init(root, commit=True)` turns a directory into a git repo (skips without git)."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")

    def init(root: Path, commit: bool = True) -> Path:
        _git(root, "init", "-q", "-b", "main")
        if commit:
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "init")
        return root

    init.git = _git  # type: ignore[attr-defined]
    return init


LARGE_TREE_FILES = 2000


@pytest.fixture
def large_tree(tmp_path: Path) -> Path:
    """~2,000 source files across nested dirs plus node_modules/ and build/ noise to exclude.

    Generated at test time so it is never committed.
    """
    root = tmp_path / "large"
    per_dir = 20
    for i in range(LARGE_TREE_FILES):
        d = root / "pkg" / f"area{i // 400}" / f"mod{(i // per_dir) % 20}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    for noise in (
        "node_modules/left-pad",
        "frontend/node_modules/react/lib",
        "build/lib",
        "pkg/build",
    ):
        d = root / noise
        d.mkdir(parents=True, exist_ok=True)
        for j in range(50):
            (d / f"n{j}.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (root / "debug.log").write_text("noise\n", encoding="utf-8")
    return root
