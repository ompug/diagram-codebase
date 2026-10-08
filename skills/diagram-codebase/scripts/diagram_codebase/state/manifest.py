"""Run state (`<out>/manifest.json`): phases, diagrams, Figma ids, pending action, events.

Schema (schema_version 1):
{schema_version, run_id, created, updated, repo{name, root_hash}, revision, options,
 phases{scan,analysis,merge,plan,publish}: {status, at},
 figma{file_key, file_url, legend_section_id, legend_hash, ignore_ids[]},
 diagrams{id: {title, type, renderer, content_hash, status, attempts, place_attempts,
               section_id, url, row, slot, priority, errors[], replaced_section_id?,
               next_content_hash?, verify?}},
 pending_action, actions_log[], events[], confirmed, action_seq, stop, verify,
 hooks_inactive, uncertain (diagram id whose generate outcome is unknown), plan_sig}

Diagram status: pending -> generated -> placed -> verified, or failed / stale / obsolete.
All writes go through common.write_json (atomic).
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any

from ..common import DCError, read_json, sha256_text, utc_now, write_json

SCHEMA_VERSION = 1
PHASES = ("scan", "analysis", "merge", "plan", "publish")
MAX_EVENTS = 200
MAX_LOG = 500


def manifest_path(out_dir: Path | str) -> Path:
    return Path(out_dir) / "manifest.json"


def new_run_id() -> str:
    """Run ids are the one place time/randomness is allowed (CODING_STANDARDS 8)."""
    return time.strftime("r%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)


def new_manifest(
    *,
    repo_name: str,
    repo_root: Path | str,
    revision: str | None,
    options: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or new_run_id(),
        "created": now,
        "updated": now,
        "repo": {
            "name": repo_name,
            "root_hash": sha256_text(os.path.realpath(str(repo_root)))[:16],
        },
        "revision": revision,
        "options": dict(options or {}),
        "phases": {p: {"status": "pending", "at": None} for p in PHASES},
        "figma": {
            "file_key": None,
            "file_url": None,
            "legend_section_id": None,
            "legend_hash": None,
            "ignore_ids": [],
        },
        "diagrams": {},
        "pending_action": None,
        "actions_log": [],
        "events": [],
        "confirmed": False,
        "action_seq": 0,
        "stop": None,
        "verify": None,
        "hooks_inactive": False,
        "uncertain": None,
        "plan_sig": None,
    }


def _upgrade(m: dict[str, Any]) -> dict[str, Any]:
    """Fill keys added after a manifest was written (forward-compatible loads)."""
    template = new_manifest(repo_name="", repo_root=".", revision=None, run_id="x")
    for key, value in template.items():
        if key not in m:
            m[key] = value
    for key, value in template["figma"].items():
        m["figma"].setdefault(key, value)
    for p in PHASES:
        m["phases"].setdefault(p, {"status": "pending", "at": None})
    for d in m["diagrams"].values():
        d.setdefault("errors", [])
        d.setdefault("attempts", 0)
        d.setdefault("place_attempts", 0)
    return m


def load(out_dir: Path | str) -> dict[str, Any] | None:
    path = manifest_path(out_dir)
    if not path.is_file():
        return None
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise DCError(f"{path} has an unsupported schema; remove it to start a fresh run")
    return _upgrade(data)


def load_required(out_dir: Path | str) -> dict[str, Any]:
    m = load(out_dir)
    if m is None:
        raise DCError(f"no manifest in {out_dir}; run `dc.py init` first")
    return m


def save(out_dir: Path | str, m: dict[str, Any]) -> None:
    m["updated"] = utc_now()
    m["events"] = m["events"][-MAX_EVENTS:]
    m["actions_log"] = m["actions_log"][-MAX_LOG:]
    write_json(manifest_path(out_dir), m)


def set_phase(m: dict[str, Any], phase: str, status: str) -> None:
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase}")
    m["phases"][phase] = {"status": status, "at": utc_now()}


def add_event(m: dict[str, Any], kind: str, message: str, **data: Any) -> dict[str, Any]:
    event = {"at": utc_now(), "kind": kind, "message": message, **data}
    m["events"].append(event)
    return event


# --------------------------------------------------------------- plan reconcile


def diagram_entry(spec: dict[str, Any], slot: int) -> dict[str, Any]:
    return {
        "title": spec.get("title") or spec["id"],
        "type": spec.get("type"),
        "renderer": spec.get("renderer"),
        "content_hash": spec.get("content_hash"),
        "status": "pending",
        "attempts": 0,
        "place_attempts": 0,
        "section_id": None,
        "url": None,
        "row": int(spec.get("row") or 0),
        "slot": slot,
        "priority": spec.get("priority", 0),
        "errors": [],
    }


def _slots(diagrams: list[dict[str, Any]]) -> dict[str, int]:
    """Position of each diagram within its row, by (priority, id)."""
    slots: dict[str, int] = {}
    per_row: dict[int, int] = {}
    for spec in sorted(diagrams, key=lambda s: (s.get("priority", 0), s["id"])):
        row = int(spec.get("row") or 0)
        slots[spec["id"]] = per_row.get(row, 0)
        per_row[row] = slots[spec["id"]] + 1
    return slots


def reconcile_plan(m: dict[str, Any], plan: dict[str, Any]) -> dict[str, list[str]]:
    """Bring manifest diagrams in line with a (new) plan.

    new -> pending; same hash -> keep status; changed hash -> stale (old section kept as
    replaced_section_id) or, if never placed, pending again; removed -> obsolete.
    """
    specs = plan.get("diagrams") or []
    slots = _slots(specs)
    seen: set[str] = set()
    summary: dict[str, list[str]] = {"added": [], "unchanged": [], "stale": [], "obsolete": []}
    for spec in specs:
        did = spec["id"]
        seen.add(did)
        cur = m["diagrams"].get(did)
        if cur is None:
            m["diagrams"][did] = diagram_entry(spec, slots[did])
            summary["added"].append(did)
            continue
        cur.update(
            title=spec.get("title") or did,
            type=spec.get("type"),
            renderer=spec.get("renderer"),
            row=int(spec.get("row") or 0),
            slot=slots[did],
            priority=spec.get("priority", 0),
        )
        new_hash = spec.get("content_hash")
        if cur["status"] == "obsolete":
            # Back in the plan: re-place unless the old section survived unchanged.
            same = cur.get("content_hash") == new_hash and cur.get("section_id")
            cur["status"] = "placed" if same else ("stale" if cur.get("section_id") else "pending")
            if not same and cur.get("section_id"):
                cur["replaced_section_id"] = cur["section_id"]
            cur["content_hash"] = new_hash
            summary["stale" if cur["status"] == "stale" else "added"].append(did)
            continue
        if cur.get("content_hash") == new_hash:
            summary["unchanged"].append(did)
            continue
        if cur["status"] == "generated":
            # Unplaced content is on the canvas: place it first, then treat it as stale.
            cur["next_content_hash"] = new_hash
            summary["stale"].append(did)
        elif cur.get("section_id"):
            cur.update(
                status="stale",
                content_hash=new_hash,
                replaced_section_id=cur.get("replaced_section_id") or cur["section_id"],
                attempts=0,
                place_attempts=0,
                verify=None,
            )
            summary["stale"].append(did)
        else:
            cur.update(status="pending", content_hash=new_hash, attempts=0, place_attempts=0)
            summary["stale"].append(did)
    for did, cur in m["diagrams"].items():
        if did not in seen and cur["status"] != "obsolete":
            cur["status"] = "obsolete"
            summary["obsolete"].append(did)
    for key in summary:
        summary[key].sort()
    return summary


def init_run(
    out_dir: Path | str,
    *,
    repo_root: Path | str,
    revision: str | None,
    options: dict[str, Any],
    repo_name: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Create a fresh manifest, or reuse the existing one for --resume/--update.

    Returns (manifest, mode) with mode fresh|resume|update. A resume keeps everything;
    an update keeps Figma ids and diagrams (reconcile_plan runs after the new plan) and
    starts a new run id. A plain run over an existing manifest starts fresh but keeps
    the FigJam file so diagrams are not duplicated into a second file.
    """
    existing = load(out_dir)
    name = repo_name or Path(os.path.realpath(str(repo_root))).name
    if existing is not None and options.get("resume"):
        existing["stop"] = None
        existing["options"] = {**existing.get("options", {}), **_sticky(options)}
        return existing, "resume"
    m = new_manifest(repo_name=name, repo_root=repo_root, revision=revision, options=options)
    if existing is not None and options.get("update"):
        m["figma"] = existing["figma"]
        m["diagrams"] = existing["diagrams"]
        m["previous_revision"] = existing.get("revision")
        m["confirmed"] = False
        return m, "update"
    return m, "fresh"


def _sticky(options: dict[str, Any]) -> dict[str, Any]:
    """Options a --resume invocation may change (others stay from the original run)."""
    return {k: options[k] for k in ("yes", "verify_visual") if options.get(k)}


def remaining_work(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Diagrams not yet finished, in publish order."""
    todo = [
        {"id": did, "title": d["title"], "status": d["status"]}
        for did, d in m["diagrams"].items()
        if d["status"] in ("pending", "generated", "stale")
    ]
    order = {did: (d.get("priority", 0), did) for did, d in m["diagrams"].items()}
    return sorted(todo, key=lambda t: order[t["id"]])
