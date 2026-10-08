from __future__ import annotations

import copy

import pytest
from diagram_codebase.config import DEFAULTS
from diagram_codebase.figma import ratelimit as rl

BUDGET = copy.deepcopy(DEFAULTS["budget"])


def attempts(*ts, event="attempt"):
    return [{"ts": t, "event": event, "counted": True} for t in ts]


def test_tool_suffix_and_counted():
    assert rl.tool_suffix("mcp__plugin_figma_figma__generate_diagram") == "generate_diagram"
    assert rl.tool_suffix("mcp__claude_ai_Figma__use_figma") == "use_figma"
    assert rl.tool_suffix("whoami") == "whoami"
    assert rl.is_counted("mcp__claude_ai_Figma__get_figjam", BUDGET)
    assert not rl.is_counted("mcp__plugin_figma_figma__whoami", BUDGET)


def test_only_counted_events_count():
    entries = attempts(1, 2) + attempts(3, event="driver_estimate")
    entries += [{"ts": 4, "event": "outcome", "counted": True}]
    entries += [{"ts": 5, "event": "attempt", "counted": False}]
    entries += [{"ts": 6, "event": "backoff", "counted": False}]
    assert rl.counted_times(entries) == [1.0, 2.0, 3.0]


def test_minute_window_waits_until_oldest_expires():
    entries = attempts(*range(100, 108))  # 8 calls = per_minute
    d = rl.decide(entries, 130, BUDGET)
    assert d["decision"] == "wait"
    assert d["seconds"] == pytest.approx(100 + 60 - 130, abs=0.05)
    assert rl.decide(entries, 160.5, BUDGET)["decision"] == "allow"


def test_window_is_rolling_not_bucketed():
    # 8 calls in the last seconds of one clock minute still block the next minute.
    entries = attempts(*[55 + i * 0.5 for i in range(8)])
    d = rl.decide(entries, 61, BUDGET)
    assert d["decision"] == "wait" and d["minute"] == 8
    # Exactly window seconds later the first call has left the window.
    assert rl.decide(entries, 115.01, BUDGET)["decision"] == "allow"


def test_need_more_than_one_slot():
    entries = attempts(*range(100, 107))  # 7 of 8
    assert rl.decide(entries, 110, BUDGET, need=1)["decision"] == "allow"
    d = rl.decide(entries, 110, BUDGET, need=2)
    assert d["decision"] == "wait"
    assert d["seconds"] == pytest.approx(100 + 60 - 110, abs=0.05)


def test_daily_budget_denies():
    budget = {**BUDGET, "per_day": 5}
    entries = attempts(0, 10, 20, 30, 40)
    d = rl.decide(entries, 1000, budget)
    assert d["decision"] == "deny" and "daily budget" in d["reason"]
    assert "local estimate" in d["reason"]
    assert rl.decide(entries, 86400 + 1, budget)["decision"] == "allow"


def test_backoff_wait():
    d = rl.decide([], 100, BUDGET, backoff_until=145)
    assert d["decision"] == "wait" and d["seconds"] == pytest.approx(45)
    assert d["reason"] == "rate-limit backoff"
    assert rl.decide([], 146, BUDGET, backoff_until=145)["decision"] == "allow"


def test_backoff_seconds_exponential_with_jitter_and_cap():
    mid = lambda: 0.5  # noqa: E731 - zero jitter
    assert rl.backoff_seconds(1, BUDGET, rng=mid) == 30
    assert rl.backoff_seconds(3, BUDGET, rng=mid) == 120
    assert rl.backoff_seconds(10, BUDGET, rng=mid) == 900
    assert rl.backoff_seconds(1, BUDGET, rng=lambda: 0.0) == pytest.approx(24)
    assert rl.backoff_seconds(1, BUDGET, rng=lambda: 0.999) == pytest.approx(36, abs=0.1)
    assert rl.backoff_seconds(10, BUDGET, rng=lambda: 0.999) == 900  # cap after jitter
    assert rl.backoff_seconds(1, BUDGET, retry_after=7) == 7


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("Retry-After: 30", 30),
        ("please retry after 2 minutes", 120),
        ("try again in 45s", 45),
        ('{"retry_after": 5}', None),
        ("retry after 1500ms", 1.5),
        ("no hint here", None),
    ],
)
def test_parse_retry_after(text, seconds):
    assert rl.parse_retry_after(text) == seconds


@pytest.mark.parametrize(
    ("text", "cls"),
    [
        ("HTTP 429", "rate_limit"),
        ("Rate limit exceeded for this seat", "rate_limit"),
        ("Too Many Requests", "rate_limit"),
        ("monthly quota exhausted", "rate_limit"),
        ("401 Unauthorized", "auth"),
        ("Please authenticate with Figma first", "auth"),
        ("403 Forbidden", "auth"),
        ("MCP server figma is not connected", "network"),
        ("request timed out", "network"),
        ("503 Service Unavailable", "network"),
        ("File not found", "not_found"),
        ("404", "not_found"),
        ("Invalid Mermaid syntax: parse error", "invalid_request"),
        ("400 Bad Request", "invalid_request"),
        ("something odd happened", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_error(text, cls):
    assert rl.classify_error(text) == cls


def test_strict_rate_limit_detection_ignores_content():
    xml = "<figjam>" + "x" * 500 + "<sticky text='Rate limit our API' /></figjam>"
    assert not rl.looks_rate_limited(xml, strict=True)
    assert not rl.looks_rate_limited("Quota service (diagram)", strict=True)
    assert rl.looks_rate_limited("Error 429: slow down", strict=True)
    assert rl.looks_rate_limited("quota exceeded", strict=False)


def test_usage_is_labelled_as_estimate():
    u = rl.usage(attempts(1, 2), 30, BUDGET)
    assert u["minute"] == 2 and u["day"] == 2
    assert "not Figma's quota" in u["note"]
