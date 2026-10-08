"""Tests for the optional Node-based Mermaid parser check."""

from __future__ import annotations

import pytest
from diagram_codebase.mermaid import parsecheck

needs_node = pytest.mark.skipif(
    not parsecheck.available(), reason="mermaid-check (node + deps) not installed"
)


def test_unavailable_when_tool_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(parsecheck.ENV_VAR, str(tmp_path / "nope"))
    assert parsecheck.available() is False
    result = parsecheck.check([{"id": "a", "text": "flowchart LR\n a --> b"}])
    assert result["status"] == "unavailable"
    assert result["results"] == {}
    assert result["detail"]


def test_unavailable_without_node(monkeypatch):
    monkeypatch.setattr(parsecheck.shutil, "which", lambda _name: None)
    assert parsecheck.unavailable_reason() == "node not found on PATH"
    assert parsecheck.check([{"id": "a", "text": "x"}])["status"] == "unavailable"


def test_env_var_accepts_check_mjs_path(monkeypatch, tmp_path):
    monkeypatch.setenv(parsecheck.ENV_VAR, str(tmp_path / "check.mjs"))
    assert parsecheck.tool_dir() == tmp_path


def test_missing_deps_reported(monkeypatch, tmp_path):
    (tmp_path / "check.mjs").write_text("// stub\n")
    monkeypatch.setenv(parsecheck.ENV_VAR, str(tmp_path))
    monkeypatch.setattr(parsecheck.shutil, "which", lambda _name: "/usr/bin/node")
    assert "mermaid is not installed" in (parsecheck.unavailable_reason() or "")


@needs_node
def test_real_parser_accepts_and_rejects():
    result = parsecheck.check(
        [
            {"id": "good", "text": 'flowchart LR\n  a["Hello (x)"] -->|"calls"| b["B"]\n'},
            {"id": "bad", "text": "flowchart LR\n  a[Hello (x)] --> b\n"},
            {"id": "seq", "text": "sequenceDiagram\n  participant API\n  API->>Db: query\n"},
            {"id": "erd", "text": 'erDiagram\n  USER ||--o{ ORDER : "places"\n'},
            {"id": "state", "text": "stateDiagram-v2\n  [*] --> Idle\n  Idle --> [*]\n"},
        ]
    )
    assert result["status"] == "ok", result
    res = result["results"]
    assert (
        res["good"] is None and res["seq"] is None and res["erd"] is None and res["state"] is None
    )
    assert res["bad"] and "Parse error" in res["bad"]


@needs_node
def test_empty_items():
    assert parsecheck.check([]) == {"status": "ok", "results": {}, "detail": ""}
