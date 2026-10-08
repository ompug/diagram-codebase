from __future__ import annotations

import multiprocessing
from pathlib import Path

from diagram_codebase.config import DEFAULTS
from diagram_codebase.figma import ledger


def test_cache_dir_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("DIAGRAM_CODEBASE_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ledger.cache_dir() == tmp_path / "xdg" / "diagram-codebase"
    monkeypatch.setenv("DIAGRAM_CODEBASE_CACHE", str(tmp_path / "explicit"))
    assert ledger.cache_dir() == tmp_path / "explicit"
    monkeypatch.delenv("DIAGRAM_CODEBASE_CACHE")
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert ledger.cache_dir() == tmp_path / "home" / ".cache" / "diagram-codebase"


def test_append_read_and_torn_lines(tmp_path):
    ledger.append({"event": "attempt", "tool": "use_figma", "counted": True, "ts": 10}, tmp_path)
    with open(tmp_path / ledger.LEDGER, "a") as fh:
        fh.write('{"ts": 11, "event": "att')  # torn write
        fh.write("\nnot json\n[1,2]\n")
    ledger.append({"event": "outcome", "tool": "use_figma", "ts": 20}, tmp_path)
    entries = ledger.read_entries(tmp_path)
    assert [e["ts"] for e in entries] == [10, 20]
    assert [e["ts"] for e in ledger.read_entries(tmp_path, since=15)] == [20]
    assert ledger.read_entries(tmp_path / "empty") == []


def _writer(cache: str, n: int) -> None:
    for i in range(n):
        ledger.append({"event": "attempt", "tool": "x", "ts": float(i)}, Path(cache))


def test_concurrent_appends_do_not_interleave(tmp_path):
    procs = [multiprocessing.Process(target=_writer, args=(str(tmp_path), 50)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert len(ledger.read_entries(tmp_path)) == 200


def test_compact_drops_old_entries(tmp_path):
    for ts in (0, 100, 200_000, 300_000):
        ledger.append({"event": "attempt", "ts": ts}, tmp_path)
    kept = ledger.compact(tmp_path, now=300_000)
    assert kept == 2
    assert [e["ts"] for e in ledger.read_entries(tmp_path)] == [200_000, 300_000]


def test_activate_deactivate_and_hook_copy(tmp_path):
    marker = ledger.activate("r1", tmp_path / "out", DEFAULTS["budget"], now=1000, cache=tmp_path)
    assert marker["run_id"] == "r1" and marker["budget"]["per_minute"] == 8
    hook = tmp_path / ledger.HOOK_NAME
    assert hook.read_bytes() == ledger.HOOK_SOURCE.read_bytes()
    again = ledger.activate("r1", tmp_path / "out", DEFAULTS["budget"], now=2000, cache=tmp_path)
    assert again["started"] == 1000 and again["refreshed"] == 2000
    assert ledger.marker_is_fresh(ledger.read_marker(tmp_path), now=2000 + 3600)
    assert not ledger.marker_is_fresh(ledger.read_marker(tmp_path), now=2000 + 7 * 3600)
    assert not ledger.deactivate("other", tmp_path)
    assert ledger.deactivate("r1", tmp_path)
    assert ledger.read_marker(tmp_path) is None
    assert not ledger.deactivate(None, tmp_path)


def test_register_rate_limit_and_reset(tmp_path):
    s = ledger.register_rate_limit(30, now=100, run_id="r", tool="use_figma", cache=tmp_path)
    assert s["backoff_until"] == 130 and s["consecutive_rate_limits"] == 1
    s = ledger.register_rate_limit(10, now=105, cache=tmp_path)
    assert s["backoff_until"] == 130  # never shortened
    assert s["consecutive_rate_limits"] == 2
    backoffs = [e for e in ledger.read_entries(tmp_path) if e["event"] == "backoff"]
    assert len(backoffs) == 2 and not backoffs[0]["counted"]
    ledger.clear_rate_limit_streak(tmp_path)
    assert ledger.read_state(tmp_path)["consecutive_rate_limits"] == 0


def test_corrupt_state_and_marker_are_ignored(tmp_path):
    (tmp_path / ledger.STATE).write_text("{oops")
    (tmp_path / ledger.MARKER).write_text("[]")
    assert ledger.read_state(tmp_path) == {}
    assert ledger.read_marker(tmp_path) is None
