"""Figma driver: `next_action` / `record` (DESIGN.md "Figma driver protocol").

Claude never decides what to send to Figma. `next_action` returns exactly one
action (the exact tool and params); Claude calls the tool and passes the raw result
to `record`. Counted actions are persisted as `pending_action` before they are
returned, so a crash between the two leaves an *uncertain* action that is
reconciled (get_figjam, or asking the user when no file exists yet) before any retry.

Publish order: confirm -> per diagram (priority order) generate_diagram then
use_figma place_section -> legend -> delete obsolete sections -> get_figjam verify ->
get_screenshot for mismatches (or all with --verify-visual) -> done.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common import DCError, read_json, sha256_text, utc_now
from ..state import manifest as mf
from . import ledger, ratelimit, scripts

ARCH_LAYOUT_CODE = "FIGMA_DIAGRAM_2026"
HOOK_DENY_TAG = "diagram-codebase:"  # prefix of every figma_gate.py deny reason
VERIFY_THRESHOLD = 0.6
MAX_LABELS = 40
RESUME = "/diagram-codebase --resume"
SKILLS = {
    "generate_diagram": ["figma:figma-generate-diagram"],
    "use_figma": ["figma:figma-use", "figma:figma-use-figjam"],
    "get_figjam": ["figma:figma-use-figjam"],
    "get_screenshot": ["figma:figma-use-figjam"],
}
_BOARD_RE = re.compile(r"https?://(?:www\.)?figma\.com/board/([A-Za-z0-9]+)(?:/[^\s\"'<>)\]\\]*)?")
_RETRYABLE_STOPS = ("budget", "rate_limit")


@dataclass
class Ctx:
    out_dir: Path
    config: dict[str, Any]
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    cache: Path | None = None
    rng: Callable[[], float] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def budget(self) -> dict[str, Any]:
        return self.config["budget"]

    @property
    def max_attempts(self) -> int:
        return int(self.config["figma"]["max_attempts_per_diagram"])


def _ctx(out_dir, config, now, sleep, cache, rng=None) -> Ctx:
    return Ctx(
        Path(out_dir),
        config,
        now=now or time.time,
        sleep=sleep or time.sleep,
        cache=cache,
        rng=rng,
    )


# ======================================================================= public


def next_action(
    out_dir: Path | str,
    config: dict[str, Any],
    *,
    now: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    cache: Path | None = None,
) -> dict[str, Any]:
    """Return the next action and persist manifest state (pending action first)."""
    ctx = _ctx(out_dir, config, now, sleep, cache)
    m = mf.load_required(ctx.out_dir)
    plan = _load_plan(ctx.out_dir)
    _sync_plan(m, plan)
    action = _decide(ctx, m, plan)
    if m.get("hooks_inactive") and action.get("tool"):
        action.setdefault("notes", []).append(
            "hooks inactive; Figma call counts are driver estimates"
        )
    mf.save(ctx.out_dir, m)
    return action


def record(
    out_dir: Path | str,
    action_id: str,
    result_text: str | None,
    error: str | None,
    config: dict[str, Any],
    *,
    now: Callable[[], float] | None = None,
    cache: Path | None = None,
    rng: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Record the outcome of the pending action. Idempotent for already-recorded ids."""
    ctx = _ctx(out_dir, config, now, None, cache, rng)
    m = mf.load_required(ctx.out_dir)
    pa = m.get("pending_action")
    if not pa or pa.get("action_id") != action_id:
        logged = _log_entry(m, action_id)
        if logged and logged.get("outcome") not in (None, "uncertain"):
            return {"recorded": action_id, "duplicate": True, "outcome": logged["outcome"]}
        current = pa.get("action_id") if pa else "none"
        raise DCError(
            f"action {action_id} is not the pending action (pending: {current}); "
            "run `dc.py next` for the current action"
        )
    plan = _load_plan(ctx.out_dir)
    m["pending_action"] = None
    result_text = result_text or ""
    if pa.get("counted"):
        _account(ctx, m, pa, result_text, error)
    handler = _HANDLERS[pa["purpose"]]
    out = handler(ctx, m, plan, pa, result_text, error)
    entry = _log_entry(m, action_id)
    if entry is not None:
        entry.update(
            recorded_at=utc_now(), outcome=out.get("outcome"), error_class=out.get("class")
        )
    out.setdefault("recorded", action_id)
    out.setdefault("next", "python3 dc.py next")
    mf.save(ctx.out_dir, m)
    return out


def confirm(out_dir: Path | str) -> dict[str, Any]:
    """User approved publishing (`dc.py confirm`)."""
    m = mf.load_required(out_dir)
    m["confirmed"] = True
    mf.add_event(m, "confirm", "user approved publishing to Figma")
    mf.save(out_dir, m)
    return {"confirmed": True, "next": "python3 dc.py next"}


def budget_report(
    config: dict[str, Any], *, now: float | None = None, cache: Path | None = None
) -> dict[str, Any]:
    """Local ledger estimates for `dc.py budget` (never Figma's quota)."""
    now = time.time() if now is None else now
    budget = config["budget"]
    entries = ledger.read_entries(cache, since=now - budget["day_window_seconds"])
    state = ledger.read_state(cache)
    marker = ledger.read_marker(cache)
    backoff = max(0.0, float(state.get("backoff_until") or 0) - now)
    return {
        **ratelimit.usage(entries, now, budget),
        "backoff_seconds_left": round(backoff, 1),
        "consecutive_rate_limits": int(state.get("consecutive_rate_limits") or 0),
        "hook_entries_24h": sum(1 for e in entries if e.get("source") == "hook"),
        "driver_estimates_24h": sum(1 for e in entries if e.get("event") == "driver_estimate"),
        "active_run": marker.get("run_id") if ledger.marker_is_fresh(marker, now) else None,
    }


def status(out_dir: Path | str) -> dict[str, Any]:
    m = mf.load_required(out_dir)
    return {
        "run_id": m["run_id"],
        "phases": m["phases"],
        "confirmed": m["confirmed"],
        "file_url": m["figma"]["file_url"],
        "diagrams": {
            did: {k: d.get(k) for k in ("title", "status", "attempts", "section_id", "verify")}
            for did, d in sorted(m["diagrams"].items())
        },
        "pending_action": _brief(m.get("pending_action")),
        "stop": (m.get("stop") or {}).get("action"),
        "hooks_inactive": m.get("hooks_inactive", False),
    }


# ================================================================ plan + state


def _load_plan(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "plan.json"
    if not path.is_file():
        raise DCError(f"{path} not found; run `dc.py plan` first")
    plan = read_json(path)
    if not isinstance(plan, dict) or not isinstance(plan.get("diagrams"), list):
        raise DCError(f"{path} has no diagrams list; re-run `dc.py plan`")
    return plan


def _sync_plan(m: dict[str, Any], plan: dict[str, Any]) -> None:
    """Reconcile the manifest whenever plan.json content changed since last seen."""
    sig = sha256_text(
        json.dumps(sorted((d["id"], d.get("content_hash")) for d in plan["diagrams"]))
    )
    if m.get("plan_sig") != sig:
        summary = mf.reconcile_plan(m, plan)
        m["plan_sig"] = sig
        mf.add_event(m, "plan", "manifest reconciled with plan.json", **summary)


def _specs(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["id"]: d for d in plan["diagrams"]}


def _ordered(m: dict[str, Any]) -> list[str]:
    return sorted(m["diagrams"], key=lambda did: (m["diagrams"][did].get("priority", 0), did))


def _log_entry(m: dict[str, Any], action_id: str) -> dict[str, Any] | None:
    for entry in reversed(m["actions_log"]):
        if entry.get("action_id") == action_id:
            return entry
    return None


def _brief(action: dict[str, Any] | None) -> dict[str, Any] | None:
    if not action:
        return None
    return {k: action.get(k) for k in ("action_id", "kind", "tool", "diagram_id", "purpose")}


def _err(d: dict[str, Any], cls: str, text: str) -> None:
    d["errors"].append(
        {"at": utc_now(), "class": cls, "attempt": d.get("attempts", 0), "message": text[:300]}
    )


# ===================================================================== decide


def _decide(ctx: Ctx, m: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    stop = m.get("stop")
    if stop:
        if stop.get("reason") not in _RETRYABLE_STOPS:
            return dict(stop["action"])
        m["stop"] = None  # budget/backoff stops are re-evaluated below

    pa = m.get("pending_action")
    if pa:
        if pa["kind"] == "ask_user":
            return _public(pa)
        _mark_log(m, pa["action_id"], "uncertain")
        m["pending_action"] = None
        if pa["purpose"] == "generate":
            m["uncertain"] = pa["diagram_id"]
            mf.add_event(
                m, "uncertain", f"outcome of {pa['action_id']} unknown", diagram=pa["diagram_id"]
            )
        # Everything else is retry-safe (scripts are idempotent, reads are reads).

    uncertain = m.get("uncertain")
    if uncertain:
        if m["figma"]["file_key"]:
            return _reconcile_action(ctx, m, uncertain)
        return _ask_file_url(ctx, m, uncertain)

    if not (m["confirmed"] or m["options"].get("yes")):
        return _confirm_action(m)

    specs = _specs(plan)
    for did in _ordered(m):
        d = m["diagrams"][did]
        if d["status"] == "generated":
            return _place_action(ctx, m, did)
    for did in _ordered(m):
        d = m["diagrams"][did]
        if d["status"] not in ("pending", "stale"):
            continue
        if d["attempts"] >= ctx.max_attempts:
            d["status"] = "failed"
            mf.add_event(m, "failed", f"{did}: no attempts left", diagram=did)
            continue
        return _generate_action(ctx, m, specs[did], did)

    legend = _legend_action(ctx, m, specs)
    if legend:
        return legend
    delete = _delete_action(ctx, m)
    if delete:
        return delete
    verify = _verify_action(ctx, m)
    if verify:
        return verify
    shot = _screenshot_action(ctx, m)
    if shot:
        return shot
    return _done_action(ctx, m)


def _public(pa: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in pa.items() if k not in ("emitted_ts", "counted", "legend_hash")}


def _mark_log(m: dict[str, Any], action_id: str, outcome: str) -> None:
    entry = _log_entry(m, action_id)
    if entry is not None and entry.get("outcome") is None:
        entry["outcome"] = outcome


def _record_hint(action_id: str) -> str:
    return (
        "Save the raw tool result to a file, then run: python3 dc.py record --action "
        f'{action_id} --result-file <file>  (if the tool failed, add --error "<error text>")'
    )


def _emit(
    ctx: Ctx,
    m: dict[str, Any],
    *,
    kind: str,
    tool: str | None,
    params: dict[str, Any],
    purpose: str,
    explain: str,
    diagram_id: str | None = None,
    need: int = 1,
    record_hint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    counted = bool(tool) and ratelimit.is_counted(tool, ctx.budget)
    if counted:
        gate = _budget_gate(ctx, m, need)
        if gate is not None:
            return gate
        ledger.activate(m["run_id"], ctx.out_dir, ctx.budget, now=ctx.now(), cache=ctx.cache)
    m["action_seq"] = int(m.get("action_seq") or 0) + 1
    action_id = f"a-{m['action_seq']:04d}"
    action: dict[str, Any] = {
        "action_id": action_id,
        "kind": kind,
        "tool": tool,
        "params": params,
        "skill": SKILLS[tool][0] if tool in SKILLS else None,
        "skills": SKILLS.get(tool or "", []),
        "diagram_id": diagram_id,
        "purpose": purpose,
        "explain": explain,
        "record_hint": (record_hint or _record_hint(action_id)).replace("{id}", action_id),
        **(extra or {}),
    }
    m["pending_action"] = {**action, "emitted_ts": ctx.now(), "counted": counted}
    m["actions_log"].append(
        {
            "action_id": action_id,
            "kind": kind,
            "tool": tool,
            "purpose": purpose,
            "diagram_id": diagram_id,
            "emitted_at": utc_now(),
            "outcome": None,
        }
    )
    return action


def _budget_gate(ctx: Ctx, m: dict[str, Any], need: int) -> dict[str, Any] | None:
    budget = ctx.budget
    need = max(1, min(need, int(budget["per_minute"])))
    max_sleep = float(budget["max_hook_sleep_seconds"])
    slept = 0.0
    for _ in range(4):
        now = ctx.now()
        entries = ledger.read_entries(ctx.cache, since=now - budget["day_window_seconds"])
        state = ledger.read_state(ctx.cache)
        backoff_until = float(state.get("backoff_until") or 0)
        dec = ratelimit.decide(entries, now, budget, backoff_until=backoff_until, need=need)
        if dec["decision"] == "allow":
            return None
        if dec["decision"] == "deny":
            return _stop(
                ctx,
                m,
                "budget",
                f"Local Figma call budget reached: {dec['reason']}.",
                f"Nothing was lost; state is saved. Resume later with {RESUME}.",
                usage=dec,
            )
        wait = float(dec["seconds"])
        if slept + wait > max_sleep:
            backoff = backoff_until > now
            return _stop(
                ctx,
                m,
                "rate_limit" if backoff else "budget",
                f"Figma calls must pause for about {int(wait) + 1} s ({dec['reason']}).",
                f"State is saved. Wait a few minutes, then run {RESUME}.",
                usage=dec,
            )
        ctx.sleep(wait)
        slept += wait
    return _stop(ctx, m, "budget", "Figma call budget did not free up.", f"Run {RESUME} later.")


def _stop(
    ctx: Ctx,
    m: dict[str, Any],
    reason: str,
    message: str,
    instructions: str,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    m["action_seq"] = int(m.get("action_seq") or 0) + 1
    action = {
        "action_id": f"a-{m['action_seq']:04d}",
        "kind": "stop",
        "tool": None,
        "params": {},
        "reason": reason,
        "explain": message,
        "instructions": instructions,
        "remaining": mf.remaining_work(m),
        "file_url": m["figma"]["file_url"],
        "resume": RESUME,
    }
    if usage is not None:
        action["usage"] = {
            "minute": usage.get("minute"),
            "day": usage.get("day"),
            "note": "local estimate, not Figma's quota",
        }
    m["stop"] = {"reason": reason, "at": utc_now(), "action": action}
    mf.add_event(m, "stop", message, reason=reason)
    ledger.deactivate(m["run_id"], ctx.cache)
    return action


# ------------------------------------------------------------------- actions


def _confirm_action(m: dict[str, Any]) -> dict[str, Any]:
    todo = [d["title"] for d in m["diagrams"].values() if d["status"] in ("pending", "stale")]
    return {
        "action_id": None,
        "kind": "confirm",
        "tool": None,
        "params": {"review_file": "publish/review.md", "diagrams": todo},
        "explain": "Show the user publish/review.md (everything that will be sent to Figma) "
        "and ask for approval.",
        "record_hint": "If the user approves run: python3 dc.py confirm  then: python3 dc.py "
        "next. If not, stop (nothing was sent).",
    }


def _mermaid_for(ctx: Ctx, did: str, d: dict[str, Any]) -> tuple[str, bool]:
    """Mermaid text for the next attempt; architecture falls back after a failure."""
    base = ctx.out_dir / "mermaid"
    fallback = base / f"{did}.fallback.mmd"
    use_fallback = d.get("renderer") == "architecture" and d["attempts"] >= 1 and fallback.is_file()
    path = fallback if use_fallback else base / f"{did}.mmd"
    if not path.is_file():
        raise DCError(f"{path} not found; re-run `dc.py plan`")
    return path.read_text(encoding="utf-8"), use_fallback


def _generate_action(ctx: Ctx, m: dict[str, Any], spec: dict[str, Any], did: str) -> dict:
    d = m["diagrams"][did]
    text, fallback = _mermaid_for(ctx, did, d)
    params: dict[str, Any] = {
        "name": d["title"],
        "mermaidSyntax": text,
        "userIntent": spec.get("purpose") or f"Codebase architecture documentation: {d['title']}",
    }
    if m["figma"]["file_key"]:
        params["fileKey"] = m["figma"]["file_key"]
    if d.get("renderer") == "architecture" and not fallback:
        params["useArchitectureLayoutCode"] = ARCH_LAYOUT_CODE
    d["fallback_used"] = fallback
    verb = "Regenerating" if d["status"] == "stale" else "Generating"
    where = "in the run's FigJam file" if m["figma"]["file_key"] else "(creates a new FigJam file)"
    note = " using the flowchart fallback" if fallback else ""
    return _emit(
        ctx,
        m,
        kind="generate_diagram",
        tool="generate_diagram",
        params=params,
        purpose="generate",
        diagram_id=did,
        need=2,  # keep room for its placement so content is not left unsectioned
        explain=f"{verb} '{d['title']}' {where}{note} (attempt {d['attempts'] + 1}).",
    )


def _place_action(ctx: Ctx, m: dict[str, Any], did: str) -> dict[str, Any]:
    d = m["diagrams"][did]
    code = scripts.place_section(
        diagram_id=did,
        title=d["title"],
        run_id=m["run_id"],
        content_hash=d.get("content_hash") or "",
        row=d.get("row", 0),
        ignore_ids=m["figma"]["ignore_ids"],
        replace_section_id=d.get("replaced_section_id"),
        gap=int(ctx.config["figma"]["section_gap"]),
    )
    replacing = " replacing its previous version" if d.get("replaced_section_id") else ""
    return _emit(
        ctx,
        m,
        kind="use_figma",
        tool="use_figma",
        params=scripts.use_figma_params(
            m["figma"]["file_key"], code, f"Place diagram '{d['title']}' in a section"
        ),
        purpose="place",
        diagram_id=did,
        explain=f"Placing '{d['title']}' into a titled section on row {d.get('row', 0)}"
        f"{replacing}.",
    )


def _reconcile_action(ctx: Ctx, m: dict[str, Any], did: str) -> dict[str, Any]:
    return _emit(
        ctx,
        m,
        kind="get_figjam",
        tool="get_figjam",
        params=_figjam_params(m),
        purpose="reconcile_generate",
        diagram_id=did,
        explain=f"Checking the FigJam file to see whether '{m['diagrams'][did]['title']}' "
        "already landed before retrying it.",
    )


def _ask_file_url(ctx: Ctx, m: dict[str, Any], did: str) -> dict[str, Any]:
    title = m["diagrams"][did]["title"]
    question = (
        f"An earlier attempt to create the FigJam file for '{title}' may have succeeded. If a "
        f"FigJam file named '{title}' appeared in your Figma drafts, paste its URL; otherwise "
        "reply none."
    )
    return _emit(
        ctx,
        m,
        kind="ask_user",
        tool=None,
        params={"question": question},
        purpose="ask_file_url",
        diagram_id=did,
        explain="Avoiding a duplicate FigJam file: ask the user whether the file exists.",
        record_hint="Write the user's reply (the figma.com/board/... URL, or none) to a file, "
        "then run: python3 dc.py record --action {id} --result-file <file>",
    )


def _figjam_params(m: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {"fileKey": m["figma"]["file_key"]}
    if int((m.get("verify") or {}).get("attempts") or 0) == 0:
        params["nodeId"] = "0:1"  # the board's page; dropped after a failed attempt
    return params


def _categories(specs: dict[str, dict[str, Any]], ids: list[str]) -> list[str]:
    cats: set[str] = set()
    for did in ids:
        spec = specs.get(did) or {}
        for item in (spec.get("nodes") or []) + (spec.get("groups") or []):
            if item.get("category"):
                cats.add(item["category"])
    return sorted(cats)


def _shown(m: dict[str, Any]) -> list[str]:
    return [
        did
        for did in _ordered(m)
        if m["diagrams"][did]["status"] in ("placed", "verified")
        and m["diagrams"][did].get("section_id")
    ]


def _legend_action(ctx: Ctx, m: dict[str, Any], specs: dict[str, Any]) -> dict | None:
    shown = _shown(m)
    if not shown or not m["figma"]["file_key"]:
        return None
    cats = _categories(specs, shown)
    items = [
        {"id": did, "title": m["diagrams"][did]["title"], "type": m["diagrams"][did]["type"]}
        for did in shown
    ]
    rev = m.get("revision")
    lhash = sha256_text(json.dumps([cats, items, rev], sort_keys=True))
    fig = m["figma"]
    if fig.get("legend_hash") == lhash:
        return None
    if fig.get("legend_failed_hash") == lhash:
        return None
    code = scripts.legend_and_index(
        run_id=m["run_id"],
        categories=cats,
        diagrams=items,
        repo_name=m["repo"]["name"],
        revision=rev,
        date=utc_now()[:10],
        gap=int(ctx.config["figma"]["section_gap"]),
    )
    action = _emit(
        ctx,
        m,
        kind="use_figma",
        tool="use_figma",
        params=scripts.use_figma_params(fig["file_key"], code, "Add legend and diagram index"),
        purpose="legend",
        explain="Adding the color legend and diagram index left of the overview row.",
    )
    if m.get("pending_action") and m["pending_action"]["action_id"] == action.get("action_id"):
        m["pending_action"]["legend_hash"] = lhash
    return action


def _delete_action(ctx: Ctx, m: dict[str, Any]) -> dict[str, Any] | None:
    ids = sorted(
        d["section_id"]
        for d in m["diagrams"].values()
        if d["status"] == "obsolete" and d.get("section_id") and not d.get("delete_failed")
    )
    if not ids or not m["figma"]["file_key"]:
        return None
    return _emit(
        ctx,
        m,
        kind="use_figma",
        tool="use_figma",
        params=scripts.use_figma_params(
            m["figma"]["file_key"], scripts.delete_sections(ids), "Remove obsolete diagrams"
        ),
        purpose="delete",
        explain=f"Removing {len(ids)} diagram section(s) that are no longer in the plan.",
        extra={"section_ids": ids},
    )


def _verify_sig(m: dict[str, Any]) -> str:
    shown = [(did, m["diagrams"][did]["section_id"]) for did in _shown(m)]
    return sha256_text(json.dumps(shown))


def _verify_action(ctx: Ctx, m: dict[str, Any]) -> dict[str, Any] | None:
    if not _shown(m):
        return None
    v = m.get("verify") or {}
    sig = _verify_sig(m)
    if v.get("sig") == sig and v.get("status") in ("done", "failed"):
        return None
    if v.get("sig") != sig:
        m["verify"] = {"sig": sig, "status": "pending", "attempts": 0}
    return _emit(
        ctx,
        m,
        kind="get_figjam",
        tool="get_figjam",
        params=_figjam_params(m),
        purpose="verify",
        explain="Reading the board back to verify every diagram section and its labels.",
    )


def _screenshot_action(ctx: Ctx, m: dict[str, Any]) -> dict[str, Any] | None:
    if (m.get("verify") or {}).get("status") != "done":
        return None
    visual = bool(m["options"].get("verify_visual"))
    for did in _shown(m):
        d = m["diagrams"][did]
        v = d.get("verify") or {}
        wanted = v.get("status") == "mismatch" or (visual and v.get("status") == "ok")
        if not wanted or v.get("visual"):
            continue
        return _emit(
            ctx,
            m,
            kind="get_screenshot",
            tool="get_screenshot",
            params={"fileKey": m["figma"]["file_key"], "nodeId": d["section_id"]},
            purpose="screenshot",
            diagram_id=did,
            explain=f"Visual check of '{d['title']}'.",
            record_hint="Look at the screenshot. Write one or two sentences to a file starting "
            "with 'OK:' if the section shows the diagram with readable labels, else "
            "'PROBLEM:' and what is wrong; then run: python3 dc.py record --action {id} "
            "--result-file <file>",
        )
    return None


def _done_action(ctx: Ctx, m: dict[str, Any]) -> dict[str, Any]:
    failed = [did for did, d in m["diagrams"].items() if d["status"] == "failed"]
    any_shown = bool(_shown(m))
    mf.set_phase(m, "publish", "done" if not failed else ("partial" if any_shown else "failed"))
    ledger.deactivate(m["run_id"], ctx.cache)
    diagrams = [
        {
            "id": did,
            "title": d["title"],
            "status": d["status"],
            "verify": (d.get("verify") or {}).get("status", "not run"),
            "errors": [e["message"] for e in d["errors"][-2:]],
        }
        for did, d in ((did, m["diagrams"][did]) for did in _ordered(m))
        if d["status"] != "obsolete"
    ]
    return {
        "action_id": None,
        "kind": "done",
        "tool": None,
        "params": {},
        "file_url": m["figma"]["file_url"],
        "diagrams": diagrams,
        "failed": sorted(failed),
        "explain": "Publishing finished. Run python3 dc.py summary to write REPORT.md.",
    }


# ===================================================================== record


def _account(
    ctx: Ctx, m: dict[str, Any], pa: dict[str, Any], result: str, error: str | None
) -> None:
    """Ledger bookkeeping for a counted action: estimates when hooks were silent, backoff."""
    if _hook_denied(error):
        return  # the hook blocked the call: nothing was sent, the hook is evidently active
    since = float(pa.get("emitted_ts") or 0) - 1
    entries = ledger.read_entries(ctx.cache, since=since)
    tool = ratelimit.tool_suffix(pa["tool"])
    hook = [e for e in entries if e.get("source") == "hook" and e.get("tool") == tool]
    if not hook:
        ledger.append(
            {
                "ts": ctx.now(),
                "event": "driver_estimate",
                "tool": tool,
                "counted": True,
                "run_id": m["run_id"],
                "action_id": pa["action_id"],
                "source": "driver",
                "ok": error is None,
            },
            ctx.cache,
        )
        if not m.get("hooks_inactive"):
            m["hooks_inactive"] = True
            mf.add_event(m, "hooks", "hooks inactive; counts are estimates")
    text = error if error else result
    limited = (
        ratelimit.looks_rate_limited(text, strict=False)
        if error
        else ratelimit.looks_rate_limited(result, strict=True)
    )
    if limited:
        if not any(e.get("event") == "backoff" and e.get("source") == "hook" for e in entries):
            state = ledger.read_state(ctx.cache)
            n = int(state.get("consecutive_rate_limits") or 0) + 1
            kwargs = {"rng": ctx.rng} if ctx.rng else {}
            seconds = ratelimit.backoff_seconds(
                n, ctx.budget, retry_after=ratelimit.parse_retry_after(text), **kwargs
            )
            ledger.register_rate_limit(
                seconds, now=ctx.now(), run_id=m["run_id"], tool=tool, cache=ctx.cache
            )
    elif error is None:
        ledger.clear_rate_limit_streak(ctx.cache)


def _hook_denied(error: str | None) -> bool:
    """The call never ran: our own PreToolUse gate denied it (see hooks/figma_gate.py)."""
    return bool(error) and HOOK_DENY_TAG in error and "--resume" in error


def _classify(result: str, error: str | None) -> tuple[str | None, str]:
    """(error class or None for success, text). Successful results are checked strictly."""
    if _hook_denied(error):
        return "denied", error or ""
    if error:
        return ratelimit.classify_error(f"{error}\n{result[:2000]}"), error
    if ratelimit.looks_rate_limited(result, strict=True):
        return "rate_limit", result
    return None, result


def _common_failure(
    ctx: Ctx, m: dict[str, Any], cls: str, text: str, what: str
) -> dict[str, Any] | None:
    """Failures handled the same way for every action kind."""
    if cls == "denied":
        return {
            "outcome": "denied",
            "class": cls,
            "explain": "the budget hook blocked the call; dc.py next waits or stops.",
        }
    if cls == "rate_limit":
        return {
            "outcome": "rate_limited",
            "class": cls,
            "explain": f"{what} was rate limited; "
            "dc.py next waits or stops depending on the backoff.",
        }
    if cls == "auth":
        stop = _stop(
            ctx,
            m,
            "auth",
            f"Figma rejected the request for {what} (authentication).",
            "Authenticate the Figma MCP server (run /mcp, select figma, authenticate), then run "
            f"{RESUME}.",
        )
        return {"outcome": "error", "class": cls, "stop": stop}
    if cls == "network":
        stop = _stop(
            ctx,
            m,
            "network",
            f"The Figma MCP server was unreachable or disconnected during {what}.",
            f"Reconnect the Figma MCP server (run /mcp), then run {RESUME}.",
        )
        return {"outcome": "error", "class": cls, "stop": stop}
    return None


def board_key(text: str) -> tuple[str, str] | None:
    """(fileKey, url) from the first figma.com/board/<key>/... URL in text."""
    match = _BOARD_RE.search(text or "")
    if not match:
        return None
    return match.group(1), match.group(0).rstrip(".,;")


def _set_file(m: dict[str, Any], key: str, url: str) -> None:
    fig = m["figma"]
    if not fig["file_key"]:
        fig["file_key"] = key
        fig["file_url"] = f"https://www.figma.com/board/{key}"
        mf.add_event(m, "file", "FigJam file created", file_url=url)
    elif fig["file_key"] != key:
        mf.add_event(m, "warning", f"tool reported a different file ({key}); keeping run file")


def _on_generate(ctx, m, plan, pa, result, error) -> dict[str, Any]:
    did = pa["diagram_id"]
    d = m["diagrams"][did]
    cls, text = _classify(result, error)
    found = board_key(result) if cls is None else None
    if cls is None and found is None:
        guessed = ratelimit.classify_error(result)
        if guessed != "unknown" and len(result) < 2000:
            cls, text = guessed, result
        elif not m["figma"]["file_key"]:
            # Success without a URL and no file yet: only the user can tell us where it went.
            d["attempts"] += 1
            m["uncertain"] = did
            return {"outcome": "uncertain", "diagram_id": did, "status": d["status"]}
    if cls is not None:
        common = _common_failure(ctx, m, cls, text, f"generating '{d['title']}'")
        if common:
            return {**common, "diagram_id": did}
        if cls == "not_found" and m["figma"]["file_key"]:
            stop = _stop(
                ctx,
                m,
                "not_found",
                f"The run's FigJam file ({m['figma']['file_url']}) was not found.",
                "Check that the file still exists and you can edit it. To start in a new file, "
                "delete .diagram-codebase/manifest.json and run /diagram-codebase again.",
            )
            return {"outcome": "error", "class": cls, "stop": stop, "diagram_id": did}
        d["attempts"] += 1
        _err(d, cls, text)
        if d["attempts"] >= ctx.max_attempts:
            d["status"] = "failed"
        hint = ""
        if (
            d["status"] != "failed"
            and d.get("renderer") == "architecture"
            and (ctx.out_dir / "mermaid" / f"{did}.fallback.mmd").is_file()
        ):
            hint = " Next attempt uses the flowchart fallback."
        return {
            "outcome": "error",
            "class": cls,
            "diagram_id": did,
            "status": d["status"],
            "explain": f"generate_diagram failed ({cls}).{hint}",
        }
    d["attempts"] += 1
    d["status"] = "generated"
    if found:
        _set_file(m, *found)
        d["url"] = found[1]
    d["generated_at"] = utc_now()
    return {
        "outcome": "ok",
        "diagram_id": did,
        "status": "generated",
        "file_url": m["figma"]["file_url"],
        "show_user": f"'{d['title']}' is in FigJam: {m['figma']['file_url']}",
    }


def _find_obj(value: Any, key: str, depth: int = 0) -> dict[str, Any] | None:
    """Find a dict containing `key` in a tool result (raw JSON, wrapped text, or prose)."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        if key in value:
            return value
        for v in value.values():
            found = _find_obj(v, key, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        for v in value:
            found = _find_obj(v, key, depth + 1)
            if found is not None:
                return found
        return None
    if not isinstance(value, str) or f'"{key}"' not in value and f'\\"{key}\\"' not in value:
        return None
    try:
        return _find_obj(json.loads(value), key, depth + 1)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(value) if ch in "{["):
        try:
            obj, _ = decoder.raw_decode(value, start)
        except ValueError:
            continue
        found = _find_obj(obj, key, depth + 1)
        if found is not None:
            return found
    return None


def _script_failure(
    ctx, m, cls: str, text: str, what: str, counter: dict[str, Any], key: str
) -> dict[str, Any]:
    common = _common_failure(ctx, m, cls, text, what)
    if common:
        return common
    counter[key] = int(counter.get(key) or 0) + 1
    return {"outcome": "error", "class": cls, "explain": f"{what} failed ({cls}): {text[:200]}"}


def _on_place(ctx, m, plan, pa, result, error) -> dict[str, Any]:
    did = pa["diagram_id"]
    d = m["diagrams"][did]
    cls, text = _classify(result, error)
    obj = _find_obj(result, "sectionId") if cls is None else None
    if cls is None and obj is None:
        cls, text = "unknown", "use_figma result had no sectionId: " + result[:200]
    if cls is not None:
        out = _script_failure(ctx, m, cls, text, f"placing '{d['title']}'", d, "place_attempts")
        if out["outcome"] == "error" and "stop" not in out:
            _err(d, cls, text)
            if d["place_attempts"] >= ctx.max_attempts:
                d["status"] = "failed"
                d["failure"] = "generated but not placed; content is unsectioned in the file"
        return {**out, "diagram_id": did, "status": d["status"]}
    fig = m["figma"]
    if obj.get("noContent") or not obj.get("sectionId"):
        d["status"] = "pending"  # the generated diagram is not on the canvas
        _err(d, "not_found", "no generated content found on the board to place")
        return {"outcome": "error", "class": "not_found", "diagram_id": did, "status": "pending"}
    d.update(status="placed", section_id=obj["sectionId"], bounds=obj.get("bounds"))
    d["placed_at"] = utc_now()
    d.pop("replaced_section_id", None)
    leftovers = [i for i in obj.get("foreignTopLevelIds") or [] if isinstance(i, str)]
    fig["ignore_ids"] = sorted(set(fig["ignore_ids"]) | set(leftovers))
    if obj.get("errors"):
        mf.add_event(m, "warning", f"{did}: placement warnings", details=obj["errors"][:5])
    if not obj.get("texts"):
        mf.add_event(m, "warning", f"{did}: placed section contains no readable text")
    if d.get("next_content_hash"):
        # The plan changed while this content waited to be placed: regenerate it.
        d.update(
            status="stale",
            content_hash=d.pop("next_content_hash"),
            replaced_section_id=d["section_id"],
            attempts=0,
            place_attempts=0,
        )
    return {
        "outcome": "ok",
        "diagram_id": did,
        "status": d["status"],
        "section_id": d["section_id"],
    }


def _on_legend(ctx, m, plan, pa, result, error) -> dict[str, Any]:
    fig = m["figma"]
    cls, text = _classify(result, error)
    obj = _find_obj(result, "sectionId") if cls is None else None
    if cls is None and (obj is None or not obj.get("sectionId")):
        cls, text = "unknown", "legend result had no sectionId: " + result[:200]
    if cls is not None:
        out = _script_failure(ctx, m, cls, text, "adding the legend", fig, "legend_attempts")
        if fig.get("legend_attempts", 0) >= ctx.max_attempts:
            fig["legend_failed_hash"] = pa.get("legend_hash")
            mf.add_event(m, "warning", "legend could not be added", error=text[:200])
        return out
    fig.update(legend_section_id=obj["sectionId"], legend_hash=pa.get("legend_hash"))
    fig["legend_attempts"] = 0
    return {"outcome": "ok", "section_id": obj["sectionId"]}


def _on_delete(ctx, m, plan, pa, result, error) -> dict[str, Any]:
    cls, text = _classify(result, error)
    obj = _find_obj(result, "deletedIds") if cls is None else None
    if cls is None and obj is None:
        cls, text = "unknown", "delete result had no deletedIds: " + result[:200]
    gone: set[str] = set()
    if cls is None:
        gone = set(obj.get("deletedIds") or []) | set(obj.get("missingIds") or [])
    else:
        counter = m["figma"]
        out = _script_failure(ctx, m, cls, text, "removing obsolete sections", counter, "del_n")
        if counter.get("del_n", 0) < ctx.max_attempts:
            return out
        for d in m["diagrams"].values():
            if d["status"] == "obsolete" and d.get("section_id") in pa.get("section_ids", []):
                d["delete_failed"] = True
        mf.add_event(m, "warning", "obsolete sections could not be removed", error=text[:200])
        return out
    for d in m["diagrams"].values():
        if d["status"] == "obsolete" and d.get("section_id") in gone:
            d["section_id"] = None
            d["deleted"] = True
    return {"outcome": "ok", "deleted": sorted(gone)}


# -------------------------------------------------------------- figjam parsing

_TOKEN_RE = re.compile(r"<(/?)([A-Za-z][\w:.-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>|([^<]+)")
_ATTR_RE = re.compile(r"([\w:.-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_CONTAINERS = {"page", "canvas", "document", "figjam", "file", "root", "board", "nodes"}
_TEXT_ATTRS = ("name", "text", "characters", "label", "title", "content", "value")


def parse_figjam(xml: str) -> dict[str, dict[str, Any]]:
    """Tolerant regex parse of get_figjam XML -> {id: {type, name, parent, top, text}}.

    `top` marks top-level canvas nodes (the nearest id-bearing ancestor is absent or a
    page/document container). `text` is all attribute/inner text of the subtree.
    """
    nodes: dict[str, dict[str, Any]] = {}
    stack: list[tuple[str, str | None]] = []  # (tag, id or None)
    for match in _TOKEN_RE.finditer(xml or ""):
        closing, tag, attrs, selfclose, chars = match.groups()
        if chars is not None:
            chunk = html.unescape(chars).strip()
            if chunk:
                for _, nid in stack:
                    if nid:
                        nodes[nid]["text"].append(chunk)
            continue
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    del stack[i:]
                    break
            continue
        attr = {k: html.unescape(a if a is not None else b) for k, a, b in _ATTR_RE.findall(attrs)}
        nid = attr.get("id") or attr.get("nodeId") or attr.get("node-id")
        if nid:
            parent = next((p for _, p in reversed(stack) if p), None)
            parent_container = next((t for t, p in reversed(stack) if p), None)
            ntype = (attr.get("type") or tag).upper()
            is_top = parent is None or (
                (parent_container or "").lower() in _CONTAINERS
                or nodes[parent]["type"] in ("PAGE", "CANVAS", "DOCUMENT")
            )
            texts = [attr[k] for k in _TEXT_ATTRS if attr.get(k)]
            nodes[nid] = {
                "type": ntype,
                "name": attr.get("name", ""),
                "parent": parent,
                "top": is_top and ntype not in ("PAGE", "CANVAS", "DOCUMENT"),
                "text": texts,
            }
            for _, p in stack:
                if p:
                    nodes[p]["text"].extend(texts)
        if not selfclose:
            stack.append((tag, nid))
    return nodes


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\"'`]", "", s)).strip().lower()


_MMD_OPEN = r"(?:\[\[|\[\(|\(\(|\(\[|\[/|\[\\|\[|\(|\{\{|\{|>)"
_MMD_CLOSE = r"(?:\]\]|\)\]|\)\)|\]\)|/\]|\\\]|\]|\)|\}\}|\})"
_MMD_LABEL_RE = re.compile(
    r"\b[A-Za-z]\w*\s*" + _MMD_OPEN + r"\s*(?:\"([^\"\n]+)\"|([^\"\]\)\}\n]+?))\s*" + _MMD_CLOSE
)


def expected_labels(spec: dict[str, Any] | None, mermaid: str = "") -> list[str]:
    """Labels a reader should find in the rendered diagram (from the spec, else Mermaid)."""
    spec = spec or {}
    renderer = spec.get("renderer")
    if renderer == "sequence":
        raw = [p.get("label") or p.get("id") for p in spec.get("participants") or []]
    elif renderer == "erd":
        raw = [e.get("id") for e in spec.get("entities") or []]
    elif renderer == "state":
        raw = [s.get("label") or s.get("id") for s in spec.get("states") or []]
    else:
        raw = [n.get("label") for n in spec.get("nodes") or []]
    labels = [r for r in raw if isinstance(r, str)]
    if not labels and mermaid:
        labels = [q or u for q, u in _MMD_LABEL_RE.findall(mermaid)]
        labels += re.findall(r"^\s*(?:participant|actor)\s+(\w+)", mermaid, re.M)
        labels += re.findall(r"^\s*([A-Za-z][\w-]*)\s*\{", mermaid, re.M)
    out: list[str] = []
    for label in labels:
        n = _norm(label)
        if len(n) >= 2 and n not in out:
            out.append(n)
    return out[:MAX_LABELS]


def label_ratio(labels: list[str], blob: str) -> tuple[float, list[str]]:
    if not labels:
        return 0.0, []
    missing = [lbl for lbl in labels if lbl not in blob]
    return (len(labels) - len(missing)) / len(labels), missing


def _labels_for(ctx: Ctx, plan: dict[str, Any], did: str) -> list[str]:
    path = ctx.out_dir / "mermaid" / f"{did}.mmd"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return expected_labels(_specs(plan).get(did), text)


def _subtree_blob(nodes: dict[str, dict[str, Any]], ids: list[str]) -> str:
    return _norm(" ".join(" ".join(nodes[i]["text"]) for i in ids if i in nodes))


def _known_ids(m: dict[str, Any]) -> set[str]:
    ids = {d["section_id"] for d in m["diagrams"].values() if d.get("section_id")}
    if m["figma"].get("legend_section_id"):
        ids.add(m["figma"]["legend_section_id"])
    return ids | set(m["figma"]["ignore_ids"])


def _on_reconcile(ctx, m, plan, pa, result, error) -> dict[str, Any]:
    did = pa["diagram_id"]
    d = m["diagrams"][did]
    cls, text = _classify(result, error)
    if cls is not None:
        common = _common_failure(ctx, m, cls, text, "reading the board")
        if common:
            return {**common, "diagram_id": did}  # stays uncertain; reconciled after resume
        # Cannot read the board: count the uncertain call as an attempt and retry.
        m["uncertain"] = None
        d["attempts"] += 1
        _err(d, "uncertain", f"could not reconcile: {text[:200]}")
        if d["attempts"] >= ctx.max_attempts:
            d["status"] = "failed"
        return {"outcome": "error", "class": cls, "diagram_id": did, "status": d["status"]}
    m["uncertain"] = None
    nodes = parse_figjam(result)
    known = _known_ids(m)
    foreign = sorted(i for i, n in nodes.items() if n["top"] and i not in known)
    labels = _labels_for(ctx, plan, did)
    ratio, _ = label_ratio(labels, _subtree_blob(nodes, foreign))
    if labels and ratio >= VERIFY_THRESHOLD:
        d["attempts"] += 1
        d["status"] = "generated"
        mf.add_event(m, "reconcile", f"{did}: earlier generate_diagram had landed", ratio=ratio)
        return {"outcome": "ok", "diagram_id": did, "status": "generated", "landed": True}
    m["figma"]["ignore_ids"] = sorted(set(m["figma"]["ignore_ids"]) | set(foreign))
    mf.add_event(m, "reconcile", f"{did}: earlier generate_diagram did not land", ratio=ratio)
    return {"outcome": "ok", "diagram_id": did, "status": d["status"], "landed": False}


def _on_ask_file_url(ctx, m, plan, pa, result, error) -> dict[str, Any]:
    did = pa["diagram_id"]
    d = m["diagrams"][did]
    found = board_key(result)
    m["uncertain"] = None
    if found:
        _set_file(m, *found)
        d["status"] = "generated"
        d["url"] = found[1]
        return {"outcome": "ok", "diagram_id": did, "status": "generated", "landed": True}
    if result.strip().lower().strip(".!") in ("none", "no", "n", ""):
        mf.add_event(m, "reconcile", f"{did}: user reports no FigJam file; retrying")
        return {"outcome": "ok", "diagram_id": did, "status": d["status"], "landed": False}
    m["uncertain"] = did
    return {
        "outcome": "error",
        "class": "invalid_request",
        "explain": "reply was neither a figma.com/board URL nor 'none'; asking again",
    }


def _on_verify(ctx, m, plan, pa, result, error) -> dict[str, Any]:
    v = m.get("verify") or {"sig": _verify_sig(m), "attempts": 0}
    m["verify"] = v
    cls, text = _classify(result, error)
    nodes = parse_figjam(result) if cls is None else {}
    if cls is None and not nodes:
        cls, text = "unknown", "get_figjam result could not be parsed"
    if cls is not None:
        common = _common_failure(ctx, m, cls, text, "verification")
        if common:
            return common
        v["attempts"] = int(v.get("attempts") or 0) + 1
        if v["attempts"] >= ctx.max_attempts:
            v["status"] = "failed"
            for did in _shown(m):
                m["diagrams"][did]["verify"] = {"status": "not run", "reason": text[:200]}
        return {"outcome": "error", "class": cls, "explain": f"verification failed: {text[:200]}"}
    report = {}
    for did in _shown(m):
        d = m["diagrams"][did]
        sid = d["section_id"]
        if sid not in nodes:
            d["verify"] = {"status": "missing", "at": utc_now()}
            report[did] = "missing"
            continue
        ids = [i for i, n in nodes.items() if i == sid or _descends(nodes, i, sid)]
        labels = _labels_for(ctx, plan, did)
        ratio, missing = label_ratio(labels, _subtree_blob(nodes, ids))
        if not labels:
            d["verify"] = {"status": "unknown", "reason": "no expected labels", "at": utc_now()}
        elif ratio >= VERIFY_THRESHOLD:
            d["verify"] = {"status": "ok", "ratio": round(ratio, 2), "at": utc_now()}
            d["status"] = "verified"
        else:
            d["verify"] = {
                "status": "mismatch",
                "ratio": round(ratio, 2),
                "missing": missing[:10],
                "at": utc_now(),
            }
        report[did] = d["verify"]["status"]
    v["status"] = "done"
    v["at"] = utc_now()
    return {"outcome": "ok", "verify": report}


def _descends(nodes: dict[str, dict[str, Any]], nid: str, ancestor: str) -> bool:
    seen = 0
    cur = nodes[nid]["parent"]
    while cur and seen < 64:
        if cur == ancestor:
            return True
        cur = nodes.get(cur, {}).get("parent")
        seen += 1
    return False


def _on_screenshot(ctx, m, plan, pa, result, error) -> dict[str, Any]:
    did = pa["diagram_id"]
    d = m["diagrams"][did]
    v = d.setdefault("verify", {})
    cls, text = _classify(result, error)
    if cls is not None:
        common = _common_failure(ctx, m, cls, text, "the screenshot")
        if common:
            return common
        v["visual"] = {"status": "failed", "error": text[:200], "at": utc_now()}
        return {"outcome": "error", "class": cls, "diagram_id": did}
    ok = result.strip().lower().startswith("ok")
    v["visual"] = {"status": "ok" if ok else "problem", "notes": result.strip()[:1000]}
    if ok and v.get("status") == "mismatch":
        v["status"] = "visual_ok"
        d["status"] = "verified"
    return {"outcome": "ok", "diagram_id": did, "visual": v["visual"]["status"]}


_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "generate": _on_generate,
    "place": _on_place,
    "legend": _on_legend,
    "delete": _on_delete,
    "verify": _on_verify,
    "reconcile_generate": _on_reconcile,
    "screenshot": _on_screenshot,
    "ask_file_url": _on_ask_file_url,
}
