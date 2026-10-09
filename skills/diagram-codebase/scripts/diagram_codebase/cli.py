"""Command line interface: `python3 dc.py <command>` (see docs/DESIGN.md "CLI").

Every command prints JSON (Markdown for `review` and `summary`) on stdout. A
`DCError` becomes one line on stderr and exit status 1, never a traceback.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .args import HELP, parse_args
from .common import DCError, read_json, sha256_text, utc_now, write_json, write_text
from .config import load_config
from .figma import driver, ledger, scripts
from .mermaid import build, lint, parsecheck
from .model import schema
from .model.merge import Merger, merge_all
from .plan.planner import _Planner, plan_diagrams
from .sanitize import Redactor
from .scan import run_scan
from .scan.gitinfo import git_info
from .state import manifest as mf
from .state import update

OUT_NAME = ".diagram-codebase"
DC = "python3 dc.py"
NEXT_STEP = {
    "scan": f"{DC} scan",
    "analysis": "Phase 3 analysis: write findings/<area>.json, check each with "
    f"`{DC} check-findings <file>`, then `{DC} merge`",
    "merge": f"{DC} merge",
    "plan": f"{DC} plan",
    "publish": f"{DC} next (publish loop)",
    "summary": f"{DC} summary",
}


# ===================================================================== helpers


def _print(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _git_toplevel(cwd: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = proc.stdout.strip()
    return Path(top) if proc.returncode == 0 and top else None


def _repo(args: argparse.Namespace) -> Path:
    raw = getattr(args, "repo", None)
    if raw:
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            raise DCError(f"--repo {raw}: not a directory")
        return root
    cwd = Path.cwd()
    return (_git_toplevel(cwd) or cwd).resolve()


def _out(args: argparse.Namespace, repo: Path, fallback: str | None = None) -> Path:
    raw = getattr(args, "out", None) or fallback
    if not raw:
        return repo / OUT_NAME
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else Path.cwd() / path).resolve()


def _ensure_out(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    gi = out / ".gitignore"
    if not gi.is_file():
        write_text(gi, "*\n")


def _need(path: Path, hint: str) -> Path:
    if not path.is_file():
        raise DCError(f"{path.name} not found in {path.parent}; {hint}")
    return path


def _set_phase(out: Path, *phases: tuple[str, str]) -> None:
    m = mf.load(out)
    if m is None:
        return
    for phase, status in phases:
        mf.set_phase(m, phase, status)
    mf.save(out, m)


def _rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


# ======================================================================= args


def cmd_args(raw_argv: list[str]) -> int:
    """`dc.py args "<raw>"`, or the raw string on stdin (quoted heredoc in SKILL.md)."""
    if len(raw_argv) == 1:
        raw = raw_argv[0]
    elif raw_argv:
        raw = shlex.join(raw_argv)
    else:
        raw = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    result = parse_args(raw.strip())
    if result["help"] or result["errors"]:
        result["usage"] = HELP
    _print(result)
    return 0


# ======================================================================= init


def _load_options(raw: str | None) -> dict[str, Any]:
    if raw == "-":
        raw = sys.stdin.read()
    base = parse_args("")
    if not raw or not raw.strip():
        return base
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"--args-json is not valid JSON ({exc.msg}); pass the output of `dc.py args`"
        raise DCError(msg) from None
    if not isinstance(data, dict):
        raise DCError("--args-json must be a JSON object (the output of `dc.py args`)")
    if data.get("errors"):
        raise DCError("argument errors: " + "; ".join(map(str, data["errors"])))
    base.update({k: v for k, v in data.items() if k in base})
    return base


def _resume_phase(m: dict[str, Any]) -> str:
    for phase in mf.PHASES:
        if m["phases"][phase]["status"] not in ("done", "skipped"):
            return phase
    return "summary"


def do_init(repo: Path, out: Path, opts: dict[str, Any]) -> dict[str, Any]:
    _ensure_out(out)
    if not (out / "config.json").is_file():
        write_json(out / "config.json", {})
    stored = {
        k: opts.get(k)
        for k in ("depth", "focus", "types", "dry_run", "resume", "update", "yes", "verify_visual")
    }
    stored["hint"] = opts.get("hint") or ""
    git = git_info(repo)
    warnings: list[str] = []
    if opts.get("resume") and mf.load(out) is None:
        warnings.append("--resume: no earlier run found here; starting a fresh run")
    if opts.get("update") and mf.load(out) is None:
        warnings.append("--update: no earlier run found here; starting a fresh run")
    m, mode = mf.init_run(out, repo_root=repo, revision=git.get("revision"), options=stored)
    m["out_dir"] = str(out)
    phase = _resume_phase(m) if mode == "resume" else "scan"
    if mode == "update":
        next_step = f"{DC} update-diff"
    else:
        next_step = NEXT_STEP[phase]
    mf.add_event(m, "init", f"run initialized ({mode})")
    mf.save(out, m)
    hook = ledger.install_hook()
    return {
        "out_dir": str(out),
        "mode": mode,
        "run_id": m["run_id"],
        "resume_from_phase": phase if mode == "resume" else None,
        "next_step": next_step,
        "revision": git.get("short"),
        "dirty": git.get("dirty"),
        "options": stored,
        "hook_copy_installed": hook is not None,
        "warnings": warnings,
    }


def cmd_init(args: argparse.Namespace) -> int:
    opts = _load_options(args.args_json)
    repo = _repo(args)
    out = _out(args, repo, opts.get("out") and str((repo / opts["out"]).resolve()))
    _print(do_init(repo, out, opts))
    return 0


# ======================================================================= scan


def do_scan(repo: Path, out: Path) -> dict[str, Any]:
    _ensure_out(out)
    result = run_scan(repo, out, load_config(out))
    _set_phase(out, ("scan", "done"))
    return {
        "brief": result["brief"],
        "inventory": "inventory.json",
        "findings": "findings/scan.json",
        "next_step": NEXT_STEP["analysis"],
    }


def cmd_scan(args: argparse.Namespace) -> int:
    repo = _repo(args)
    _print(do_scan(repo, _out(args, repo)))
    return 0


# ============================================================ findings template

_EV = {"file": "path/to/file.py", "lines": "12-40", "symbol": "function_name",
       "status": "confirmed"}  # fmt: skip
TEMPLATE: dict[str, Any] = {
    "agent": "<area slug: execution | dataflow | algorithms | infrastructure | sub-<slug>>",
    "area": "<one line: what this findings file covers>",
    "subsystems": [{"id": "sub:<slug>", "name": "Readable name", "description": "...",
                    "paths": ["dir"]}],
    "nodes": [{"id": "algo:<slug>", "name": "Readable name", "kind": "algorithm",
               "subsystem": "sub:<slug>", "description": "...", "evidence": [_EV]}],
    "edges": [{"from": "<node id>", "to": "<node id>", "kind": "calls", "label": "short verb phrase",
               "phase": "runtime", "evidence": [_EV]}],
    "flows": [{"id": "flow-<slug>", "name": "...", "kind": "execution", "trigger": "...",
               "steps": [{"from": "<node id>", "to": "<node id>", "label": "...", "kind": "call",
                          "condition": "", "phase": "runtime"}], "evidence": [_EV]}],
    "dataflows": [{"id": "df-<slug>", "name": "...",
                   "stages": [{"node": "<node id>", "role": "input", "representation": "..."}],
                   "links": [{"from": "<node id>", "to": "<node id>", "label": "..."}],
                   "evidence": [_EV]}],
    "algorithms": [{"id": "algo-<slug>", "name": "...", "node": "algo:<slug>", "purpose": "...",
                    "inputs": [], "outputs": [],
                    "stages": [{"id": "s1", "name": "...", "kind": "step",
                                "functions": ["fn:<path>:<Qual>"], "evidence": [_EV]}],
                    "transitions": [{"from": "s1", "to": "s2", "label": "..."}],
                    "evidence": [_EV]}],
    "state_machines": [{"id": "sm-<slug>", "name": "...", "states": [{"id": "Idle", "name": "Idle"}],
                        "initial": "Idle", "transitions": [{"from": "Idle", "to": "Run",
                                                            "label": "start"}], "evidence": [_EV]}],
    "erd": {"entities": [{"id": "users", "name": "users",
                          "attributes": [{"name": "id", "type": "int", "key": "PK"}],
                          "evidence": [_EV]}],
            "relations": [{"from": "users", "to": "orders", "cardinality": "one-to-many",
                           "label": "places"}]},
    "uncertainties": ["What could not be traced, and why."],
}  # fmt: skip
ID_CONVENTIONS = {
    "mod:<path>": "source file/module, e.g. mod:app/service.py",
    "cls:<path>:<Qual>": "class",
    "fn:<path>:<Qual>": "function or method (Class.method)",
    "api:<METHOD> <norm path>": "HTTP endpoint, path params as {}",
    "ros:<name>": "ROS 2 node",
    "topic:/<name> srv:/<name> action:/<name>": "ROS 2 interfaces",
    "tf:<frame>": "TF frame",
    "chan:<name>": "queue/topic/event channel (also chan:<Class>.<attr>, chan:SIGTERM)",
    "state:<name>": "client-side state (Redux slice, React context)",
    "store:<slug>": "datastore",
    "ent:<table>": "DB entity",
    "ext:<host or vendor>": "external service",
    "cfg:env:<VAR> cfg:file:<name> param:<node>/<name>": "configuration",
    "deploy:<name> launch:<path>": "deployment units, launch files",
    "sub:<slug>": "subsystem",
    "<concept>:<slug>": "Claude-defined concept, e.g. algo:icp, data:point-cloud",
}


def cmd_findings_template(args: argparse.Namespace) -> int:
    _print(
        {
            "template": TEMPLATE,
            "id_conventions": ID_CONVENTIONS,
            "vocabularies": {
                "node_kinds": list(schema.NODE_KINDS),
                "edge_kinds": list(schema.EDGE_KINDS),
                "evidence_status": list(schema.EVIDENCE_STATUSES),
                "phases": list(schema.PHASES),
                "flow_step_kinds": list(schema.FLOW_STEP_KINDS),
                "dataflow_roles": list(schema.DATA_ROLES),
                "algorithm_stage_kinds": list(schema.STAGE_KINDS),
            },
            "rules": [
                "Every new node, edge, flow, dataflow, algorithm, state machine and ERD entity "
                "needs inline evidence: file (repo-relative), lines 'N' or 'N-M', symbol, status.",
                "The cited symbol must appear within 3 lines of the cited range.",
                "Imports are never calls. Use static_inferred unless the code states it directly.",
                "An existing node id (from the scan) is an update; evidence is optional.",
                "Omit collections the area does not produce. Validate with check-findings.",
            ],
        }
    )
    return 0


# ============================================================== check-findings


def cmd_check_findings(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out = _out(args, repo)
    path = Path(args.file).expanduser()
    if not path.is_file() and (out / "findings" / path.name).is_file():
        path = out / "findings" / path.name
    if not path.is_file():
        raise DCError(f"findings file {args.file} not found")
    if path.name == "scan.json":
        raise DCError("findings/scan.json belongs to the scanner; check your own area files")
    inventory = read_json(_need(out / "inventory.json", f"run `{DC} scan` first"))
    data = read_json(path)
    if not isinstance(data, dict):
        raise DCError(f"{path.name}: findings must be a JSON object")
    m = Merger(repo, inventory)
    scan = out / "findings" / "scan.json"
    if scan.is_file():
        m.add_scan(read_json(scan))
    scan_warnings = len(m.warnings)
    m.add_findings(path.stem, data)
    counts = {
        k: len(data.get(k) or [])
        for k in ("subsystems", "nodes", "edges", "flows", "dataflows", "algorithms",
                  "state_machines")
    }  # fmt: skip
    counts["erd_entities"] = len((data.get("erd") or {}).get("entities") or [])
    _print(
        {
            "file": path.name,
            "ok": not m.rejected,
            "items": counts,
            "rejected_count": len(m.rejected),
            "rejected": m.rejected,
            "warnings": m.warnings[scan_warnings:],
        }
    )
    return 0


# ======================================================================= merge


def do_merge(repo: Path, out: Path) -> dict[str, Any]:
    _need(out / "inventory.json", f"run `{DC} scan` first")
    model, report = merge_all(repo, out)
    validation = {
        "errors": report.get("errors", []),
        "warnings": report.get("warnings", []),
        "rejected": report.get("rejected", []),
        "stats": report.get("stats", {}),
    }
    write_json(out / "model.json", model)
    write_json(out / "validation.json", validation)
    sources = model["meta"]["coverage"]["findings_sources"]
    analysis = "done" if any(s != "scan" for s in sources) else "skipped"
    _set_phase(
        out, ("analysis", analysis), ("merge", "done" if not validation["errors"] else "failed")
    )
    kinds: dict[str, int] = {}
    for n in model["nodes"]:
        kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
    return {
        "ok": not validation["errors"],
        "nodes": len(model["nodes"]),
        "edges": len(model["edges"]),
        "subsystems": len(model["subsystems"]),
        "flows": len(model["flows"]),
        "dataflows": len(model["dataflows"]),
        "algorithms": len(model["algorithms"]),
        "state_machines": len(model["state_machines"]),
        "erd_entities": len(model["erd"]["entities"]),
        "node_kinds": dict(sorted(kinds.items())),
        "findings_sources": sources,
        "rejected_count": len(validation["rejected"]),
        "rejected": validation["rejected"][:20],
        "warnings_count": len(validation["warnings"]),
        "warnings": validation["warnings"][:20],
        "errors": validation["errors"][:20],
        "uncertainties": len(model["meta"].get("uncertainties") or []),
        "files": ["model.json", "validation.json"],
        "next_step": NEXT_STEP["plan"],
    }


def cmd_merge(args: argparse.Namespace) -> int:
    repo = _repo(args)
    _print(do_merge(repo, _out(args, repo)))
    return 0


# ======================================================================== plan


def _counts(spec: dict[str, Any]) -> tuple[int, int]:
    r = spec.get("renderer")
    if r == "sequence":
        return len(spec.get("participants") or []), len(spec.get("messages") or [])
    if r == "erd":
        return len(spec.get("entities") or []), len(spec.get("relations") or [])
    if r == "state":
        return len(spec.get("states") or []), len(spec.get("transitions") or [])
    return len(spec.get("nodes") or []), len(spec.get("edges") or [])


def _issue_text(issues: list[dict[str, Any]], limit: int = 3) -> str:
    return "; ".join(f"line {i['line']}: {i['message']}" for i in issues[:limit])


def _render_checked(
    spec: dict[str, Any], redactor: Redactor, config: dict[str, Any]
) -> tuple[str | None, str | None, list[dict[str, Any]], str | None]:
    """(text, fallback text, lint issues, drop reason). Lint errors are a hard gate."""
    try:
        text = build.render(spec, redactor)
    except ValueError as exc:
        return None, None, [], f"Mermaid could not be built: {exc}"
    issues = lint.lint(text, spec["renderer"], config)
    fallback = None
    if spec["renderer"] == "architecture":
        fb_spec = build.architecture_fallback(spec)
        fb_text = build.render(fb_spec, Redactor())
        fb_issues = lint.lint(fb_text, "flowchart", config)
        if lint.errors(issues) and not lint.errors(fb_issues):
            spec.update(fb_spec)
            spec.pop("lanes", None)
            spec["notes"] = list(spec.get("notes") or []) + [
                "architecture layout failed the Figma lint ("
                + _issue_text(lint.errors(issues), 1)
                + "); drawn as a flowchart"
            ]
            return fb_text, None, fb_issues, None
        if not lint.errors(fb_issues):
            fallback = fb_text
    if lint.errors(issues):
        return None, None, issues, "Mermaid lint (Figma rules): " + _issue_text(lint.errors(issues))
    return text, fallback, issues, None


def estimate_calls(
    plan: dict[str, Any], m: dict[str, Any] | None, verify_visual: bool = False
) -> dict[str, Any]:
    """Minimum counted Figma calls to publish `plan` (local estimate, excluding retries)."""
    known = (m or {}).get("diagrams") or {}
    ids = {d["id"] for d in plan["diagrams"]}
    todo = [
        d["id"]
        for d in plan["diagrams"]
        if not (
            d["id"] in known
            and known[d["id"]].get("content_hash") == d.get("content_hash")
            and known[d["id"]].get("status") in ("placed", "verified")
        )
    ]
    obsolete = [did for did, d in known.items() if did not in ids and d.get("section_id")]
    changed = bool(todo or obsolete)
    calls = {
        "generate_diagram": len(todo),
        "use_figma_place": len(todo),
        "use_figma_legend": 1 if changed else 0,
        "use_figma_delete": 1 if obsolete else 0,
        "get_figjam_verify": 1 if changed else 0,
        "get_screenshot": len(todo) if verify_visual else 0,
    }
    return {
        "diagrams_to_publish": todo,
        "unchanged": sorted(ids - set(todo)),
        "obsolete": sorted(obsolete),
        "calls": calls,
        "total": sum(calls.values()),
        "note": "Local estimate of this skill's counted Figma MCP calls, excluding retries "
        "(each failed attempt adds up to 2). whoami is exempt. Not Figma's quota.",
    }


def do_plan(repo: Path, out: Path) -> dict[str, Any]:
    config = load_config(out)
    model = read_json(_need(out / "model.json", f"run `{DC} merge` first"))
    validation = read_json(out / "validation.json", default={})
    if validation.get("errors"):
        raise DCError(
            f"model.json has {len(validation['errors'])} validation error(s) (see "
            "validation.json); fix the findings and merge again"
        )
    m = mf.load(out)
    options = (m or {}).get("options") or {}
    opts = {
        "depth": options.get("depth") or "standard",
        "types": options.get("types") or [],
        "focus": options.get("focus"),
        "generated_at": utc_now(),
    }
    plan = plan_diagrams(model, opts, config)
    redactor = Redactor()
    kept: list[dict[str, Any]] = []
    texts: dict[str, tuple[str, str | None]] = {}
    lint_report: dict[str, Any] = {}
    dropped = 0
    for spec in plan["diagrams"]:
        text, fallback, issues, reason = _render_checked(spec, redactor, config)
        if reason or text is None:
            plan["skipped"].append({"type": spec["type"], "id": spec["id"], "reason": reason})
            dropped += 1
            continue
        long = build.long_labels(spec)
        if long:
            spec["notes"] = list(spec.get("notes") or []) + [
                f"{len(long)} label(s) longer than {build.MAX_LABEL} characters end in '...': "
                + "; ".join(lbl[:50] + "..." for lbl in long[:5])
            ]
        texts[spec["id"]] = (text, fallback)
        lint_report[spec["id"]] = {
            "errors": 0,
            "warnings": [f"line {i['line']}: {i['rule']}: {i['message']}" for i in issues],
        }
        kept.append(spec)

    items = [{"id": did, "text": t} for did, (t, _) in texts.items()]
    items += [{"id": f"{did}.fallback", "text": f} for did, (_, f) in texts.items() if f]
    parsed = parsecheck.check(items)
    parser = {"status": parsed["status"], "detail": parsed["detail"], "checked": 0, "failed": {}}
    if parsed["status"] == "ok":
        parser["checked"] = len(items)
        for key, err in sorted(parsed["results"].items()):
            if not err:
                continue
            parser["failed"][key] = str(err)[:300]
            did = key.removesuffix(".fallback")
            if key.endswith(".fallback"):
                texts[did] = (texts[did][0], None)
                continue
            spec = next(s for s in kept if s["id"] == did)
            kept.remove(spec)
            texts.pop(did)
            lint_report.pop(did, None)
            dropped += 1
            plan["skipped"].append(
                {"type": spec["type"], "id": did, "reason": f"Mermaid parser: {str(err)[:200]}"}
            )

    mdir = out / "mermaid"
    mdir.mkdir(parents=True, exist_ok=True)
    wanted: set[str] = set()
    for i, spec in enumerate(kept):
        text, fallback = texts[spec["id"]]
        spec["priority"] = i
        spec["content_hash"] = sha256_text(f"{spec['title']}\n{text}")
        write_text(mdir / f"{spec['id']}.mmd", text)
        wanted.add(f"{spec['id']}.mmd")
        if fallback:
            write_text(mdir / f"{spec['id']}.fallback.mmd", fallback)
            wanted.add(f"{spec['id']}.fallback.mmd")
    for old in mdir.glob("*.mmd"):
        if old.name not in wanted:
            old.unlink()
    plan["diagrams"] = kept
    if dropped:
        plan["coverage"] = _Planner(model, opts, config)._coverage(kept)
    plan["checks"] = {"lint": lint_report, "parser": parser}
    plan["redaction"] = redactor.report()
    write_json(out / "plan.json", plan)
    estimate = estimate_calls(plan, m, bool(options.get("verify_visual")))
    write_text(
        out / "publish" / "review.md", review_markdown(plan, model, m, estimate, config, texts)
    )
    _set_phase(out, ("plan", "done"))
    return {
        "diagrams": [
            {
                "id": d["id"],
                "title": d["title"],
                "type": d["type"],
                "renderer": d["renderer"],
                "nodes": _counts(d)[0],
                "edges": _counts(d)[1],
            }
            for d in kept
        ],
        "skipped": len(plan["skipped"]),
        "dropped_by_checks": dropped,
        "lint_warnings": sum(len(v["warnings"]) for v in lint_report.values()),
        "parser": {k: parser[k] for k in ("status", "detail", "checked")},
        "redaction": plan["redaction"],
        "estimated_figma_calls": estimate["total"],
        "files": ["plan.json", "mermaid/", "publish/review.md"],
        "next_step": f"{DC} review",
    }


def cmd_plan(args: argparse.Namespace) -> int:
    repo = _repo(args)
    _print(do_plan(repo, _out(args, repo)))
    return 0


def _legend_lines(plan: dict[str, Any], model: dict[str, Any]) -> list[str]:
    cats = sorted(
        {
            item["category"]
            for d in plan["diagrams"]
            for item in (d.get("nodes") or []) + (d.get("groups") or [])
            if item.get("category") in scripts.CATEGORY_STICKY
        }
    )
    repo = ((model.get("meta") or {}).get("repo") or {}).get("name") or "repository"
    rev = ((model.get("meta") or {}).get("revision") or "unknown revision")[:10]
    lines = [
        scripts.LEGEND_NAME,
        f"{repo} @ {rev}, generated <date>",
        "Evidence: see local .diagram-codebase/model.json",
    ]
    lines += [scripts.CATEGORY_STICKY[c][1] for c in cats]
    lines.append("Diagrams")
    lines += [f"{i}. {d['title']} ({d['type']})" for i, d in enumerate(plan["diagrams"], 1)]
    return lines


def review_markdown(
    plan: dict[str, Any],
    model: dict[str, Any],
    m: dict[str, Any] | None,
    estimate: dict[str, Any],
    config: dict[str, Any],
    texts: dict[str, tuple[str, str | None]],
) -> str:
    meta = model.get("meta") or {}
    repo = (meta.get("repo") or {}).get("name") or "repository"
    rev = (meta.get("revision") or "")[:10] or "no git revision"
    state = " (uncommitted changes)" if meta.get("dirty") else ""
    opts = plan.get("options") or {}
    L: list[str] = [
        f"# Publish review: {repo}",
        "",
        f"Revision {rev}{state}. Depth {opts.get('depth')}"
        + (f", focus '{opts['focus']}'" if opts.get("focus") else "")
        + (f", types {', '.join(opts['types'])}" if opts.get("types") else "")
        + ".",
        "",
        "Nothing has been sent to Figma yet. Approving publishes the diagrams below to one "
        "FigJam file (created by the first diagram). Everything that leaves this machine is "
        "listed in the last section, verbatim.",
        "",
        f"## Diagrams ({len(plan['diagrams'])})",
        "",
        "| # | Diagram | Type | Renderer | Nodes | Edges | Question it answers |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, d in enumerate(plan["diagrams"], 1):
        n, e = _counts(d)
        purpose = (d.get("purpose") or "").replace("|", "/")
        L.append(f"| {i} | {d['title']} | {d['type']} | {d['renderer']} | {n} | {e} | {purpose} |")
    noted = [d for d in plan["diagrams"] if d.get("notes")]
    if noted:
        L += ["", "### Notes (what each diagram leaves out)", ""]
        for d in noted:
            L.append(f"- **{d['title']}**: " + " ".join(f"{x.rstrip('.')}." for x in d["notes"]))
    if plan.get("skipped"):
        L += ["", f"## Not drawn ({len(plan['skipped'])})", ""]
        for s in plan["skipped"]:
            L.append(f"- {s['type']}{' `' + s['id'] + '`' if s.get('id') else ''}: {s['reason']}")
    cov = plan.get("coverage") or {}
    if cov:
        any_d = cov.get("any_diagram") or {}
        L += [
            "",
            "## Coverage",
            "",
            f"- {any_d.get('count', 0)} of {any_d.get('of', 0)} model nodes appear in at least "
            "one diagram (directly or folded into a module/subsystem box).",
            f"- {(cov.get('only_in_model') or {}).get('count', 0)} model nodes are only in "
            "model.json.",
        ]
    calls = estimate["calls"]
    budget = config["budget"]
    L += [
        "",
        "## Estimated Figma calls",
        "",
        f"About **{estimate['total']}** counted calls: {calls['generate_diagram']} "
        f"generate_diagram, {calls['use_figma_place']} use_figma placements, "
        f"{calls['use_figma_legend']} legend, {calls['use_figma_delete']} obsolete-section "
        f"removal, {calls['get_figjam_verify']} get_figjam verification, "
        f"{calls['get_screenshot']} screenshots. Plus one `whoami` (exempt) to find your "
        "Figma plan.",
        "",
        f"{estimate['note']} The skill paces itself at {budget['per_minute']} calls/minute and "
        f"{budget['per_day']}/day (its own limits, below Figma's documented ones).",
    ]
    if estimate["unchanged"]:
        L.append(f"Unchanged and already placed (not resent): {', '.join(estimate['unchanged'])}.")
    if estimate["obsolete"]:
        L.append(f"Sections to remove: {', '.join(estimate['obsolete'])}.")
    checks = plan.get("checks") or {}
    parser = checks.get("parser") or {}
    warn = sum(len(v["warnings"]) for v in (checks.get("lint") or {}).values())
    pstatus = {
        "ok": f"ran on {parser.get('checked', 0)} Mermaid texts; "
        f"{len(parser.get('failed') or {})} rejected",
        "unavailable": f"not run ({parser.get('detail')})",
        "error": f"could not run ({parser.get('detail')})",
    }.get(parser.get("status"), "not run")
    red = plan.get("redaction") or {}
    by_cat = ", ".join(f"{k} {v}" for k, v in (red.get("by_category") or {}).items())
    L += [
        "",
        "## Checks",
        "",
        f"- Figma Mermaid rules (lint): every listed diagram passed; {warn} warning(s).",
        f"- Mermaid parser: {pstatus}.",
        f"- Redaction: {red.get('total', 0)} value(s) redacted in {red.get('labels', 0)} "
        f"label(s){' (' + by_cat + ')' if by_cat else ''}. Redacted values are never shown.",
        "",
        "## Exactly what leaves this machine",
        "",
        "Per diagram: `name` (title), `userIntent` (purpose) and `mermaidSyntax` (below) go to "
        "`generate_diagram`; the title and plugin data (diagram id, run id, content hash, row) "
        "go to a `use_figma` section script. Section names start with "
        f"`{scripts.SECTION_PREFIX.strip()}`.",
    ]
    for d in plan["diagrams"]:
        text, fallback = texts[d["id"]]
        L += ["", f"### {d['title']} (`mermaid/{d['id']}.mmd`)", "", f"userIntent: {d['purpose']}"]
        L += ["", "```mermaid", text.rstrip("\n"), "```"]
        if fallback:
            L += [
                "",
                f"If Figma rejects the architecture layout, this flowchart version "
                f"(`mermaid/{d['id']}.fallback.mmd`) is sent instead:",
                "",
                "```mermaid",
                fallback.rstrip("\n"),
                "```",
            ]
    L += ["", "### Legend and index section", "", "```text", *_legend_lines(plan, model), "```", ""]
    return "\n".join(L)


# ====================================================================== review


def cmd_review(args: argparse.Namespace) -> int:
    repo = _repo(args)
    path = _need(_out(args, repo) / "publish" / "review.md", f"run `{DC} plan` first")
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return 0


# ================================================================ driver loop


def cmd_confirm(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out = _out(args, repo)
    _need(out / "plan.json", f"run `{DC} plan` first")
    _print(driver.confirm(out))
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out = _out(args, repo)
    _print(driver.next_action(out, load_config(out)))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out = _out(args, repo)
    result: str | None = None
    if args.result_file == "-":
        result = sys.stdin.read()
    elif args.result_file:
        path = Path(args.result_file).expanduser()
        if not path.is_file() and (out / args.result_file).is_file():
            path = out / args.result_file
        if not path.is_file():
            raise DCError(f"--result-file {args.result_file} not found")
        result = path.read_text(encoding="utf-8", errors="replace")
    if result is None and args.error is None:
        raise DCError("record needs --result-file <file|-> or --error <text>")
    _print(driver.record(out, args.action, result, args.error, load_config(out)))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = _repo(args)
    _print(driver.status(_out(args, repo)))
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out = _out(args, repo)
    report = driver.budget_report(load_config(out if out.is_dir() else None))
    report["note"] = "Local estimates of this skill's own Figma calls; not Figma's quota."
    _print(report)
    return 0


def cmd_update_diff(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out = _out(args, repo)
    _print(update.affected(out, repo, load_config(out)))
    return 0


# ===================================================================== summary


def summary_markdown(out: Path) -> str:
    model = read_json(_need(out / "model.json", f"run `{DC} merge` first"))
    plan = read_json(out / "plan.json", default={"diagrams": [], "skipped": []})
    validation = read_json(out / "validation.json", default={})
    m = mf.load(out)
    meta = model.get("meta") or {}
    cov = meta.get("coverage") or {}
    repo = (meta.get("repo") or {}).get("name") or "repository"
    rev = (meta.get("revision") or "")[:10]
    dirty = meta.get("dirty")
    published = bool(m and m["figma"].get("file_url"))
    L = [f"# Architecture diagrams: {repo}", ""]
    rev_text = f"{rev} ({'uncommitted changes' if dirty else 'clean'})" if rev else "not a git repo"
    L += [f"- Model revision: {rev_text}", f"- Report written: {utc_now()}"]
    if m:
        L.append(f"- Run: {m['run_id']} (publish phase: {m['phases']['publish']['status']})")
    L += ["", "## FigJam", ""]
    if published:
        L.append(f"- File: {m['figma']['file_url']}")
    else:
        L.append("- Nothing was published to Figma in this run (dry run or not confirmed).")
    L += ["", "## Diagrams", "", "| Diagram | Type | Status | Verification |", "|---|---|---|---|"]
    known = (m or {}).get("diagrams") or {}
    for d in plan.get("diagrams") or []:
        entry = known.get(d["id"]) or {}
        status = (
            entry.get("status")
            if published or entry.get("status") not in (None, "pending")
            else "planned (not published)"
        )
        verify = (entry.get("verify") or {}).get("status") or "not run"
        L.append(
            f"| {d['title']} | {d['type']} | {status or 'planned (not published)'} | {verify} |"
        )
    for _did, entry in sorted(known.items()):
        if entry["status"] == "obsolete":
            L.append(f"| {entry['title']} | {entry.get('type')} | obsolete | - |")
    for s in plan.get("skipped") or []:
        L.append(f"| {s.get('id') or s['type']} | {s['type']} | skipped: {s['reason']} | - |")
    any_d = (plan.get("coverage") or {}).get("any_diagram") or {}
    entries = meta.get("entry_points") or []
    drawn = {n for d in plan.get("diagrams") or [] for n in d.get("model_nodes") or []}
    L += [
        "",
        "## Coverage",
        "",
        f"- Analyzed {cov.get('deterministically_analyzed_files', 0)} of "
        f"{cov.get('source_files', 0)} source files deterministically "
        f"({cov.get('parse_errors', 0)} parse errors).",
        f"- {any_d.get('count', 0)} of {any_d.get('of', len(model.get('nodes') or []))} model "
        "nodes appear in at least one diagram.",
        f"- {sum(1 for e in entries if e in drawn)} of {len(entries)} entry points appear in a "
        "diagram.",
        f"- Findings sources: {', '.join(cov.get('findings_sources') or []) or 'none'}.",
    ]
    unsupported = cov.get("unsupported_language_files") or {}
    if unsupported:
        L.append(
            "- Not analyzed (unsupported languages): "
            + ", ".join(f"{k} {v}" for k, v in sorted(unsupported.items()))
            + "."
        )
    unc = list(meta.get("uncertainties") or [])
    warns = list(validation.get("warnings") or [])
    L += ["", "## Uncertainties and warnings", ""]
    L += [f"- {u}" for u in unc[:30]] or ["- No uncertainties recorded by the analysis."]
    if warns:
        L.append(f"- {len(warns)} merge warning(s); first ones:")
        L += [f"  - {w}" for w in warns[:10]]
    L.append(
        f"- Rejected findings items (no acceptable evidence): {len(validation.get('rejected') or [])}"
        " (details in validation.json)."
    )
    budget = driver.budget_report(load_config(out))
    L += [
        "",
        "## Figma usage (local estimates, not Figma's quota)",
        "",
        f"- Counted calls in the last minute: {(budget.get('minute') or {}).get('used', 'unknown')}; "
        f"last 24 h: {(budget.get('day') or {}).get('used', 'unknown')} (estimated).",
    ]
    if m and m.get("hooks_inactive"):
        L.append("- Hooks were inactive; counts are driver estimates.")
    L += [
        "",
        "## Artifacts (in the output directory)",
        "",
        "- `REPORT.md`, `model.json`, `validation.json`, `plan.json`, `mermaid/*.mmd`, "
        "`publish/review.md`, `manifest.json`",
        "",
        "## Next steps",
        "",
        "- After code changes: `/diagram-codebase --update` (re-analyzes what changed, "
        "regenerates only affected diagrams).",
    ]
    pending = mf.remaining_work(m) if m else []
    if pending and published:
        L.append(f"- {len(pending)} diagram(s) still pending: `/diagram-codebase --resume`.")
    elif not published:
        L.append("- To publish: `/diagram-codebase --resume` (or run without `--dry-run`).")
    return "\n".join(L) + "\n"


def cmd_summary(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out = _out(args, repo)
    text = summary_markdown(out)
    write_text(out / "REPORT.md", text)
    sys.stdout.write(text)
    return 0


# ========================================================================= run


def cmd_run(args: argparse.Namespace) -> int:
    if not args.dry_run:
        raise DCError("`run` only supports --dry-run; publishing needs Claude (/diagram-codebase)")
    opts = _load_options(args.args_json)
    opts.update(dry_run=True, resume=False, update=False)
    repo = _repo(args)
    out = _out(args, repo, opts.get("out") and str((repo / opts["out"]).resolve()))
    init = do_init(repo, out, opts)
    scan = do_scan(repo, out)
    merge = do_merge(repo, out)
    plan = do_plan(repo, out)
    _print(
        {
            "out_dir": init["out_dir"],
            "mode": init["mode"],
            "scan": {
                k: scan["brief"][k]
                for k in ("languages", "frameworks", "source_files", "analyzed_files")
            },
            "merge": {k: merge[k] for k in ("nodes", "edges", "subsystems", "rejected_count")},
            "plan": {
                k: plan[k] for k in ("diagrams", "skipped", "parser", "estimated_figma_calls")
            },
            "note": "Dry run: no Figma calls were made.",
        }
    )
    return 0


# ======================================================================= parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo", default=argparse.SUPPRESS, help="repository root (default: git toplevel of cwd)"
    )
    common.add_argument(
        "--out", default=argparse.SUPPRESS, help="output dir (default: <repo>/.diagram-codebase)"
    )
    p = argparse.ArgumentParser(
        prog="dc.py",
        description="diagram-codebase: repository -> evidence-backed model -> Mermaid -> FigJam.",
        parents=[common],
    )
    p.add_argument("--version", action="version", version=f"diagram-codebase {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>", required=True)

    def add(name: str, fn, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        sp.set_defaults(func=fn)
        return sp

    sub.add_parser("args", help='parse "/diagram-codebase" arguments (raw string or stdin) to JSON')
    sp = add("init", cmd_init, "create or resume a run (out dir, .gitignore, config, manifest)")
    sp.add_argument("--args-json", default=None, help="output of `dc.py args` ('-' reads stdin)")
    add("scan", cmd_scan, "inventory + deterministic findings; prints the analysis brief")
    add("findings-template", cmd_findings_template, "print the findings JSON template")
    sp = add("check-findings", cmd_check_findings, "validate one findings file without merging")
    sp.add_argument("file")
    add("merge", cmd_merge, "merge findings into model.json + validation.json")
    add("plan", cmd_plan, "plan.json + mermaid/*.mmd + checks + publish/review.md")
    add("review", cmd_review, "print the publish review (Markdown)")
    add("confirm", cmd_confirm, "record the user's approval to publish")
    add("next", cmd_next, "print the next Figma action")
    sp = add("record", cmd_record, "record the result of the pending action")
    sp.add_argument("--action", required=True)
    sp.add_argument("--result-file", default=None, help="raw tool result file ('-' reads stdin)")
    sp.add_argument("--error", default=None, help="one-line error text if the tool call failed")
    add("status", cmd_status, "run state summary")
    add("budget", cmd_budget, "local Figma call estimates (not Figma's quota)")
    add("update-diff", cmd_update_diff, "what changed since the last run (--update)")
    add("summary", cmd_summary, "write and print REPORT.md")
    sp = add("run", cmd_run, "init + scan + merge + plan without Claude findings (needs --dry-run)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--args-json", default=None, help="output of `dc.py args`")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        # `args` takes the raw /diagram-codebase string verbatim (it may contain
        # flags like --depth), so it bypasses argparse; skip global options first.
        i = 0
        while i < len(argv) and argv[i] in ("--repo", "--out"):
            i += 2
        if i < len(argv) and argv[i] == "args":
            return cmd_args(argv[i + 1 :])
        args = build_parser().parse_args(argv)
        return int(args.func(args) or 0)
    except DCError as exc:
        sys.stderr.write(f"dc.py: error: {str(exc).splitlines()[0] if str(exc) else 'failed'}\n")
        return 1
    except BrokenPipeError:  # pragma: no cover - stdout closed early
        return 1
