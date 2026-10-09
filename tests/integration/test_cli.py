"""End-to-end checks of the dc.py command-line wiring (subprocess, no Figma)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DC = ROOT / "skills" / "diagram-codebase" / "scripts" / "dc.py"
FIXTURE = ROOT / "tests" / "fixtures" / "a_python_app"


def run(
    tmp_path: Path, *argv: str, stdin: str | None = None, out: Path | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, "DIAGRAM_CODEBASE_CACHE": str(tmp_path / "cache")}
    cmd = [
        sys.executable,
        str(DC),
        "--repo",
        str(FIXTURE),
        "--out",
        str(out or tmp_path / "out"),
        *argv,
    ]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, timeout=300)


def as_json(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_help_lists_every_command(tmp_path):
    proc = subprocess.run([sys.executable, str(DC), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0
    for cmd in ("args", "init", "scan", "merge", "plan", "review", "confirm", "next", "record",
                "status", "budget", "update-diff", "summary", "run", "check-findings"):  # fmt: skip
        assert cmd in proc.stdout


def test_args_from_stdin_with_quotes_and_global_options(tmp_path):
    raw = '--depth deep --focus "it\'s \\"repo\\"" --type dataflow,algo extra hint\n'
    parsed = as_json(run(tmp_path, "args", stdin=raw))
    assert parsed["depth"] == "deep"
    assert parsed["focus"] == 'it\'s "repo"'
    assert parsed["types"] == ["dataflow", "algorithm"]
    assert parsed["hint"] == "extra hint"
    assert parsed["errors"] == []


def test_args_help_and_errors(tmp_path):
    assert as_json(run(tmp_path, "args", "--help"))["help"] is True
    assert as_json(run(tmp_path, "args", "--depth wrong"))["errors"]


def test_dry_run_writes_artifacts_and_never_touches_figma(tmp_path):
    out = tmp_path / "out"
    result = as_json(run(tmp_path, "run", "--dry-run"))
    assert "no Figma calls" in result["note"]
    assert result["plan"]["diagrams"]
    assert (out / ".gitignore").read_text().strip() == "*"
    for name in ("inventory.json", "model.json", "validation.json", "plan.json", "manifest.json"):
        assert (out / name).is_file(), name
    plan = json.loads((out / "plan.json").read_text())
    for spec in plan["diagrams"]:
        text = (out / "mermaid" / f"{spec['id']}.mmd").read_text()
        assert text.strip() and spec["content_hash"]
    assert (out / "publish" / "review.md").is_file()
    assert not (tmp_path / "cache" / "active.json").exists()


def test_run_without_dry_run_flag_is_rejected(tmp_path):
    proc = run(tmp_path, "run")
    assert proc.returncode == 1 and "dry-run" in proc.stderr


def test_step_by_step_flow_until_first_figma_action(tmp_path):
    init = as_json(run(tmp_path, "init", "--args-json", json.dumps({"depth": "overview"})))
    assert init["mode"] == "fresh"
    assert as_json(run(tmp_path, "scan"))["brief"]["repo"] == "a_python_app"
    assert as_json(run(tmp_path, "merge"))["ok"] is True
    as_json(run(tmp_path, "plan"))
    review = run(tmp_path, "review")
    assert review.returncode == 0 and "# Publish review" in review.stdout
    assert as_json(run(tmp_path, "next"))["kind"] == "confirm"
    assert as_json(run(tmp_path, "confirm"))["confirmed"] is True
    action = as_json(run(tmp_path, "next"))
    assert action["kind"] in ("whoami", "generate_diagram")
    assert action["action_id"]
    status = as_json(run(tmp_path, "status"))
    assert status["run_id"] == init["run_id"]
    budget = as_json(run(tmp_path, "budget"))
    assert "not Figma's quota" in budget["note"]
    summary = run(tmp_path, "summary")
    assert summary.returncode == 0 and (tmp_path / "out" / "REPORT.md").is_file()


def test_record_requires_result_or_error(tmp_path):
    as_json(run(tmp_path, "init"))
    proc = run(tmp_path, "record", "--action", "a-9999")
    assert proc.returncode == 1 and proc.stderr.startswith("dc.py: error:")


def test_second_init_resumes(tmp_path):
    first = as_json(run(tmp_path, "init"))
    as_json(run(tmp_path, "scan"))
    second = as_json(run(tmp_path, "init", "--args-json", json.dumps({"resume": True})))
    assert second["mode"] == "resume"
    assert second["run_id"] == first["run_id"]
