"""Optional real-parser check: `mermaid.parse` via Node (tools/mermaid-check).

Never required. When Node or the tool's dependencies are missing, `check`
returns status "unavailable" and callers must report it as not run (never as
passed). The tool directory can be overridden with
`$DIAGRAM_CODEBASE_MERMAID_CHECK` (a directory containing `check.mjs` and its
`node_modules/`, or the path of `check.mjs` itself).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

ENV_VAR = "DIAGRAM_CODEBASE_MERMAID_CHECK"
DEFAULT_TOOL_DIR = Path(__file__).resolve().parents[3] / "tools" / "mermaid-check"
DEFAULT_TIMEOUT = 120.0


def tool_dir() -> Path:
    override = os.environ.get(ENV_VAR)
    if override:
        path = Path(override).expanduser()
        return path.parent if path.name == "check.mjs" else path
    return DEFAULT_TOOL_DIR


def _node() -> str | None:
    return shutil.which("node")


def unavailable_reason() -> str | None:
    """Why the checker cannot run, or None when it can."""
    if _node() is None:
        return "node not found on PATH"
    root = tool_dir()
    if not (root / "check.mjs").is_file():
        return f"{root / 'check.mjs'} not found"
    for dep in ("mermaid", "jsdom"):
        if not (root / "node_modules" / dep / "package.json").is_file():
            return f"{dep} is not installed in {root} (run `npm install` there)"
    return None


def available() -> bool:
    return unavailable_reason() is None


def check(items: list[dict[str, str]], timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Parse each `{id, text}` with Mermaid.

    Returns `{"status": "ok"|"unavailable"|"error", "results": {id: None | error}, "detail": str}`.
    `results` maps every id to None (parsed) or the parser's error text; it is empty
    unless status is "ok".
    """
    reason = unavailable_reason()
    if reason:
        return {"status": "unavailable", "results": {}, "detail": reason}
    if not items:
        return {"status": "ok", "results": {}, "detail": ""}
    payload = json.dumps([{"id": str(i["id"]), "text": str(i["text"])} for i in items])
    root = tool_dir()
    try:
        proc = subprocess.run(
            [_node() or "node", str(root / "check.mjs")],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            cwd=str(root),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "results": {},
            "detail": f"mermaid-check timed out after {timeout:.0f}s",
        }
    except OSError as exc:
        return {"status": "error", "results": {}, "detail": f"could not run node: {exc}"}
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return {
            "status": "error",
            "results": {},
            "detail": f"mermaid-check exited {proc.returncode}: {' | '.join(tail)}",
        }
    try:
        results = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "results": {}, "detail": "mermaid-check printed invalid JSON"}
    if not isinstance(results, dict):
        return {"status": "error", "results": {}, "detail": "mermaid-check printed a non-object"}
    missing = [str(i["id"]) for i in items if str(i["id"]) not in results]
    if missing:
        return {"status": "error", "results": {}, "detail": f"no result for {missing[:5]}"}
    return {"status": "ok", "results": {str(k): v for k, v in results.items()}, "detail": ""}
