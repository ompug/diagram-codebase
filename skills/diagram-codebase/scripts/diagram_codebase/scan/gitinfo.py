"""Git revision and identity (read-only git plumbing; never mutates the repo)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def sanitize_remote(url: str | None) -> str | None:
    """Strip credentials from a remote URL (https://user:token@host/x -> https://host/x)."""
    if not url:
        return None
    return re.sub(r"(?<=://)[^/@\s]+@", "", url)


def git_info(root: Path) -> dict[str, Any]:
    rev = _git(root, "rev-parse", "HEAD")
    if rev is None:
        return {
            "is_git": False,
            "revision": None,
            "short": None,
            "branch": None,
            "dirty": None,
            "remote": None,
        }
    status = _git(root, "status", "--porcelain", "--untracked-files=normal") or ""
    return {
        "is_git": True,
        "revision": rev,
        "short": rev[:10],
        "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status.strip()),
        "remote": sanitize_remote(_git(root, "config", "--get", "remote.origin.url")),
    }


def changed_files(root: Path, since: str) -> dict[str, list[str]] | None:
    """Files changed between `since` and the working tree (incl. uncommitted + untracked)."""
    diff = _git(root, "diff", "--name-status", "--no-renames", since)
    if diff is None:
        return None
    result: dict[str, list[str]] = {"added": [], "modified": [], "deleted": []}
    for line in diff.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code, path = parts[0][:1], parts[-1]
        key = {"A": "added", "D": "deleted"}.get(code, "modified")
        result[key].append(path)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard") or ""
    result["added"].extend(p for p in untracked.splitlines() if p and p not in result["added"])
    return result
