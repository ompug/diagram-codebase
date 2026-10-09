from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path

import pytest
from diagram_codebase.common import write_json
from diagram_codebase.config import DEFAULTS
from diagram_codebase.state import manifest as mf
from diagram_codebase.state import update

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
CONFIG = copy.deepcopy(DEFAULTS)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    files = {
        "app/service.py": "def restock():\n    pass\n",
        "app/repo.py": "def save():\n    pass\n",
        "app/cli.py": "def main():\n    pass\n",
        "lib/util.py": "X = 1\n",
        "pyproject.toml": "[project]\nname='x'\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / ".gitignore").write_text(".diagram-codebase/\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    rev = git(root, "rev-parse", "HEAD")
    out = root / ".diagram-codebase"
    m = mf.new_manifest(repo_name="repo", repo_root=root, revision=rev, options={})
    mf.save(out, m)
    write_json(out / "inventory.json", {"files": [{"path": p} for p in files]})
    write_json(
        out / "model.json",
        {
            "subsystems": [
                {"id": "sub:app", "paths": ["app"]},
                {"id": "sub:lib", "paths": ["lib"]},
            ],
            "nodes": [
                {
                    "id": "fn:app/service.py:restock",
                    "subsystem": "sub:app",
                    "evidence_ids": ["ev:1"],
                },
                {"id": "fn:app/repo.py:save", "subsystem": "sub:app", "evidence_ids": ["ev:2"]},
                {"id": "fn:app/cli.py:main", "subsystem": "sub:app", "evidence_ids": ["ev:3"]},
                {"id": "mod:lib/util.py", "subsystem": "sub:lib", "evidence_ids": []},
            ],
            "edges": [
                {
                    "from": "fn:app/service.py:restock",
                    "to": "fn:app/repo.py:save",
                    "evidence_ids": ["ev:1"],
                },
            ],
            "evidence": [
                {"id": "ev:1", "file": "app/service.py"},
                {"id": "ev:2", "file": "app/repo.py"},
                {"id": "ev:3", "file": "app/cli.py"},
            ],
        },
    )
    write_json(out / "findings" / "scan.json", {"nodes": [{"evidence": [{"file": "app/cli.py"}]}]})
    write_json(
        out / "findings" / "execution.json",
        {"flows": [{"evidence": [{"file": "app/service.py", "lines": "1"}]}]},
    )
    write_json(
        out / "plan.json",
        {
            "diagrams": [
                {"id": "master", "model_nodes": ["fn:app/repo.py:save"]},
                {"id": "lib", "model_nodes": ["mod:lib/util.py"]},
            ]
        },
    )
    return root, out


def test_no_changes(repo):
    root, out = repo
    res = update.affected(out, root, CONFIG)
    assert res["recommendation"] == "none" and res["changed_count"] == 0


def test_partial_change(repo):
    root, out = repo
    (root / "app" / "service.py").write_text("def restock():\n    return 1\n")
    res = update.affected(out, root, CONFIG)
    assert res["changed"]["modified"] == ["app/service.py"]
    assert res["affected_nodes"] == ["fn:app/repo.py:save", "fn:app/service.py:restock"]
    assert res["affected_subsystems"] == ["sub:app"]
    assert res["findings_to_reanalyze"] == [
        {"file": "findings/execution.json", "cites": ["app/service.py"]}
    ]
    assert res["affected_diagrams"] == ["master"]
    assert res["recommendation"] == "partial"
    assert res["build_files_changed"] == []


def test_one_hop_neighbors(repo):
    root, out = repo
    (root / "app" / "repo.py").write_text("def save():\n    return 2\n")
    res = update.affected(out, root, CONFIG)
    assert res["affected_nodes"] == ["fn:app/repo.py:save"]
    assert res["neighbor_nodes"] == ["fn:app/service.py:restock"]
    assert res["churn"] == pytest.approx(0.2)


def test_high_churn_and_build_files(repo):
    root, out = repo
    for rel in ("app/service.py", "app/repo.py", "pyproject.toml"):
        (root / rel).write_text("# changed\n")
    res = update.affected(out, root, CONFIG)
    assert res["build_files_changed"] == ["pyproject.toml"]
    assert res["recommendation"] == "full" and res["churn"] == pytest.approx(0.6)


def test_added_and_deleted_files(repo):
    root, out = repo
    (root / "lib" / "util.py").unlink()
    (root / "app" / "new.py").write_text("Y = 2\n")
    res = update.affected(out, root, CONFIG)
    assert res["changed"]["deleted"] == ["lib/util.py"]
    assert res["changed"]["added"] == ["app/new.py"]
    assert "mod:lib/util.py" in res["affected_nodes"]
    assert set(res["affected_subsystems"]) == {"sub:app", "sub:lib"}


def test_no_revision_or_not_git(tmp_path):
    res = update.affected(tmp_path, tmp_path, CONFIG)
    assert res["recommendation"] == "full"
    m = mf.new_manifest(repo_name="x", repo_root=tmp_path, revision="deadbeef", options={})
    mf.save(tmp_path, m)
    res = update.affected(tmp_path, tmp_path, CONFIG)
    assert res["recommendation"] == "full" and "git" in res["reason"]


def test_build_file_patterns():
    assert update.is_build_file("svc/package.json")
    assert update.is_build_file("launch/robot.launch.py")
    assert update.is_build_file("docker-compose.prod.yml")
    assert not update.is_build_file("app/service.py")
