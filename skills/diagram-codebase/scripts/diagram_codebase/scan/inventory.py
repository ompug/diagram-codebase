"""Repository file inventory that respects .gitignore and project exclusions.

Uses `git ls-files` when the target is a git work tree (the most faithful
.gitignore implementation available); otherwise walks the tree with a small
gitignore-subset matcher. Never executes repository code.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".scala": "scala",
    ".m": "objective-c",
    ".lua": "lua",
    ".r": "r",
    ".jl": "julia",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".proto": "protobuf",
    ".msg": "ros-interface",
    ".srv": "ros-interface",
    ".action": "ros-interface",
    ".vue": "vue",
    ".svelte": "svelte",
}

# Languages with deterministic structural analysis in this package.
ANALYZED_LANGUAGES = {"python", "javascript", "typescript", "c", "cpp"}

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "bower_components",
    "vendor",
    "third_party",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "env",
    ".eggs",
    "dist",
    "build",
    "out",
    "target",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".cache",
    "coverage",
    "htmlcov",
    ".gradle",
    ".idea",
    ".vscode",
    "install",
    "log",
    ".diagram-codebase",
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".pdf",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".tar", ".jar", ".war",
    ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".lib", ".pyc", ".class",
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3", ".pt", ".pth", ".onnx",
    ".h5", ".npy", ".npz", ".pkl", ".parquet", ".bag", ".mcap", ".pcd", ".ply",
    ".mp3", ".mp4", ".wav", ".mov", ".avi", ".ttf", ".otf", ".woff", ".woff2",
}  # fmt: skip

GENERATED_MARKERS = ("_pb2.py", "_pb2_grpc.py", ".min.js", ".bundle.js", ".generated.")
LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "uv.lock",
}
TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec", "specs", "testing"}


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (
        any(p in TEST_DIR_NAMES for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith(
            (
                "_test.py",
                ".test.js",
                ".test.ts",
                ".test.tsx",
                ".spec.js",
                ".spec.ts",
                "_test.go",
                "_test.cpp",
            )
        )
        or name == "conftest.py"
    )


def find_repo_root(start: Path) -> Path:
    start = start.resolve()
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def _git_files(root: Path) -> list[str] | None:
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    files = [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]
    # ls-files lists deleted-but-tracked files too; keep only those present.
    return sorted({f for f in files if (root / f).is_file()})


class _IgnoreMatcher:
    """Subset of gitignore semantics for non-git directories.

    Supports comments, negation, directory-only patterns, anchored patterns and
    `**`. Patterns from nested .gitignore files apply relative to their dir.
    """

    def __init__(self) -> None:
        self.rules: list[tuple[str, str, bool, bool]] = []  # (base, pattern, negate, dir_only)

    def add_file(self, path: Path, base: str) -> None:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            if line:
                self.rules.append((base, line, negate, dir_only))

    def ignored(self, rel: str, is_dir: bool) -> bool:
        result = False
        for base, pattern, negate, dir_only in self.rules:
            if dir_only and not is_dir:
                continue
            if base and not rel.startswith(base + "/"):
                continue
            sub = rel[len(base) + 1 :] if base else rel
            if "/" in pattern.strip("/"):
                pat = pattern.lstrip("/")
                hit = fnmatch.fnmatch(sub, pat) or fnmatch.fnmatch(sub, pat.replace("**/", ""))
            elif pattern.startswith("/"):
                hit = fnmatch.fnmatch(sub, pattern[1:])
            else:
                hit = fnmatch.fnmatch(sub.rsplit("/", 1)[-1], pattern)
            if hit:
                result = not negate
        return result


def _walk_files(root: Path, pruned: Counter | None = None) -> list[str]:
    """List files under `root`, honoring .gitignore files; `pruned` counts skipped default dirs."""
    matcher = _IgnoreMatcher()
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(os.path.relpath(dirpath, root)).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        if ".gitignore" in filenames:
            matcher.add_file(Path(dirpath) / ".gitignore", rel_dir)
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if d in DEFAULT_EXCLUDED_DIRS:
                if pruned is not None:
                    pruned[d] += 1
                continue
            if matcher.ignored(rel, True):
                continue
            if (Path(dirpath) / d).is_symlink():
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if not matcher.ignored(rel, False):
                files.append(rel)
    return files


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return True
    return b"\0" in chunk


def build_inventory(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    scan_cfg = config.get("scan", {})
    extra_excludes = scan_cfg.get("exclude", []) or []
    max_bytes = int(scan_cfg.get("max_file_bytes", 1_000_000))
    include_tests = bool(scan_cfg.get("include_tests", False))

    files: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    # git mode counts excluded *files* per directory name; walk mode never descends into
    # excluded directories, so it counts the *directories* it pruned.
    excluded_dirs: Counter = Counter()
    listed = _git_files(root)
    method = "git ls-files"
    excluded_unit = "files"
    if listed is None:
        listed = _walk_files(root, excluded_dirs)
        method = "filesystem walk with .gitignore subset"
        excluded_unit = "directories"

    for rel in listed:
        parts = rel.split("/")
        hit_dir = next((p for p in parts[:-1] if p in DEFAULT_EXCLUDED_DIRS), None)
        if hit_dir:
            excluded_dirs[hit_dir] += 1
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in extra_excludes):
            skipped["config exclude"] += 1
            continue
        name = parts[-1]
        ext = os.path.splitext(name)[1].lower()
        if ext in BINARY_EXTENSIONS:
            skipped["binary/asset"] += 1
            continue
        if name in LOCKFILES:
            skipped["lockfile"] += 1
            continue
        if any(m in name for m in GENERATED_MARKERS):
            skipped["generated"] += 1
            continue
        path = root / rel
        try:
            size = path.stat().st_size
        except OSError:
            skipped["unreadable"] += 1
            continue
        if size > max_bytes:
            skipped["too large"] += 1
            continue
        lang = LANGUAGES.get(ext)
        if lang and _looks_binary(path):
            skipped["binary/asset"] += 1
            continue
        test = is_test_path(rel)
        files.append(
            {
                "path": rel,
                "language": lang,
                "size": size,
                "test": test,
                "analyzed": bool(lang in ANALYZED_LANGUAGES and (include_tests or not test)),
            }
        )

    lang_counts = Counter(f["language"] for f in files if f["language"])
    source_files = [
        f for f in files if f["language"] and f["language"] not in ("ros-interface", "sql")
    ]
    unsupported = Counter(
        f["language"] for f in source_files if f["language"] not in ANALYZED_LANGUAGES
    )
    return {
        "root_name": root.name,
        "method": method,
        "files": files,
        "languages": dict(lang_counts.most_common()),
        "source_file_count": len(source_files),
        "test_file_count": sum(1 for f in files if f["test"]),
        "analyzed_file_count": sum(1 for f in files if f["analyzed"]),
        "unsupported_languages": dict(unsupported),
        "skipped": dict(skipped),
        "excluded_dirs": dict(excluded_dirs),
        "excluded_dirs_unit": excluded_unit,
        "extra_excludes": list(extra_excludes),
        "top_level_dirs": sorted({f["path"].split("/")[0] for f in files if "/" in f["path"]}),
    }
