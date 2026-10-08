#!/usr/bin/env python3
"""diagram-codebase Figma gate: PreToolUse / PostToolUse / PostToolUseFailure hook.

Usage (from hook config): python3 figma_gate.py pre|post|post_failure  (JSON on stdin)

Standard library only and standalone: it runs from a copy in the cache dir and must
not import the diagram_codebase package. It mirrors figma/ledger.py (file formats)
and figma/ratelimit.py (window math, backoff, rate-limit detection).

Contract:
- Acts only while the run marker `active.json` exists and is fresh (< 6 h); otherwise
  it is a fast no-op, because skill hooks stay registered for the whole session.
- Never crashes or blocks by accident: any exception exits 0 silently. The only
  intentional block is a PreToolUse "deny" decision printed as JSON on stdout.
- pre: rolling minute window -> sleep (<= max_hook_sleep_seconds) or deny; rolling day
  window or long backoff -> deny; then record an attempt.
- post/post_failure: record the outcome; rate-limit text -> extend backoff.
- Events are deduplicated by tool_use_id.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

MARKER_MAX_AGE = 6 * 3600
RESUME_HINT = "state saved by dc.py; resume later with /diagram-codebase --resume"
DEFAULT_BUDGET = {
    "per_minute": 8,
    "per_day": 160,
    "minute_window_seconds": 60,
    "day_window_seconds": 86400,
    "exempt_tools": [
        "whoami",
        "create_new_file",
        "add_code_connect_map",
        "authenticate",
        "complete_authentication",
    ],
    "max_hook_sleep_seconds": 65,
    "backoff_base_seconds": 30,
    "backoff_max_seconds": 900,
}

_RATE_STRICT = re.compile(r"\b429\b|rate[- ]?limit(?:ed)?\b|too many requests", re.I)
_RATE_LOOSE = re.compile(
    r"\b429\b|rate[- ]?limit|too many requests|\bquota\b|retry[- ]after|throttl", re.I
)
_RETRY_RES = [
    re.compile(
        r"retry[- ]after[\"']?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|sec|seconds?|m|min|minutes?)?",
        re.I,
    ),
    re.compile(
        r"try again in\s*(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|sec|seconds?|m|min|minutes?)?", re.I
    ),
]


def cache_dir() -> Path:
    explicit = os.environ.get("DIAGRAM_CODEBASE_CACHE")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "diagram-codebase"


class Lock:
    def __init__(self, d: Path) -> None:
        self.path = d / "ledger.lock"
        self.fh = None

    def __enter__(self) -> Lock:
        self.fh = open(self.path, "a+")
        if fcntl is not None:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fh is not None:
            if fcntl is not None:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()


def read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def read_entries(d: Path, since: float) -> list[dict]:
    out = []
    path = d / "ledger.jsonl"
    if not path.is_file():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and isinstance(e.get("ts"), (int, float)) and e["ts"] >= since:
                out.append(e)
    return out


def append(d: Path, entry: dict) -> None:
    with open(d / "ledger.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")


def active_marker(d: Path, now: float) -> dict | None:
    marker = read_json(d / "active.json")
    if not marker.get("run_id"):
        return None
    stamp = max(float(marker.get("started") or 0), float(marker.get("refreshed") or 0))
    return marker if 0 <= now - stamp < MARKER_MAX_AGE else None


def budget_of(marker: dict) -> dict:
    budget = dict(DEFAULT_BUDGET)
    budget.update(marker.get("budget") or {})
    return budget


def suffix(tool_name: str) -> str:
    return (tool_name or "").rsplit("__", 1)[-1]


def counted_times(entries: list[dict]) -> list[float]:
    return sorted(
        float(e["ts"])
        for e in entries
        if e.get("event") in ("attempt", "driver_estimate") and e.get("counted", True)
    )


def in_window(times: list[float], now: float, window: float) -> list[float]:
    return [t for t in times if now - window < t <= now]


def parse_retry_after(text: str) -> float | None:
    for rx in _RETRY_RES:
        m = rx.search(text or "")
        if m:
            value = float(m.group(1))
            unit = (m.group(2) or "s").lower()
            if unit == "ms" or unit.startswith("milli"):
                value /= 1000
            elif unit.startswith("m"):
                value *= 60
            return value
    return None


def backoff_seconds(consecutive: int, budget: dict, retry_after: float | None) -> float:
    if retry_after is not None and retry_after > 0:
        return float(retry_after)
    base, cap = float(budget["backoff_base_seconds"]), float(budget["backoff_max_seconds"])
    raw = min(cap, base * (2 ** max(0, consecutive - 1)))
    return round(min(cap, raw * (1 + (random.random() * 0.4 - 0.2))), 2)


def deny(reason: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def text_of(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# ------------------------------------------------------------------ modes


def handle_pre(d: Path, payload: dict, marker: dict, now_fn=time.time, sleep_fn=time.sleep) -> None:
    budget = budget_of(marker)
    tool = payload.get("tool_name") or ""
    if suffix(tool) in set(budget["exempt_tools"]):
        return
    use_id = payload.get("tool_use_id")
    slept = 0.0
    max_sleep = float(budget["max_hook_sleep_seconds"])
    while True:
        now = now_fn()
        with Lock(d):
            entries = read_entries(d, now - float(budget["day_window_seconds"]))
            if use_id and any(
                e.get("event") == "attempt" and e.get("tool_use_id") == use_id for e in entries
            ):
                return
            state = read_json(d / "state.json")
            times = counted_times(entries)
            day = in_window(times, now, float(budget["day_window_seconds"]))
            if len(day) >= int(budget["per_day"]):
                deny(
                    f"diagram-codebase: daily Figma call budget reached ({len(day)}/"
                    f"{budget['per_day']} in 24 h, local estimate). Budget reached: {RESUME_HINT}."
                )
                return
            wait = 0.0
            backoff_left = float(state.get("backoff_until") or 0) - now
            if backoff_left > 0:
                wait = backoff_left
            minute = in_window(times, now, float(budget["minute_window_seconds"]))
            excess = len(minute) + 1 - int(budget["per_minute"])
            if excess > 0:
                wait = max(wait, minute[excess - 1] + float(budget["minute_window_seconds"]) - now)
            if wait <= 0:
                append(
                    d,
                    {
                        "ts": now,
                        "event": "attempt",
                        "tool": suffix(tool),
                        "counted": True,
                        "run_id": marker.get("run_id"),
                        "tool_use_id": use_id,
                        "source": "hook",
                    },
                )
                return
        if slept + wait > max_sleep:
            kind = "rate-limit backoff" if backoff_left > 0 else "minute budget"
            deny(
                f"diagram-codebase: Figma {kind} needs a {int(wait) + 1}s pause, longer than the "
                f"hook may wait. Budget reached: {RESUME_HINT}."
            )
            return
        sleep_fn(wait + 0.05)
        slept += wait + 0.05


def handle_post(d: Path, payload: dict, marker: dict, failure: bool, now_fn=time.time) -> None:
    budget = budget_of(marker)
    tool = payload.get("tool_name") or ""
    counted = suffix(tool) not in set(budget["exempt_tools"])
    use_id = payload.get("tool_use_id")
    if failure:
        text = text_of(payload.get("error")) or text_of(payload.get("tool_response"))
        rate_limited = bool(_RATE_LOOSE.search(text))
    else:
        text = text_of(payload.get("tool_response"))
        rate_limited = bool(_RATE_STRICT.search(text[:400]))
    now = now_fn()
    with Lock(d):
        if use_id and any(
            e.get("event") == "outcome" and e.get("tool_use_id") == use_id
            for e in read_entries(d, now - float(budget["day_window_seconds"]))
        ):
            return
        entry = {
            "ts": now,
            "event": "outcome",
            "tool": suffix(tool),
            "counted": counted,
            "run_id": marker.get("run_id"),
            "tool_use_id": use_id,
            "ok": not failure and not rate_limited,
            "rate_limited": rate_limited,
            "source": "hook",
        }
        if failure or rate_limited:
            entry["error"] = text[:300]
        append(d, entry)
        state = read_json(d / "state.json")
        if rate_limited:
            n = int(state.get("consecutive_rate_limits") or 0) + 1
            seconds = backoff_seconds(n, budget, parse_retry_after(text))
            state.update(
                consecutive_rate_limits=n,
                last_rate_limit_at=now,
                backoff_until=max(float(state.get("backoff_until") or 0), now + seconds),
            )
            write_json(d / "state.json", state)
            append(
                d,
                {
                    "ts": now,
                    "event": "backoff",
                    "tool": suffix(tool),
                    "counted": False,
                    "run_id": marker.get("run_id"),
                    "source": "hook",
                    "seconds": seconds,
                },
            )
        elif not failure and state.get("consecutive_rate_limits"):
            state["consecutive_rate_limits"] = 0
            write_json(d / "state.json", state)


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    if mode not in ("pre", "post", "post_failure"):
        return 0
    d = cache_dir()
    if not (d / "active.json").is_file():
        return 0  # fast path: no run in progress
    marker = active_marker(d, time.time())
    if marker is None:
        return 0
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        return 0
    if mode == "pre":
        handle_pre(d, payload, marker)
    else:
        handle_post(d, payload, marker, failure=(mode == "post_failure"))
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv)
    except BaseException:  # noqa: BLE001 - a hook must never break the tool call
        code = 0
    sys.exit(code)
