"""Pure rate-limit math over ledger entries (no IO, no clock: `now` is passed in).

Windows are rolling, not bucketed: a call made at t counts until t + window.
All numbers are local estimates of this machine's own calls, never Figma's quota.
`hooks/figma_gate.py` carries a stdlib copy of `classify_error`/`backoff_seconds`
logic; tests keep the two in agreement.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterable
from typing import Any

COUNTED_EVENTS = ("attempt", "driver_estimate")


def tool_suffix(name: str) -> str:
    """mcp__plugin_figma_figma__generate_diagram -> generate_diagram."""
    return (name or "").rsplit("__", 1)[-1]


def is_counted(tool: str, budget: dict[str, Any]) -> bool:
    return tool_suffix(tool) not in set(budget.get("exempt_tools") or [])


def counted_times(entries: Iterable[dict[str, Any]]) -> list[float]:
    """Timestamps of counted calls (hook attempts + driver estimates), sorted."""
    return sorted(
        float(e["ts"])
        for e in entries
        if e.get("event") in COUNTED_EVENTS and e.get("counted", True) and "ts" in e
    )


def window_count(times: list[float], now: float, window: float) -> int:
    """Calls in the rolling window (now - window, now]."""
    return sum(1 for t in times if now - window < t <= now)


def usage(entries: Iterable[dict[str, Any]], now: float, budget: dict[str, Any]) -> dict[str, Any]:
    times = counted_times(entries)
    return {
        "minute": window_count(times, now, budget["minute_window_seconds"]),
        "day": window_count(times, now, budget["day_window_seconds"]),
        "per_minute": budget["per_minute"],
        "per_day": budget["per_day"],
        "note": "local estimate of this machine's calls, not Figma's quota",
    }


def _wait_for_room(times: list[float], now: float, window: float, limit: int, need: int) -> float:
    """Seconds until the rolling window has room for `need` more calls (0 if it has now)."""
    inside = [t for t in times if now - window < t <= now]
    excess = len(inside) + need - limit
    if excess <= 0:
        return 0.0
    if excess > len(inside):
        return float("inf")  # need exceeds the limit itself
    # The excess-th oldest call must leave the window.
    return max(0.0, inside[excess - 1] + window - now) + 0.01


def decide(
    entries: Iterable[dict[str, Any]],
    now: float,
    budget: dict[str, Any],
    *,
    backoff_until: float | None = None,
    need: int = 1,
) -> dict[str, Any]:
    """Decide whether `need` more counted calls may start now.

    Returns {"decision": "allow"} | {"decision": "wait", "seconds": s, "reason"} |
    {"decision": "deny", "reason"}; always includes "minute"/"day" counts.
    Daily exhaustion is a deny (waiting hours is not useful); minute-window and
    backoff waits are returned as waits and the caller decides how long it may sleep.
    """
    times = counted_times(entries)
    minute_w, day_w = budget["minute_window_seconds"], budget["day_window_seconds"]
    out: dict[str, Any] = {
        "minute": window_count(times, now, minute_w),
        "day": window_count(times, now, day_w),
    }
    if out["day"] + need > budget["per_day"]:
        return {
            **out,
            "decision": "deny",
            "reason": f"daily budget reached ({out['day']}/{budget['per_day']} calls in the last "
            f"24 h, local estimate)",
        }
    wait = 0.0
    reason = ""
    if backoff_until and backoff_until > now:
        wait, reason = backoff_until - now, "rate-limit backoff"
    minute_wait = _wait_for_room(times, now, minute_w, budget["per_minute"], need)
    if minute_wait > wait:
        wait, reason = minute_wait, f"minute budget ({out['minute']}/{budget['per_minute']})"
    if wait > 0:
        return {**out, "decision": "wait", "seconds": round(wait, 2), "reason": reason}
    return {**out, "decision": "allow"}


# ------------------------------------------------------------------ backoff

_RETRY_RE = [
    re.compile(
        r"retry[- ]after[\"']?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|sec|seconds?|m|min|minutes?)?",
        re.I,
    ),
    re.compile(
        r"try again in\s*(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|sec|seconds?|m|min|minutes?)?", re.I
    ),
]


def parse_retry_after(text: str | None) -> float | None:
    """Seconds from 'Retry-After: 30', 'retry after 2 minutes', 'try again in 45s'."""
    if not text:
        return None
    for rx in _RETRY_RE:
        m = rx.search(text)
        if m:
            value = float(m.group(1))
            unit = (m.group(2) or "s").lower()
            if unit.startswith("m") and unit not in ("ms",) and not unit.startswith("milli"):
                value *= 60
            elif unit == "ms" or unit.startswith("milli"):
                value /= 1000
            return value
    return None


def backoff_seconds(
    consecutive: int,
    budget: dict[str, Any],
    *,
    retry_after: float | None = None,
    rng: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff (base * 2^(n-1), capped, +-20% jitter); retry-after wins."""
    if retry_after is not None and retry_after > 0:
        return float(retry_after)
    base = float(budget["backoff_base_seconds"])
    cap = float(budget["backoff_max_seconds"])
    raw = min(cap, base * (2 ** max(0, consecutive - 1)))
    jitter = 1 + (rng() * 0.4 - 0.2)
    return round(min(cap, raw * jitter), 2)


# ------------------------------------------------------------- error classes

_CLASSES: list[tuple[str, re.Pattern[str]]] = [
    (
        "rate_limit",
        re.compile(
            r"\b429\b|rate[- ]?limit|too many requests|\bquota\b|retry[- ]after|throttl", re.I
        ),
    ),
    (
        "auth",
        re.compile(
            r"\b40[13]\b|unauthori[sz]ed|unauthenticated|not authenticated|authenticat|"
            r"forbidden|invalid[_ ]token|token (?:has )?expired|access denied|log ?in required|"
            r"permission denied|oauth",
            re.I,
        ),
    ),
    (
        "network",
        re.compile(
            r"\b50[234]\b|timed? ?out|timeout|econn|connection (?:refused|reset|closed|error)|"
            r"network|disconnected|not connected|socket|unreachable|service unavailable|"
            r"no such tool|tool (?:is )?(?:not available|unavailable)|mcp server|fetch failed",
            re.I,
        ),
    ),
    (
        "not_found",
        re.compile(r"\b404\b|not found|does not exist|no such (?:file|node)|unknown file", re.I),
    ),
    (
        "invalid_request",
        re.compile(
            r"\b400\b|\b422\b|invalid|syntax|parse error|could not parse|failed to parse|"
            r"bad request|validation|unsupported|unexpected token|mermaid|lexical error",
            re.I,
        ),
    ),
]


def classify_error(text: str | None) -> str:
    """rate_limit | auth | network | not_found | invalid_request | unknown (first match wins)."""
    if not text:
        return "unknown"
    for name, rx in _CLASSES:
        if rx.search(text):
            return name
    return "unknown"


_STRICT_RATE_RE = re.compile(r"\b429\b|rate[- ]?limit(?:ed)?\b|too many requests", re.I)


def looks_rate_limited(text: str | None, *, strict: bool) -> bool:
    """Rate-limit detection. `strict` (successful results) checks only the start of the
    text with unambiguous phrases, so diagram content mentioning 'quota' never triggers."""
    if not text:
        return False
    if strict:
        return bool(_STRICT_RATE_RE.search(text[:400]))
    return classify_error(text) == "rate_limit"
