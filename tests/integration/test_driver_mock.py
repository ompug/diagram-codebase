"""Mocked end-to-end driver loop: dc.py next/record against a fake Figma MCP.

The fake models what the real tools and our use_figma scripts do to a FigJam
canvas (top-level nodes, tagged sections), so the driver's state machine is
exercised without network, Figma or Node.
"""

from __future__ import annotations

import copy
import html
import json
import re
from pathlib import Path

import pytest
from diagram_codebase.common import write_json, write_text
from diagram_codebase.config import DEFAULTS
from diagram_codebase.figma import driver, ledger
from diagram_codebase.state import manifest as mf

KEY = "FakeKey123"


# --------------------------------------------------------------------- fakes


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        self.t += 1.0
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeFigma:
    """Fake MCP server. `fail` maps tool -> list of queued error strings (popped per call)."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.top: list[str] = []
        self.seq = 10
        self.file_exists = False
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, list[str]] = {}
        self.drop_labels: set[str] = set()  # titles whose shapes render without text
        self.plans = [{"key": "team::123", "name": "Shop team", "tier": "pro"}]

    def _new(self, ntype: str, name: str, text: str = "", parent: str | None = None) -> str:
        self.seq += 1
        nid = f"1:{self.seq}"
        self.nodes[nid] = {
            "type": ntype,
            "name": name,
            "text": text,
            "children": [],
            "parent": parent,
            "tags": {},
        }
        if parent:
            self.nodes[parent]["children"].append(nid)
        else:
            self.top.append(nid)
        return nid

    def _remove(self, nid: str) -> None:
        node = self.nodes.pop(nid)
        for c in list(node["children"]):
            self._remove(c)
        if node["parent"] is None and nid in self.top:
            self.top.remove(nid)

    def ours(self, nid: str) -> bool:
        n = self.nodes[nid]
        return n["type"] == "SECTION" and bool(n["tags"].get("kind"))

    def call(self, tool: str, params: dict) -> str:
        self.calls.append((tool, params))
        queued = self.fail.get(tool)
        if queued:
            raise RuntimeError(queued.pop(0))
        return getattr(self, tool)(params)

    def whoami(self, p: dict) -> str:
        assert p == {}
        body = {"handle": "Fake User", "email": "fake@example.com", "plans": self.plans}
        return json.dumps([{"type": "text", "text": json.dumps(body)}])

    def generate_diagram(self, p: dict) -> str:
        if "planKey" in p:
            assert re.fullmatch(r"(team|organization)::\d+", p["planKey"])
        if "INVALID" in p["mermaidSyntax"]:
            raise RuntimeError("Invalid Mermaid syntax: parse error on line 2")
        if "fileKey" in p:
            assert p["fileKey"] == KEY
        self.file_exists = True
        labels = driver.expected_labels(None, p["mermaidSyntax"])
        for label in labels:
            if p["name"] in self.drop_labels:
                self._new("SHAPE_WITH_TEXT", "Shape", "")
            else:
                self._new("SHAPE_WITH_TEXT", label, label)
        return f"Created diagram. View it at https://www.figma.com/board/{KEY}/{p['name']}?t=x"

    def use_figma(self, p: dict) -> str:
        assert p["fileKey"] == KEY and "figma-use" in p["skillNames"]
        code = p["code"]
        assert "figma.notify" not in code and "createPage" not in code
        P = json.loads(re.match(r"const P = (\{.*?\});\n", code).group(1))
        if "noContent" in code:
            out = self._place(P)
        elif '"Legend and Index"' in code:
            out = self._legend(P)
        elif "deletedIds" in code:
            out = self._delete(P)
        else:
            raise AssertionError("unknown script")
        # Real MCP results arrive wrapped as content blocks.
        return json.dumps([{"type": "text", "text": json.dumps(out)}])

    def _place(self, P: dict) -> dict:
        for nid in self.top:
            t = self.nodes[nid]["tags"]
            if (t.get("diagram_id"), t.get("content_hash"), t.get("run_id")) == (
                P["diagramId"],
                P["contentHash"],
                P["runId"],
            ):
                return {"sectionId": nid, "alreadyPlaced": True, "texts": ["x"]}
        old = P["replaceSectionId"]
        content = [n for n in self.top if not self.ours(n) and n not in P["ignoreIds"] and n != old]
        if not content:
            return {"sectionId": None, "noContent": True, "foreignTopLevelIds": []}
        sid = self._new("SECTION", P["title"])
        title = self._new("TEXT", "Title", P["title"], parent=sid)
        for n in content:
            self.top.remove(n)
            self.nodes[n]["parent"] = sid
            self.nodes[sid]["children"].append(n)
        self.nodes[sid]["tags"] = {
            "kind": "diagram",
            "diagram_id": P["diagramId"],
            "run_id": P["runId"],
            "content_hash": P["contentHash"],
            "row": str(P["row"]),
        }
        removed = []
        if old and old in self.nodes:
            self._remove(old)
            removed.append(old)
        texts = [
            self.nodes[c]["text"] for c in self.nodes[sid]["children"] if self.nodes[c]["text"]
        ]
        return {
            "sectionId": sid,
            "createdNodeIds": [sid, title],
            "movedNodeIds": content,
            "removedSectionIds": removed,
            "foreignTopLevelIds": [n for n in self.top if not self.ours(n)],
            "texts": texts,
            "bounds": {"x": 0, "y": 0, "width": 100, "height": 100},
        }

    def _legend(self, P: dict) -> dict:
        removed = [n for n in self.top if self.nodes[n]["tags"].get("kind") == "legend"]
        for n in removed:
            self._remove(n)
        sid = self._new("SECTION", "Legend and Index")
        self._new("TEXT", "t", "Legend and Index", parent=sid)
        for c in P["categories"]:
            self._new("STICKY", "s", c["label"], parent=sid)
        self.nodes[sid]["tags"] = {"kind": "legend", "run_id": P["runId"]}
        return {"sectionId": sid, "createdNodeIds": [sid], "removedSectionIds": removed}

    def _delete(self, P: dict) -> dict:
        deleted, missing = [], []
        for i in P["ids"]:
            if i in self.nodes:
                self._remove(i)
                deleted.append(i)
            else:
                missing.append(i)
        return {"deletedIds": deleted, "missingIds": missing, "skippedIds": []}

    def _xml(self, nid: str) -> str:
        n = self.nodes[nid]
        tag = n["type"].lower().replace("_", "-")
        inner = html.escape(n["text"]) + "".join(self._xml(c) for c in n["children"])
        return f'<{tag} id="{nid}" name="{html.escape(n["name"])}">{inner}</{tag}>'

    def get_figjam(self, p: dict) -> str:
        assert p["fileKey"] == KEY and p["nodeId"] == "0:1"
        assert p["includeImagesOfNodes"] is False
        body = "".join(self._xml(n) for n in self.top)
        return f'<figjam fileKey="{KEY}"><page id="0:1" name="Page 1">{body}</page></figjam>'

    def get_screenshot(self, p: dict) -> str:
        return "OK: the section shows the diagram with readable labels."

    def generated_count(self, name: str) -> int:
        return sum(1 for t, p in self.calls if t == "generate_diagram" and p["name"] == name)


# ------------------------------------------------------------------ fixtures


SPECS = {
    "master": {
        "title": "System overview",
        "type": "master",
        "renderer": "architecture",
        "row": 0,
        "priority": 0,
        "labels": ["Api Service", "Orders Database", "Payment Gateway"],
        "categories": ["app", "data", "external"],
    },
    "flow-checkout": {
        "title": "Checkout flow",
        "type": "execution",
        "renderer": "flowchart",
        "row": 2,
        "priority": 10,
        "labels": ["Validate cart", "Charge card", "Send receipt"],
        "categories": ["app", "processing"],
    },
}


def mermaid(labels: list[str], invalid: bool = False) -> str:
    lines = ["flowchart LR"]
    for i, label in enumerate(labels):
        lines.append(f'  n{i}["{label}"]')
    if invalid:
        lines.append("  INVALID -->")
    return "\n".join(lines) + "\n"


def write_plan(out: Path, specs: dict, *, invalid: set[str] = frozenset(), fallback=()) -> None:
    diagrams = []
    for did, s in specs.items():
        text = mermaid(s["labels"], did in invalid)
        write_text(out / "mermaid" / f"{did}.mmd", text)
        if did in fallback:
            write_text(out / "mermaid" / f"{did}.fallback.mmd", mermaid(s["labels"]))
        diagrams.append(
            {
                "id": did,
                "title": s["title"],
                "type": s["type"],
                "renderer": s["renderer"],
                "row": s["row"],
                "priority": s["priority"],
                "purpose": f"What does {s['title']} show",
                "nodes": [
                    {
                        "id": f"n{i}",
                        "label": lbl,
                        "category": s["categories"][i % len(s["categories"])],
                    }
                    for i, lbl in enumerate(s["labels"])
                ],
                "content_hash": s.get("hash") or f"h-{did}-1",
            }
        )
    write_json(out / "plan.json", {"diagrams": diagrams, "skipped": []})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    cache = tmp_path / "cache"
    monkeypatch.setenv("DIAGRAM_CODEBASE_CACHE", str(cache))
    config = copy.deepcopy(DEFAULTS)
    config["budget"].update(per_minute=100, per_day=1000)
    config["figma"]["plan_key"] = "team::123"  # whoami flow has its own tests
    m = mf.new_manifest(repo_name="shop", repo_root=tmp_path, revision="abcdef1234567", options={})
    mf.save(out, m)
    write_plan(out, SPECS)
    return {"out": out, "cache": cache, "config": config, "clock": Clock(), "fig": FakeFigma()}


def nxt(env) -> dict:
    return driver.next_action(
        env["out"], env["config"], now=env["clock"], sleep=env["clock"].sleep, cache=env["cache"]
    )


def rec(env, action, result=None, error=None) -> dict:
    return driver.record(
        env["out"],
        action["action_id"],
        result,
        error,
        env["config"],
        now=env["clock"],
        cache=env["cache"],
        rng=lambda: 0.5,
    )


def run(env, *, max_steps: int = 60, stop_at=("done", "stop", "ask_user")) -> list[dict]:
    """Drive the loop like Claude would; returns every action emitted."""
    seen = []
    for _ in range(max_steps):
        action = nxt(env)
        seen.append(action)
        if action["kind"] == "confirm":
            driver.confirm(env["out"])
            continue
        if action["kind"] in stop_at:
            return seen
        try:
            result = env["fig"].call(action["tool"], action["params"])
        except RuntimeError as exc:
            rec(env, action, None, str(exc))
        else:
            rec(env, action, result)
    raise AssertionError("loop did not finish")


def manifest(env) -> dict:
    return mf.load(env["out"])


def kinds(actions) -> list[str]:
    return [a["kind"] if a["kind"] != "use_figma" else f"use_figma:{a['purpose']}" for a in actions]


# --------------------------------------------------------------------- tests


def test_happy_path(env):
    actions = run(env)
    assert kinds(actions) == [
        "confirm",
        "generate_diagram",
        "use_figma:place",
        "generate_diagram",
        "use_figma:place",
        "use_figma:legend",
        "get_figjam",
        "done",
    ]
    gen1, gen2 = actions[1], actions[3]
    assert "fileKey" not in gen1["params"]
    assert gen1["params"]["useArchitectureLayoutCode"] == "FIGMA_DIAGRAM_2026"
    assert gen1["skill"] == "figma:figma-generate-diagram"
    assert gen2["params"]["fileKey"] == KEY
    assert "useArchitectureLayoutCode" not in gen2["params"]
    assert actions[2]["params"]["skillNames"] == "figma-use,figma-use-figjam"
    m = manifest(env)
    assert m["figma"]["file_key"] == KEY
    assert {d["status"] for d in m["diagrams"].values()} == {"verified"}
    assert m["pending_action"] is None
    assert m["phases"]["publish"]["status"] == "done"
    assert m["hooks_inactive"] is True
    done = actions[-1]
    assert done["file_url"] == f"https://www.figma.com/board/{KEY}"
    assert [d["verify"] for d in done["diagrams"]] == ["ok", "ok"]
    # Hooks never fired, so every counted call left a driver estimate.
    estimates = [e for e in ledger.read_entries(env["cache"]) if e["event"] == "driver_estimate"]
    assert len(estimates) == 6
    assert ledger.read_marker(env["cache"]) is None  # deactivated on done
    assert nxt(env)["kind"] == "done"  # stable after completion


def test_pending_action_persisted_before_return(env):
    driver.confirm(env["out"])
    action = nxt(env)
    m = manifest(env)
    assert m["pending_action"]["action_id"] == action["action_id"]
    assert ledger.read_marker(env["cache"])["run_id"] == m["run_id"]
    assert (env["cache"] / "figma_gate.py").is_file()


def test_record_rejects_wrong_action_and_is_idempotent(env):
    driver.confirm(env["out"])
    action = nxt(env)
    with pytest.raises(Exception, match="not the pending action"):
        driver.record(env["out"], "a-9999", "x", None, env["config"], cache=env["cache"])
    result = env["fig"].call(action["tool"], action["params"])
    rec(env, action, result)
    again = rec(env, action, result)
    assert again["duplicate"] is True


def test_hook_entries_suppress_driver_estimates(env):
    driver.confirm(env["out"])
    action = nxt(env)
    ledger.append(
        {
            "event": "attempt",
            "tool": "generate_diagram",
            "counted": True,
            "source": "hook",
            "ts": env["clock"].t + 0.5,
        },
        env["cache"],
    )
    rec(env, action, env["fig"].call(action["tool"], action["params"]))
    entries = ledger.read_entries(env["cache"])
    assert not [e for e in entries if e["event"] == "driver_estimate"]
    assert manifest(env)["hooks_inactive"] is False


def test_rate_limit_stop_then_resume_without_duplicates(env):
    env2 = env
    action = _until(env2, lambda a: a.get("diagram_id") == "flow-checkout")
    assert action["kind"] == "generate_diagram"
    env2["fig"].calls.append(("generate_diagram", action["params"]))  # the limited call
    rec(env2, action, None, "Error 429: Too Many Requests. Retry after 600 seconds")
    stop = nxt(env2)
    assert stop["kind"] == "stop" and stop["reason"] == "rate_limit"
    assert [r["id"] for r in stop["remaining"]] == ["flow-checkout"]
    assert "--resume" in stop["instructions"]
    assert ledger.read_marker(env2["cache"]) is None
    assert ledger.read_state(env2["cache"])["backoff_until"] > env2["clock"].t + 500
    # Later: the backoff has expired and the user resumes.
    env2["clock"].t += 700
    m, mode = mf.init_run(
        env2["out"], repo_root=".", revision="abcdef1234567", options={"resume": True}
    )
    mf.save(env2["out"], m)
    assert mode == "resume"
    actions = run(env2)
    assert actions[-1]["kind"] == "done"
    assert env2["fig"].generated_count("System overview") == 1
    assert env2["fig"].generated_count("Checkout flow") == 2  # the limited call + the retry
    assert manifest(env2)["diagrams"]["flow-checkout"]["attempts"] == 1


def test_short_backoff_sleeps_instead_of_stopping(env):
    driver.confirm(env["out"])
    action = nxt(env)
    rec(env, action, None, "rate limit exceeded, retry after 20s")
    before = env["clock"].t
    action = nxt(env)
    assert action["kind"] == "generate_diagram"
    assert env["clock"].t - before >= 20


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("401 Unauthorized: please authenticate with Figma", "auth"),
        ("MCP server 'figma' is not connected", "network"),
    ],
)
def test_auth_and_disconnect_stop_with_instructions(env, error, reason):
    env["fig"].fail["generate_diagram"] = [error]
    actions = run(env)
    stop = actions[-1]
    assert stop["kind"] == "stop" and stop["reason"] == reason
    assert "/mcp" in stop["instructions"] and "--resume" in stop["instructions"]
    assert nxt(env)["action_id"] == stop["action_id"]  # re-emitted until resumed
    m = manifest(env)
    assert m["diagrams"]["master"]["attempts"] == 0
    m, _ = mf.init_run(
        env["out"], repo_root=".", revision="abcdef1234567", options={"resume": True}
    )
    mf.save(env["out"], m)
    assert run(env)[-1]["kind"] == "done"


def test_uncertain_first_generate_user_reports_file(env):
    driver.confirm(env["out"])
    action = nxt(env)
    env["fig"].call(action["tool"], action["params"])  # landed, but record never happened
    ask = nxt(env)
    assert ask["kind"] == "ask_user" and "System overview" in ask["params"]["question"]
    assert nxt(env)["action_id"] == ask["action_id"]  # stays pending until answered
    out = rec(env, ask, f"https://www.figma.com/board/{KEY}/System-overview")
    assert out["landed"] is True
    actions = run(env)
    assert actions[0]["purpose"] == "place"
    assert actions[-1]["kind"] == "done"
    assert env["fig"].generated_count("System overview") == 1


def test_uncertain_first_generate_user_says_none(env):
    driver.confirm(env["out"])
    nxt(env)  # emitted, never called
    ask = nxt(env)
    rec(env, ask, "none")
    actions = run(env)
    assert actions[0]["kind"] == "generate_diagram" and "fileKey" not in actions[0]["params"]
    assert actions[-1]["kind"] == "done"


def _until(env, pred):
    while True:
        action = nxt(env)
        if action["kind"] == "confirm":
            driver.confirm(env["out"])
            continue
        if pred(action):
            return action
        rec(env, action, env["fig"].call(action["tool"], action["params"]))


@pytest.mark.parametrize("landed", [True, False])
def test_uncertain_generate_reconciled_with_get_figjam(env, landed):
    action = _until(env, lambda a: a.get("diagram_id") == "flow-checkout")
    assert action["kind"] == "generate_diagram"
    if landed:
        env["fig"].call(action["tool"], action["params"])
    check = nxt(env)
    assert check["kind"] == "get_figjam" and check["purpose"] == "reconcile_generate"
    out = rec(env, check, env["fig"].call(check["tool"], check["params"]))
    assert out["landed"] is landed
    following = nxt(env)
    if landed:
        assert following["purpose"] == "place"
    else:
        assert following["kind"] == "generate_diagram"
    rec(env, following, env["fig"].call(following["tool"], following["params"]))
    assert run(env)[-1]["kind"] == "done"
    assert env["fig"].generated_count("Checkout flow") == 1
    assert manifest(env)["diagrams"]["flow-checkout"]["status"] == "verified"


def test_invalid_mermaid_twice_fails(env):
    write_plan(env["out"], SPECS, invalid={"flow-checkout"})
    actions = run(env)
    assert env["fig"].generated_count("Checkout flow") == 2
    m = manifest(env)
    flow = m["diagrams"]["flow-checkout"]
    assert flow["status"] == "failed" and flow["attempts"] == 2
    assert all(e["class"] == "invalid_request" for e in flow["errors"])
    done = actions[-1]
    assert done["kind"] == "done" and done["failed"] == ["flow-checkout"]
    assert m["phases"]["publish"]["status"] == "partial"


def test_architecture_falls_back_to_flowchart(env):
    write_plan(env["out"], SPECS, invalid={"master"}, fallback={"master"})
    actions = run(env)
    gens = [a for a in actions if a["kind"] == "generate_diagram" and a["diagram_id"] == "master"]
    assert len(gens) == 2
    assert "useArchitectureLayoutCode" in gens[0]["params"]
    assert "useArchitectureLayoutCode" not in gens[1]["params"]
    assert "INVALID" not in gens[1]["params"]["mermaidSyntax"]
    assert manifest(env)["diagrams"]["master"]["status"] == "verified"


def test_placement_failure_retries_without_regenerating(env):
    env["fig"].fail["use_figma"] = ["Error: in appendChild: unexpected internal error"]
    actions = run(env)
    assert actions[-1]["kind"] == "done"
    assert env["fig"].generated_count("System overview") == 1
    places = [a for a in actions if a.get("purpose") == "place" and a["diagram_id"] == "master"]
    assert len(places) == 2
    assert manifest(env)["diagrams"]["master"]["place_attempts"] == 1


def test_budget_exhaustion_stops_with_remaining_work(env):
    env["config"]["budget"]["per_day"] = 3
    actions = run(env)
    stop = actions[-1]
    assert stop["kind"] == "stop" and stop["reason"] == "budget"
    assert [r["id"] for r in stop["remaining"]] == ["flow-checkout"]
    assert "/diagram-codebase --resume" in stop["instructions"]
    assert stop["usage"]["note"].startswith("local estimate")
    assert env["fig"].generated_count("Checkout flow") == 0
    assert manifest(env)["diagrams"]["master"]["status"] == "placed"


def test_verify_mismatch_takes_screenshot(env):
    env["fig"].drop_labels.add("Checkout flow")
    actions = run(env)
    shots = [a for a in actions if a["kind"] == "get_screenshot"]
    assert len(shots) == 1 and shots[0]["diagram_id"] == "flow-checkout"
    flow = manifest(env)["diagrams"]["flow-checkout"]
    assert flow["verify"]["status"] == "visual_ok"
    assert flow["verify"]["visual"]["status"] == "ok"


def test_verify_visual_option_screenshots_everything(env):
    m = manifest(env)
    m["options"]["verify_visual"] = True
    mf.save(env["out"], m)
    actions = run(env)
    assert len([a for a in actions if a["kind"] == "get_screenshot"]) == 2


def test_update_replaces_stale_and_deletes_obsolete(env):
    run(env)
    m = manifest(env)
    old_master = m["diagrams"]["master"]["section_id"]
    old_flow = m["diagrams"]["flow-checkout"]["section_id"]
    specs = {
        "master": {
            **SPECS["master"],
            "labels": ["Api Service", "Orders Database", "Ledger"],
            "hash": "h-master-2",
        },
        "seq-login": {
            "title": "Login sequence",
            "type": "sequence",
            "renderer": "flowchart",
            "row": 2,
            "priority": 20,
            "labels": ["Browser", "Auth Service"],
            "categories": ["app"],
        },
    }
    write_plan(env["out"], specs)
    m, mode = mf.init_run(env["out"], repo_root=".", revision="fffffff", options={"update": True})
    mf.save(env["out"], m)
    assert mode == "update"
    actions = run(env)
    assert kinds(actions) == [
        "confirm",
        "generate_diagram",
        "use_figma:place",
        "generate_diagram",
        "use_figma:place",
        "use_figma:legend",
        "use_figma:delete",
        "get_figjam",
        "done",
    ]
    replace = actions[2]
    assert f'"replaceSectionId": "{old_master}"' in replace["params"]["code"]
    assert actions[1]["params"]["fileKey"] == KEY
    fig = env["fig"]
    assert old_master not in fig.nodes and old_flow not in fig.nodes
    m = manifest(env)
    assert m["diagrams"]["flow-checkout"]["status"] == "obsolete"
    assert m["diagrams"]["flow-checkout"]["section_id"] is None
    assert m["diagrams"]["master"]["status"] == "verified"
    assert m["diagrams"]["seq-login"]["status"] == "verified"
    legends = [n for n in fig.top if fig.nodes[n]["tags"].get("kind") == "legend"]
    assert len(legends) == 1


def test_parse_figjam_tolerates_unknown_shapes():
    xml = (
        '<figjam><page id="0:1" name="P"><section id="2:1" name="S">'
        '<shape-with-text id="2:2" name="Api &amp; Web" />'
        '<group id="2:3"><text id="2:4">Orders DB</text></group></section>'
        '<sticky id="2:9" text="loose" /></page></figjam>'
    )
    nodes = driver.parse_figjam(xml)
    assert nodes["2:1"]["top"] and nodes["2:9"]["top"]
    assert not nodes["2:2"]["top"] and nodes["2:4"]["parent"] == "2:3"
    blob = " ".join(nodes["2:1"]["text"]).lower()
    assert "api & web" in blob and "orders db" in blob


def test_expected_labels_from_mermaid_fallback():
    text = 'flowchart LR\n  a["Api (main)"] --> b[(Orders)]\n  c{Decide?}\n'
    assert driver.expected_labels(None, text) == ["api (main)", "orders", "decide?"]
    seq = "sequenceDiagram\n  participant Browser\n  participant AuthService\n"
    assert driver.expected_labels(None, seq) == ["browser", "authservice"]


def test_hook_denied_call_consumes_no_attempt(env):
    driver.confirm(env["out"])
    action = nxt(env)
    reason = (
        "diagram-codebase: daily Figma call budget reached (160/160 in 24 h, local estimate). "
        "Budget reached: state saved by dc.py; resume later with /diagram-codebase --resume."
    )
    out = rec(env, action, None, reason)
    assert out["outcome"] == "denied"
    m = manifest(env)
    assert m["diagrams"]["master"]["attempts"] == 0 and not m["diagrams"]["master"]["errors"]
    assert not [e for e in ledger.read_entries(env["cache"]) if e["event"] == "driver_estimate"]
    assert nxt(env)["kind"] == "generate_diagram"


# ------------------------------------------------------------- whoami / planKey


def _no_plan_key(env):
    env["config"]["figma"]["plan_key"] = ""
    return env


def test_whoami_single_plan_sets_plan_key(env):
    actions = run(_no_plan_key(env))
    assert kinds(actions)[:3] == ["confirm", "whoami", "generate_diagram"]
    gens = [p for t, p in env["fig"].calls if t == "generate_diagram"]
    assert gens and all(p["planKey"] == "team::123" for p in gens)
    m = manifest(env)
    assert m["figma"]["plan_key"] == "team::123"
    text = json.dumps(m)
    assert "fake@example.com" not in text and "Fake User" not in text
    assert actions[-1]["kind"] == "done"


def test_whoami_several_plans_asks_user(env):
    _no_plan_key(env)
    env["fig"].plans = [
        {"key": "team::1", "name": "Personal"},
        {"key": "organization::2", "name": "Acme Org"},
    ]
    actions = run(env)
    assert actions[-1]["kind"] == "ask_user"
    assert "Acme Org (organization::2)" in actions[-1]["params"]["question"]
    bad = rec(env, actions[-1], json.dumps({"answer": "something else"}))
    assert bad["outcome"] == "error"
    again = nxt(env)
    assert again["kind"] == "ask_user"
    assert rec(env, again, json.dumps({"answer": "acme org"}))["plan_key"] == "organization::2"
    rest = run(env)
    assert rest[-1]["kind"] == "done"
    gens = [p for t, p in env["fig"].calls if t == "generate_diagram"]
    assert all(p["planKey"] == "organization::2" for p in gens)


def test_whoami_without_plans_generates_without_plan_key(env):
    _no_plan_key(env)
    env["fig"].plans = []
    actions = run(env)
    assert actions[-1]["kind"] == "done"
    gens = [p for t, p in env["fig"].calls if t == "generate_diagram"]
    assert gens and all("planKey" not in p for p in gens)
    assert manifest(env)["figma"]["plan_checked"] is True


def test_place_script_identifies_sections_by_name_prefix():
    from diagram_codebase.figma import scripts

    code = scripts.place_section(
        diagram_id="master", title="Overview", run_id="r1", content_hash="h", row=0,
        known_ids=["1:5"],
    )  # fmt: skip
    params = json.loads(re.match(r"const P = (\{.*?\});\n", code).group(1))
    assert params["sectionName"] == scripts.SECTION_PREFIX + "Overview"
    assert params["knownIds"] == ["1:5"]
    assert "setPluginData(" not in code and "loadAllPagesAsync" not in code
    assert "n.setSharedPluginData" in code and "try {" in code
    legend = scripts.legend_and_index(
        run_id="r1", categories=["app"], diagrams=[], repo_name="x", revision=None, date="d"
    )
    assert "setPluginData(" not in legend and json.dumps(scripts.LEGEND_NAME) in legend
