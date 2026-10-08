from __future__ import annotations

import json

import pytest
from diagram_codebase.common import DCError
from diagram_codebase.state import manifest as mf


def plan(*items):
    return {
        "diagrams": [
            {
                "id": i,
                "title": i.title(),
                "type": "master",
                "renderer": "flowchart",
                "row": r,
                "priority": p,
                "content_hash": h,
            }
            for i, r, p, h in items
        ]
    }


def fresh(tmp_path):
    return mf.new_manifest(repo_name="shop", repo_root=tmp_path, revision="abc", options={})


def test_new_manifest_schema(tmp_path):
    m = fresh(tmp_path)
    assert m["schema_version"] == mf.SCHEMA_VERSION
    assert set(m["phases"]) == set(mf.PHASES)
    assert m["figma"] == {
        "file_key": None,
        "file_url": None,
        "legend_section_id": None,
        "legend_hash": None,
        "ignore_ids": [],
    }
    assert m["confirmed"] is False and m["pending_action"] is None
    assert len(m["repo"]["root_hash"]) == 16 and str(tmp_path) not in json.dumps(m["repo"])


def test_save_load_roundtrip_atomic(tmp_path):
    m = fresh(tmp_path)
    mf.set_phase(m, "scan", "done")
    mf.add_event(m, "note", "hello", extra=1)
    mf.save(tmp_path, m)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".manifest")]
    loaded = mf.load(tmp_path)
    assert loaded["phases"]["scan"]["status"] == "done"
    assert loaded["events"][-1]["extra"] == 1
    with pytest.raises(ValueError):
        mf.set_phase(m, "bogus", "done")


def test_load_missing_and_bad_schema(tmp_path):
    assert mf.load(tmp_path) is None
    with pytest.raises(DCError):
        mf.load_required(tmp_path)
    (tmp_path / "manifest.json").write_text('{"schema_version": 99}')
    with pytest.raises(DCError):
        mf.load(tmp_path)


def test_reconcile_new_unchanged_changed_removed(tmp_path):
    m = fresh(tmp_path)
    s = mf.reconcile_plan(m, plan(("a", 0, 0, "h1"), ("b", 1, 5, "h2"), ("c", 1, 6, "h3")))
    assert s["added"] == ["a", "b", "c"]
    assert m["diagrams"]["b"]["slot"] == 0 and m["diagrams"]["c"]["slot"] == 1
    m["diagrams"]["a"].update(status="verified", section_id="1:1")
    m["diagrams"]["b"].update(status="placed", section_id="1:2", attempts=1)
    m["diagrams"]["c"].update(status="generated", attempts=1)
    s = mf.reconcile_plan(
        m, plan(("a", 0, 0, "h1"), ("b", 1, 5, "h2-new"), ("c", 1, 6, "h3-new"), ("d", 2, 9, "h4"))
    )
    assert s == {"added": ["d"], "unchanged": ["a"], "stale": ["b", "c"], "obsolete": []}
    assert m["diagrams"]["a"]["status"] == "verified"
    b = m["diagrams"]["b"]
    assert b["status"] == "stale" and b["replaced_section_id"] == "1:2" and b["attempts"] == 0
    assert b["content_hash"] == "h2-new"
    c = m["diagrams"]["c"]  # unplaced content stays generated until it is placed
    assert c["status"] == "generated" and c["next_content_hash"] == "h3-new"
    s = mf.reconcile_plan(m, plan(("d", 2, 9, "h4")))
    assert s["obsolete"] == ["a", "b", "c"]
    assert m["diagrams"]["a"]["section_id"] == "1:1"  # kept so the section can be deleted


def test_reconcile_obsolete_returning(tmp_path):
    m = fresh(tmp_path)
    mf.reconcile_plan(m, plan(("a", 0, 0, "h1")))
    m["diagrams"]["a"].update(status="placed", section_id="1:1")
    mf.reconcile_plan(m, plan())
    assert m["diagrams"]["a"]["status"] == "obsolete"
    mf.reconcile_plan(m, plan(("a", 0, 0, "h1")))
    assert m["diagrams"]["a"]["status"] == "placed"


def test_reconcile_changed_never_placed_goes_pending(tmp_path):
    m = fresh(tmp_path)
    mf.reconcile_plan(m, plan(("a", 0, 0, "h1")))
    m["diagrams"]["a"].update(status="failed", attempts=2)
    mf.reconcile_plan(m, plan(("a", 0, 0, "h2")))
    assert m["diagrams"]["a"]["status"] == "pending" and m["diagrams"]["a"]["attempts"] == 0


def test_init_run_modes(tmp_path):
    m, mode = mf.init_run(tmp_path, repo_root=tmp_path, revision="r1", options={})
    assert mode == "fresh"
    m["figma"]["file_key"] = "K"
    m["diagrams"]["a"] = mf.diagram_entry({"id": "a", "title": "A"}, 0)
    m["stop"] = {"reason": "auth"}
    mf.save(tmp_path, m)
    resumed, mode = mf.init_run(
        tmp_path, repo_root=tmp_path, revision="r2", options={"resume": True, "yes": True}
    )
    assert mode == "resume" and resumed["run_id"] == m["run_id"] and resumed["stop"] is None
    assert resumed["options"]["yes"] is True and resumed["revision"] == "r1"
    updated, mode = mf.init_run(
        tmp_path, repo_root=tmp_path, revision="r2", options={"update": True}
    )
    assert mode == "update" and updated["run_id"] != m["run_id"]
    assert updated["figma"]["file_key"] == "K" and "a" in updated["diagrams"]
    assert updated["previous_revision"] == "r1" and updated["revision"] == "r2"


def test_remaining_work_order(tmp_path):
    m = fresh(tmp_path)
    mf.reconcile_plan(m, plan(("z", 0, 0, "h"), ("a", 1, 5, "h"), ("done", 1, 1, "h")))
    m["diagrams"]["done"]["status"] = "verified"
    assert [r["id"] for r in mf.remaining_work(m)] == ["z", "a"]
