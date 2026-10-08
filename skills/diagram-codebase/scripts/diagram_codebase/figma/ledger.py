"""Global Figma call ledger, run marker and backoff state (the cache directory).

Figma quota is per account, so this state is global rather than per repo:

- `ledger.jsonl`  append-only call log shared by the hook and `dc.py next`/`record`.
  Entry: {ts, event: attempt|outcome|driver_estimate|backoff, tool, counted, run_id,
  source: hook|driver, tool_use_id?, action_id?, ok?, rate_limited?, error?, artifact?}
- `active.json`   run marker {run_id, out_dir, started, refreshed, budget{...}}; the hook
  acts only while it exists and is fresh (< MARKER_MAX_AGE seconds).
- `state.json`    {backoff_until, consecutive_rate_limits, last_rate_limit_at}
- `figma_gate.py` copy of the hook, so a stable path exists outside the plugin cache.

`hooks/figma_gate.py` reimplements the reading/writing of these files with the
standard library only (it cannot import this package); keep both in sync.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..common import read_json, write_json

try:  # POSIX only; on other platforms writes are line-sized appends without a lock.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

LEDGER = "ledger.jsonl"
MARKER = "active.json"
STATE = "state.json"
LOCK = "ledger.lock"
HOOK_NAME = "figma_gate.py"
MARKER_MAX_AGE = 6 * 3600
# Entries older than this are dropped when the ledger is compacted.
KEEP_SECONDS = 2 * 86400

HOOK_SOURCE = Path(__file__).resolve().parents[3] / "hooks" / HOOK_NAME


def cache_dir() -> Path:
    """$DIAGRAM_CODEBASE_CACHE, else $XDG_CACHE_HOME/diagram-codebase, else ~/.cache/..."""
    explicit = os.environ.get("DIAGRAM_CODEBASE_CACHE")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "diagram-codebase"


def _dir(cache: Path | None) -> Path:
    d = Path(cache) if cache is not None else cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextlib.contextmanager
def locked(cache: Path | None = None) -> Iterator[Path]:
    """Exclusive advisory lock over the cache dir (ledger + state read-modify-write)."""
    d = _dir(cache)
    with open(d / LOCK, "a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield d
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _append_unlocked(d: Path, entry: dict[str, Any]) -> None:
    line = json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
    with open(d / LEDGER, "a", encoding="utf-8") as fh:
        fh.write(line)


def append(entry: dict[str, Any], cache: Path | None = None) -> dict[str, Any]:
    """Append one entry (fills `ts` if missing) and return it."""
    entry = dict(entry)
    entry.setdefault("ts", time.time())
    with locked(cache) as d:
        _append_unlocked(d, entry)
    return entry


def _read_unlocked(d: Path, since: float | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    path = d / LEDGER
    if not path.is_file():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line never invalidates the rest of the ledger
            if not isinstance(entry, dict) or not isinstance(entry.get("ts"), (int, float)):
                continue
            if since is None or entry["ts"] >= since:
                out.append(entry)
    return out


def read_entries(cache: Path | None = None, since: float | None = None) -> list[dict[str, Any]]:
    with locked(cache) as d:
        return _read_unlocked(d, since)


def compact(cache: Path | None = None, now: float | None = None) -> int:
    """Drop entries older than KEEP_SECONDS. Returns the number of entries kept."""
    now = time.time() if now is None else now
    with locked(cache) as d:
        kept = _read_unlocked(d, now - KEEP_SECONDS)
        tmp = d / (LEDGER + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in kept:
                fh.write(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n")
        os.replace(tmp, d / LEDGER)
    return len(kept)


# ------------------------------------------------------------------ backoff state


def read_state(cache: Path | None = None) -> dict[str, Any]:
    d = _dir(cache)
    try:
        data = read_json(d / STATE, default={})
    except Exception:  # corrupt state must never stop a run; it is advisory
        data = {}
    return data if isinstance(data, dict) else {}


def update_state(cache: Path | None, **changes: Any) -> dict[str, Any]:
    with locked(cache) as d:
        state = read_state(d)
        state.update(changes)
        write_json(d / STATE, state)
    return state


def register_rate_limit(
    seconds: float,
    *,
    now: float | None = None,
    run_id: str | None = None,
    tool: str = "",
    source: str = "driver",
    cache: Path | None = None,
) -> dict[str, Any]:
    """Extend backoff to now+seconds, bump the consecutive counter, log a backoff entry."""
    now = time.time() if now is None else now
    with locked(cache) as d:
        state = read_state(d)
        state["consecutive_rate_limits"] = int(state.get("consecutive_rate_limits") or 0) + 1
        state["last_rate_limit_at"] = now
        state["backoff_until"] = max(float(state.get("backoff_until") or 0), now + seconds)
        write_json(d / STATE, state)
        _append_unlocked(
            d,
            {
                "ts": now,
                "event": "backoff",
                "tool": tool,
                "counted": False,
                "run_id": run_id,
                "source": source,
                "seconds": round(seconds, 1),
            },
        )
    return state


def clear_rate_limit_streak(cache: Path | None = None) -> None:
    state = read_state(cache)
    if state.get("consecutive_rate_limits"):
        update_state(cache, consecutive_rate_limits=0)


# ---------------------------------------------------------------- run marker


def read_marker(cache: Path | None = None) -> dict[str, Any] | None:
    d = _dir(cache)
    try:
        data = read_json(d / MARKER, default={})
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("run_id") else None


def marker_is_fresh(marker: dict[str, Any] | None, now: float | None = None) -> bool:
    if not marker:
        return False
    now = time.time() if now is None else now
    stamp = max(float(marker.get("started") or 0), float(marker.get("refreshed") or 0))
    return 0 <= now - stamp < MARKER_MAX_AGE


def install_hook(cache: Path | None = None) -> Path | None:
    """Copy hooks/figma_gate.py into the cache dir (only when missing or changed)."""
    d = _dir(cache)
    dest = d / HOOK_NAME
    if not HOOK_SOURCE.is_file():
        return None
    try:
        if not dest.is_file() or dest.read_bytes() != HOOK_SOURCE.read_bytes():
            tmp = d / (HOOK_NAME + ".tmp")
            shutil.copyfile(HOOK_SOURCE, tmp)
            os.replace(tmp, dest)
    except OSError:
        return None
    return dest


def activate(
    run_id: str,
    out_dir: Path | str,
    budget: dict[str, Any],
    *,
    now: float | None = None,
    cache: Path | None = None,
) -> dict[str, Any]:
    """Write (or refresh) the run marker and install the hook copy."""
    now = time.time() if now is None else now
    d = _dir(cache)
    current = read_marker(d)
    started = now
    if current and current.get("run_id") == run_id and marker_is_fresh(current, now):
        started = float(current.get("started") or now)
    marker = {
        "run_id": run_id,
        "out_dir": str(out_dir),
        "started": started,
        "refreshed": now,
        "budget": dict(budget),
    }
    write_json(d / MARKER, marker)
    install_hook(d)
    if current is None:
        compact(d, now)
    return marker


def deactivate(run_id: str | None = None, cache: Path | None = None) -> bool:
    """Remove the marker (only if it belongs to `run_id`, when given)."""
    d = _dir(cache)
    current = read_marker(d)
    if current is None:
        return False
    if run_id is not None and current.get("run_id") != run_id:
        return False
    try:
        (d / MARKER).unlink()
    except FileNotFoundError:
        return False
    return True
