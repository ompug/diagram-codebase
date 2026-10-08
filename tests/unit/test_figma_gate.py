"""hooks/figma_gate.py: subprocess runs with crafted stdin and a temp cache dir."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import figma_gate  # tests pythonpath includes skills/diagram-codebase/hooks
import pytest
from diagram_codebase.config import DEFAULTS
from diagram_codebase.figma import ledger, ratelimit

HOOK = Path(figma_gate.__file__)
TOOL = "mcp__plugin_figma_figma__generate_diagram"


def run_hook(cache: Path, mode: str, payload, *, timeout=30) -> subprocess.CompletedProcess:
    env = {**os.environ, "DIAGRAM_CODEBASE_CACHE": str(cache)}
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK), mode],
        input=data,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def activate(cache: Path, **budget) -> None:
    b = copy.deepcopy(DEFAULTS["budget"])
    b.update(budget)
    ledger.activate("run-1", cache / "out", b, cache=cache)


def payload(use_id="tu-1", tool=TOOL, **extra):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {},
        "tool_use_id": use_id,
        "session_id": "s",
        "cwd": "/",
        **extra,
    }


def entries(cache):
    return ledger.read_entries(cache)


def test_noop_without_marker(tmp_path):
    r = run_hook(tmp_path, "pre", payload())
    assert r.returncode == 0 and r.stdout == ""
    assert not (tmp_path / "ledger.jsonl").exists()


def test_noop_with_stale_marker(tmp_path):
    activate(tmp_path)
    m = json.loads((tmp_path / "active.json").read_text())
    m["started"] = m["refreshed"] = time.time() - 7 * 3600
    (tmp_path / "active.json").write_text(json.dumps(m))
    r = run_hook(tmp_path, "pre", payload())
    assert r.returncode == 0 and r.stdout == ""
    assert entries(tmp_path) == []


def test_pre_records_attempt_and_dedupes(tmp_path):
    activate(tmp_path)
    for _ in range(2):
        r = run_hook(tmp_path, "pre", payload("tu-7"))
        assert r.returncode == 0 and r.stdout == ""
    e = entries(tmp_path)
    assert len(e) == 1
    assert e[0]["event"] == "attempt" and e[0]["tool"] == "generate_diagram"
    assert e[0]["tool_use_id"] == "tu-7" and e[0]["source"] == "hook" and e[0]["counted"]


def test_exempt_tool_not_counted(tmp_path):
    activate(tmp_path)
    run_hook(tmp_path, "pre", payload(tool="mcp__claude_ai_Figma__whoami"))
    assert entries(tmp_path) == []


def test_bad_input_never_crashes(tmp_path):
    activate(tmp_path)
    for data in ("", "not json", "[1, 2]", '{"tool_name": 5}'):
        r = run_hook(tmp_path, "pre", data)
        assert r.returncode == 0 and r.stderr == ""
    r = run_hook(tmp_path, "bogus-mode", payload())
    assert r.returncode == 0


def test_deny_when_daily_budget_reached(tmp_path):
    activate(tmp_path, per_day=2)
    now = time.time()
    for i in range(2):
        ledger.append(
            {"event": "attempt", "counted": True, "tool": "x", "ts": now - 100 - i}, tmp_path
        )
    r = run_hook(tmp_path, "pre", payload())
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "/diagram-codebase --resume" in out["permissionDecisionReason"]
    assert "budget reached" in out["permissionDecisionReason"].lower()
    assert len(entries(tmp_path)) == 2  # denied calls are not recorded as attempts


def test_deny_on_long_backoff(tmp_path):
    activate(tmp_path, max_hook_sleep_seconds=1)
    ledger.update_state(tmp_path, backoff_until=time.time() + 600)
    r = run_hook(tmp_path, "pre", payload())
    reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "backoff" in reason and "--resume" in reason


def test_sleeps_through_short_minute_window(tmp_path):
    activate(tmp_path, per_minute=1, minute_window_seconds=1.5, max_hook_sleep_seconds=5)
    ledger.append({"event": "attempt", "counted": True, "tool": "x", "ts": time.time()}, tmp_path)
    start = time.monotonic()
    r = run_hook(tmp_path, "pre", payload())
    assert r.stdout == ""
    assert time.monotonic() - start >= 1.0
    assert len(entries(tmp_path)) == 2


def test_sleep_path_with_fake_clock(tmp_path):
    activate(tmp_path)
    marker = ledger.read_marker(tmp_path)
    t = {"now": 10_000.0}
    slept = []
    for i in range(8):
        ledger.append(
            {"event": "attempt", "counted": True, "tool": "x", "ts": 9_990.0 + i}, tmp_path
        )

    def sleep(s):
        slept.append(s)
        t["now"] += s

    figma_gate.handle_pre(tmp_path, payload(), marker, now_fn=lambda: t["now"], sleep_fn=sleep)
    assert slept and sum(slept) == pytest.approx(9_990 + 60 - 10_000, abs=0.2)
    assert entries(tmp_path)[-1]["ts"] == t["now"]


def test_post_failure_rate_limit_sets_backoff(tmp_path):
    activate(tmp_path)
    before = time.time()
    r = run_hook(
        tmp_path,
        "post_failure",
        payload(
            "tu-9",
            hook_event_name="PostToolUseFailure",
            error="Error 429 Too Many Requests; retry after 120 seconds",
        ),
    )
    assert r.returncode == 0
    e = entries(tmp_path)
    outcome = next(x for x in e if x["event"] == "outcome")
    assert outcome["rate_limited"] and not outcome["ok"]
    assert any(x["event"] == "backoff" for x in e)
    state = ledger.read_state(tmp_path)
    assert state["consecutive_rate_limits"] == 1
    assert state["backoff_until"] == pytest.approx(before + 120, abs=5)


def test_post_success_logs_outcome_dedupes_and_resets_streak(tmp_path):
    activate(tmp_path)
    ledger.update_state(tmp_path, consecutive_rate_limits=3)
    p = payload(
        "tu-3",
        hook_event_name="PostToolUse",
        tool_response={"content": [{"type": "text", "text": "Quota dashboard diagram"}]},
    )
    run_hook(tmp_path, "post", p)
    run_hook(tmp_path, "post", p)
    outcomes = [x for x in entries(tmp_path) if x["event"] == "outcome"]
    assert len(outcomes) == 1 and outcomes[0]["ok"] and not outcomes[0]["rate_limited"]
    assert ledger.read_state(tmp_path)["consecutive_rate_limits"] == 0


def test_hook_logic_matches_package():
    budget = DEFAULTS["budget"]
    for text in ("Retry-After: 30", "retry after 2 minutes", "try again in 1500ms", "none"):
        assert figma_gate.parse_retry_after(text) == ratelimit.parse_retry_after(text)
    assert figma_gate.backoff_seconds(2, budget, 9) == ratelimit.backoff_seconds(
        2, budget, retry_after=9
    )
    assert DEFAULTS["budget"] == figma_gate.DEFAULT_BUDGET
    assert figma_gate.MARKER_MAX_AGE == ledger.MARKER_MAX_AGE
