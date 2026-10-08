"""Tests for read-only git helpers."""

from __future__ import annotations

from diagram_codebase.scan.gitinfo import changed_files, git_info, sanitize_remote


def test_sanitize_remote_strips_credentials():
    assert (
        sanitize_remote("https://user:tok_FAKE@github.com/o/r.git") == "https://github.com/o/r.git"
    )
    assert sanitize_remote("https://tok_FAKE@host/x") == "https://host/x"
    assert sanitize_remote("git@github.com:o/r.git") == "git@github.com:o/r.git"
    assert sanitize_remote(None) is None
    assert sanitize_remote("") is None


def test_git_info_outside_git(tmp_path):
    info = git_info(tmp_path)
    assert info == {
        "is_git": False,
        "revision": None,
        "short": None,
        "branch": None,
        "dirty": None,
        "remote": None,
    }


def test_git_info_clean_then_dirty(make_repo, git_init):
    root = git_init(make_repo({"a.py": "x = 1\n"}))
    git_init.git(root, "remote", "add", "origin", "https://bot:ghp_FAKE@example.com/o/r.git")
    info = git_info(root)
    assert info["is_git"] and len(info["revision"]) == 40
    assert info["short"] == info["revision"][:10]
    assert info["branch"] == "main"
    assert info["dirty"] is False
    assert info["remote"] == "https://example.com/o/r.git"
    (root / "a.py").write_text("x = 2\n")
    assert git_info(root)["dirty"] is True


def test_changed_files_reports_added_modified_deleted_untracked(make_repo, git_init):
    root = git_init(make_repo({"keep.py": "1\n", "gone.py": "1\n", "edit.py": "1\n"}))
    base = git_info(root)["revision"]
    (root / "gone.py").unlink()
    (root / "edit.py").write_text("2\n")
    (root / "new.py").write_text("3\n")
    git_init.git(root, "add", "-A")
    git_init.git(root, "commit", "-q", "-m", "change")
    (root / "untracked.py").write_text("4\n")
    ch = changed_files(root, base)
    assert ch["deleted"] == ["gone.py"]
    assert ch["modified"] == ["edit.py"]
    assert sorted(ch["added"]) == ["new.py", "untracked.py"]


def test_changed_files_unknown_revision_returns_none(make_repo, git_init):
    root = git_init(make_repo({"a.py": "1\n"}))
    assert changed_files(root, "deadbeef" * 5) is None
    assert changed_files(root.parent / "nope", "HEAD") is None
