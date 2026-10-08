"""Tests for the repository inventory (gitignore via git and via the walk fallback)."""

from __future__ import annotations

import pytest
from diagram_codebase.config import DEFAULTS, deep_merge
from diagram_codebase.scan.inventory import (
    _IgnoreMatcher,
    build_inventory,
    find_repo_root,
    is_test_path,
)

GITIGNORE = """\
# comment
*.log
secrets/
/root_only.py
!keep.log
docs/**/draft.md
"""

FILES = {
    ".gitignore": GITIGNORE,
    "app/main.py": "print('hi')\n",
    "app/util.js": "export const x = 1;\n",
    "debug.log": "x\n",
    "keep.log": "x\n",
    "secrets/key.py": "KEY = 'sk_test_FAKE'\n",
    "root_only.py": "x = 1\n",
    "nested/root_only.py": "x = 1\n",
    "docs/a/b/draft.md": "draft\n",
    "docs/final.md": "final\n",
    "node_modules/pkg/index.js": "module.exports = 1;\n",
    "build/out.py": "x = 1\n",
    "app/__pycache__/m.py": "x = 1\n",
    "assets/logo.png": "not really a png\n",
    "package-lock.json": "{}\n",
    "gen/api_pb2.py": "x = 1\n",
    "tests/test_main.py": "def test(): pass\n",
    "main.go": "package main\n",
    "lib.rs": "fn main() {}\n",
    "query.sql": "select 1;\n",
    "sub/.gitignore": "local.py\n",
    "sub/local.py": "x = 1\n",
    "sub/other.py": "x = 1\n",
}

EXPECTED = {
    ".gitignore",
    "app/main.py",
    "app/util.js",
    "keep.log",
    "nested/root_only.py",
    "docs/final.md",
    "tests/test_main.py",
    "main.go",
    "lib.rs",
    "query.sql",
    "sub/.gitignore",
    "sub/other.py",
}


def cfg(**scan):
    return deep_merge(DEFAULTS, {"scan": scan})


def paths(inv):
    return {f["path"] for f in inv["files"]}


def test_walk_fallback_honors_gitignore_and_default_excludes(make_repo):
    root = make_repo(FILES)
    inv = build_inventory(root, cfg())
    assert inv["method"].startswith("filesystem walk")
    assert paths(inv) == EXPECTED
    assert inv["excluded_dirs_unit"] == "directories"
    assert {"node_modules", "build", "__pycache__"} <= set(inv["excluded_dirs"])
    assert inv["skipped"]["binary/asset"] == 1
    assert inv["skipped"]["lockfile"] == 1
    assert inv["skipped"]["generated"] == 1


def test_git_ls_files_honors_gitignore_and_default_excludes(make_repo, git_init):
    root = make_repo(FILES)
    git_init(root, commit=False)  # untracked files are listed via --others --exclude-standard
    inv = build_inventory(root, cfg())
    assert inv["method"] == "git ls-files"
    assert paths(inv) == EXPECTED
    assert inv["excluded_dirs_unit"] == "files"
    assert inv["excluded_dirs"] == {"node_modules": 1, "build": 1, "__pycache__": 1}


def test_git_ls_files_skips_tracked_but_deleted(make_repo, git_init):
    root = git_init(make_repo({"a.py": "1\n", "b.py": "2\n"}))
    (root / "b.py").unlink()
    assert paths(build_inventory(root, cfg())) == {"a.py"}


def test_languages_counts_and_unsupported(make_repo):
    inv = build_inventory(make_repo(FILES), cfg())
    by_path = {f["path"]: f for f in inv["files"]}
    assert by_path["app/main.py"]["language"] == "python" and by_path["app/main.py"]["analyzed"]
    assert by_path["main.go"]["language"] == "go" and not by_path["main.go"]["analyzed"]
    assert by_path["docs/final.md"]["language"] is None
    assert inv["unsupported_languages"] == {"go": 1, "rust": 1}
    # sql is counted as a language but not as a source file
    assert inv["languages"]["sql"] == 1
    assert inv["source_file_count"] == 7  # 4 python (incl. test) + js + go + rust
    assert inv["test_file_count"] == 1
    assert by_path["tests/test_main.py"]["test"] and not by_path["tests/test_main.py"]["analyzed"]
    assert inv["top_level_dirs"] == ["app", "docs", "nested", "sub", "tests"]


def test_include_tests_and_extra_excludes_and_size_limit(make_repo):
    root = make_repo({**FILES, "big.py": "x = 1\n" * 200})
    inv = build_inventory(
        root, cfg(include_tests=True, exclude=["sub/*", "*.go"], max_file_bytes=1000)
    )
    by_path = {f["path"]: f for f in inv["files"]}
    assert by_path["tests/test_main.py"]["analyzed"]
    assert "sub/other.py" not in by_path and "main.go" not in by_path
    assert inv["skipped"]["config exclude"] == 3  # sub/.gitignore, sub/other.py, main.go
    assert "big.py" not in by_path and inv["skipped"]["too large"] == 1
    assert inv["extra_excludes"] == ["sub/*", "*.go"]


def test_binary_content_with_source_extension_is_skipped(make_repo):
    root = make_repo({"ok.py": "x = 1\n"})
    (root / "blob.py").write_bytes(b"\x00\x01binary")
    inv = build_inventory(root, cfg())
    assert paths(inv) == {"ok.py"}
    assert inv["skipped"]["binary/asset"] == 1


def test_symlinked_directories_are_not_followed(make_repo):
    root = make_repo({"real/a.py": "x = 1\n"})
    try:
        (root / "link").symlink_to(root / "real", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unsupported")
    assert paths(build_inventory(root, cfg())) == {"real/a.py"}


def test_empty_directory(tmp_path):
    inv = build_inventory(tmp_path, cfg())
    assert inv["files"] == [] and inv["source_file_count"] == 0


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("tests/x.py", True),
        ("pkg/__tests__/a.js", True),
        ("test_x.py", True),
        ("x_test.py", True),
        ("a.spec.ts", True),
        ("conftest.py", True),
        ("src/testing_utils.py", False),
        ("src/contest.py", False),
    ],
)
def test_is_test_path(rel, expected):
    assert is_test_path(rel) is expected


def test_ignore_matcher_semantics(tmp_path):
    (tmp_path / ".gitignore").write_text("*.tmp\n!important.tmp\nout/\n/top.txt\na/**/z.txt\n")
    m = _IgnoreMatcher()
    m.add_file(tmp_path / ".gitignore", "")
    assert m.ignored("x.tmp", False) and m.ignored("deep/x.tmp", False)
    assert not m.ignored("important.tmp", False)
    assert m.ignored("out", True) and not m.ignored("out", False)
    assert m.ignored("top.txt", False) and not m.ignored("d/top.txt", False)
    assert m.ignored("a/b/c/z.txt", False) and m.ignored("a/z.txt", False)


def test_find_repo_root(make_repo, git_init, tmp_path):
    root = git_init(make_repo({"pkg/a.py": "1\n"}))
    assert find_repo_root(root / "pkg") == root.resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert find_repo_root(plain) == plain.resolve()


def test_large_tree_walk_excludes_dependency_and_build_dirs(large_tree):
    inv = build_inventory(large_tree, cfg())
    listed = paths(inv)
    assert len([p for p in listed if p.endswith(".py")]) == 2000
    assert not any("node_modules" in p or "/build/" in p or p.startswith("build/") for p in listed)
    assert "debug.log" not in listed
    assert inv["analyzed_file_count"] == 2000
    assert inv["excluded_dirs"] == {"node_modules": 2, "build": 2}


def test_large_tree_git_excludes_dependency_and_build_dirs(large_tree, git_init):
    git_init(large_tree, commit=False)
    inv = build_inventory(large_tree, cfg())
    assert inv["method"] == "git ls-files"
    assert inv["analyzed_file_count"] == 2000
    assert inv["excluded_dirs"] == {"node_modules": 100, "build": 100}
