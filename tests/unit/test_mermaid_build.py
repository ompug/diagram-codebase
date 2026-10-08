"""Tests for DiagramSpec -> Mermaid rendering (all five renderers)."""

from __future__ import annotations

import re

import pytest
from diagram_codebase.mermaid import build, parsecheck
from diagram_codebase.mermaid.lint import errors, lint
from diagram_codebase.sanitize import Redactor

NASTY = [
    'say "hi" [x] {y} (z)',
    "a|b;c #35; d <b>e</b> <T> & f",
    "end",
    "x 🚀 ✅ 日本語 ünïcödé",
    "L" * 300,
    "pipe | and %% comment",
    "graph",
    "subgraph",
    "o",
    "x",
    "a --> b ==> c -.-> d",
    "C:\\Users\\dev\\proj\\main.py",
    "token = abc123def456",
    "",
]


def nasty_flowchart() -> dict:
    shapes = list(build.SHAPES)
    cats = list(build.PALETTE)
    nodes = [
        {
            "id": f"n_{i}",
            "label": NASTY[i % len(NASTY)],
            "shape": shapes[i % len(shapes)],
            "category": cats[i % len(cats)],
            "group": ["g1", "g2", "g3", None][i % 4],
        }
        for i in range(16)
    ]
    nodes += [
        {"id": "end", "label": "end"},
        {"id": "a_b", "label": "a_b"},
        {"id": "aB", "label": "aB"},
        {"id": "a-b", "label": "a-b"},
        {"id": "o", "label": "o"},
        {"id": "x", "label": "x"},
        {"id": "style", "label": "style"},
    ]
    edges = [
        {
            "from": f"n_{i}",
            "to": f"n_{i + 1}",
            "label": NASTY[(i + 3) % len(NASTY)],
            "style": ["solid", "dotted", "thick"][i % 3],
            "end": ["arrow", "cross", "circle", "none"][i % 4],
            "bidir": i % 5 == 0,
        }
        for i in range(15)
    ]
    edges += [
        {"from": "end", "to": "o", "label": "end"},
        {"from": "o", "to": "x"},
        {"from": "a_b", "to": "aB"},
        {"from": "aB", "to": "a-b", "label": "one"},
        {"from": "aB", "to": "a-b", "label": "two"},
        {"from": "style", "to": "style"},
    ]
    return {
        "id": "nasty-flow",
        "renderer": "flowchart",
        "direction": "TD",
        "groups": [
            {"id": "g1", "label": "end", "category": "data"},
            {"id": "g2", "label": 'sub "two"', "parent": "g1"},
            {"id": "g3", "label": "too deep", "parent": "g2", "category": "error"},
            {"id": "empty", "label": "nothing here"},
        ],
        "nodes": nodes,
        "edges": edges,
    }


def nasty_sequence() -> dict:
    names = [
        "service.py",
        "Service",
        "end",
        "Note",
        "loop",
        "x",
        "API",
        "auth server",
        "🚀",
        "Participant",
    ]
    return {
        "id": "nasty-seq",
        "renderer": "sequence",
        "title": 'Login: flow; #1 "quoted"',
        "participants": [{"id": f"p{i}", "label": n} for i, n in enumerate(names)],
        "messages": [
            {"from": f"p{i}", "to": f"p{(i + 1) % len(names)}", "label": lab, "kind": kind}
            for i, (lab, kind) in enumerate(
                [
                    ("GET /api/todos/{}: 200; ok #3", "sync"),
                    ("", "reply"),
                    ("a->>b -->> c", "async"),
                    ("Note over A: hi", "sync"),
                    ("%% not a comment", "sync"),
                    ("end", None),
                    ("<b>bold</b> 🚀", "reply"),
                    ("L" * 200, "sync"),
                    ("loop", "sync"),
                    ("+activate", "sync"),
                ]
            )
        ]
        + [{"from": "p0", "to": "undeclared", "label": "implicit"}],
    }


def nasty_erd() -> dict:
    return {
        "id": "nasty-erd",
        "renderer": "erd",
        "direction": "LR",
        "entities": [
            {
                "id": "ent:users",
                "label": 'User "Account"',
                "attributes": [
                    {"name": "id", "type": "int", "key": "PK"},
                    {"name": "team-id", "type": "varchar(255)", "key": ["FK", "UK"]},
                    {"name": "1st", "type": "", "key": None},
                    {"name": "naïve name", "type": "text"},
                ],
            },
            {"id": "ent:orders", "attributes": []},
            {"id": "ent:end", "label": "end"},
            {"id": "ent:one", "attributes": [{"name": "x", "type": "int"}]},
            {"id": "ent:line_item", "attributes": [{"name": "qty", "type": "int"}]},
        ],
        "relations": [
            {
                "from": "ent:users",
                "to": "ent:orders",
                "cardinality": "one-to-many",
                "label": 'places "x"; #y',
                "identifying": True,
            },
            {"from": "ent:orders", "to": "ent:end", "cardinality": "many-to-many", "label": ""},
            {"from": "ent:users", "to": "ent:one", "cardinality": "one-to-one", "label": "has"},
            {
                "from": "ent:orders",
                "to": "ent:line_item",
                "cardinality": "weird",
                "label": "contains",
            },
            {
                "from": "ent:line_item",
                "to": "ent:many",
                "cardinality": "many_to_one",
                "label": "refs",
            },
        ],
    }


def nasty_state() -> dict:
    return {
        "id": "nasty-state",
        "renderer": "state",
        "direction": "TD",
        "states": [
            {"id": "idle", "label": 'Idle: waiting "x"'},
            {"id": "Run", "label": "Run"},
            {"id": "end", "label": "end"},
            {"id": "note", "label": "note"},
            {"id": "state", "label": "state"},
            {"id": "Lonely"},
            {"id": "s_1", "label": "S 1 🚀 <b>x</b>"},
        ],
        "initial": "idle",
        "finals": ["end"],
        "transitions": [
            {"from": "idle", "to": "Run", "label": "start: now; #1"},
            {"from": "Run", "to": "end"},
            {"from": "Run", "to": "note", "label": "x"},
            {"from": "note", "to": "state", "label": "classDef y"},
            {"from": "state", "to": "s_1"},
            {"from": "s_1", "to": "Run", "label": "retry"},
        ],
    }


def nasty_architecture() -> dict:
    lanes = {
        "web": "client",
        "alb": "gateway",
        "auth": "service",
        "orders": "service",
        "billing": "service",
        "pg": "datastore",
        "queue": "async",
        "stripe": "external",
        "end": "service",
        "service": "service",
    }
    return {
        "id": "nasty-arch",
        "renderer": "architecture",
        "nodes": [{"id": k, "label": k} for k in lanes],
        "lanes": lanes,
        "lane_labels": {"service": 'Core "Services"'},
        "edges": [
            {"from": "web", "to": "alb", "label": "HTTPS"},
            {"from": "web", "to": "alb", "label": "WebSocket", "bidir": True},
            {"from": "alb", "to": "auth", "label": "Routes /auth"},
            {"from": "alb", "to": "orders"},
            {"from": "auth", "to": "orders"},
            {"from": "orders", "to": "billing"},
            {"from": "billing", "to": "auth", "label": "invalidate"},  # closes a cycle
            {"from": "orders", "to": "auth", "label": "callback"},  # reverse of auth->orders
            {"from": "orders", "to": "pg", "label": "Writes"},
            {"from": "billing", "to": "pg"},
            {"from": "orders", "to": "queue", "label": "events", "bidir": True},
            {"from": "billing", "to": "stripe", "label": "Stripe: Charges", "style": "solid"},
            {"from": "end", "to": "service"},
            {"from": "auth", "to": "end"},
            {"from": "service", "to": "pg"},
        ],
    }


ALL = [nasty_flowchart, nasty_sequence, nasty_erd, nasty_state, nasty_architecture]


@pytest.mark.parametrize("make", ALL, ids=lambda f: f.__name__)
def test_nasty_specs_lint_clean(make):
    spec = make()
    text = build.render(spec)
    errs = errors(lint(text, spec["renderer"]))
    assert errs == [], text
    assert "\\n" not in text
    assert "🚀" not in text and "<b>" not in text
    assert build.render(make()) == text  # deterministic


@pytest.mark.skipif(not parsecheck.available(), reason="mermaid-check (node + deps) not installed")
def test_nasty_specs_parse_with_mermaid():
    items = [{"id": make.__name__, "text": build.render(make())} for make in ALL]
    result = parsecheck.check(items)
    assert result["status"] == "ok", result
    assert result["results"] == {item["id"]: None for item in items}


def test_flowchart_ids_are_figma_safe_and_unique():
    spec = nasty_flowchart()
    ids = build.id_map(spec)
    assert set(ids) == {n["id"] for n in spec["nodes"]}
    values = list(ids.values())
    assert len({v.lower() for v in values}) == len(values)
    for v in values:
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", v), v
        assert len(v) <= 34
        assert v.lower() not in build.FLOW_RESERVED
    assert ids["end"] == "endNode"
    assert ids["style"] == "styleNode"
    # a_b, aB and a-b collide after camelCasing; sorted spec ids decide who gets the suffix.
    assert sorted([ids["a_b"], ids["aB"], ids["a-b"]]) == ["aB", "aB2", "aB3"]


def test_long_ids_are_truncated_with_hash():
    long_id = "fn:" + "/".join(["very_long_directory_name"] * 4) + ":Klass.method"
    spec = {
        "renderer": "flowchart",
        "nodes": [{"id": long_id, "label": "x"}, {"id": long_id + "2", "label": "y"}],
    }
    ids = build.id_map(spec)
    assert all(len(v) <= build.MAX_ID for v in ids.values())
    assert len(set(ids.values())) == 2


def test_flowchart_shapes_edges_and_styles():
    spec = {
        "renderer": "flowchart",
        "direction": "LR",
        "groups": [{"id": "g", "label": "Group", "category": "data"}],
        "nodes": [
            {"id": "a", "label": "A", "shape": "cylinder", "category": "data", "group": "g"},
            {"id": "b", "label": "B (main)", "shape": "diamond", "category": "app"},
            {"id": "c", "label": "C", "shape": "lean_l"},
            {"id": "d", "label": "D", "shape": "odd"},
        ],
        "edges": [
            {"from": "a", "to": "b", "label": "O(1) lookup"},
            {"from": "b", "to": "c", "style": "dotted"},
            {"from": "c", "to": "d", "style": "thick", "end": "cross"},
            {"from": "d", "to": "a", "end": "circle"},
            {"from": "a", "to": "c", "end": "none"},
            {"from": "b", "to": "d", "bidir": True},
        ],
    }
    text = build.render(spec)
    assert text.startswith("flowchart LR\n")
    for fragment in [
        'subgraph grpG ["Group"]',
        'a[("A")]',
        'b{"B (main)"}',
        'c[\\"C"\\]',
        'd>"D"]',
        'a -->|"O(1) lookup"| b',
        "b -.-> c",
        "c ==x d",
        "d --o a",
        "a --- c",
        "b <--> d",
        "classDef data fill:#CDF4D3,stroke:#66D575",
        "classDef app fill:#C2E5FF,stroke:#3DADFF",
        "class a data",
        "class b app",
        "style grpG fill:#CDF4D3,stroke:#66D575",
    ]:
        assert fragment in text, fragment
    assert "classDef processing" not in text  # only used categories


def test_duplicate_edges_are_merged():
    spec = {
        "renderer": "flowchart",
        "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        "edges": [
            {"from": "a", "to": "b", "label": "reads"},
            {"from": "a", "to": "b", "label": "writes"},
        ],
    }
    assert 'a -->|"reads, writes"| b' in build.render(spec)


def test_nesting_is_capped():
    spec = nasty_flowchart()
    text = build.render(spec)
    depth = max_depth = 0
    for line in text.splitlines():
        if line.strip().startswith("subgraph "):
            depth += 1
            max_depth = max(max_depth, depth)
        elif line.strip() == "end":
            depth -= 1
    assert max_depth == 2
    assert "nothing here" not in text  # empty groups are not drawn


def test_unknown_edge_endpoint_raises():
    spec = {
        "id": "d",
        "renderer": "flowchart",
        "nodes": [{"id": "a"}],
        "edges": [{"from": "a", "to": "zz"}],
    }
    with pytest.raises(ValueError, match="zz"):
        build.render(spec)


def test_labels_are_sanitized_and_counted():
    spec = {
        "renderer": "flowchart",
        "nodes": [
            {"id": "a", "label": "/home/alice/app/db.py"},
            {"id": "b", "label": "owner bob@example.com"},
        ],
        "edges": [{"from": "a", "to": "b", "label": "password = hunter2"}],
    }
    red = Redactor()
    text = build.render(spec, redactor=red)
    assert "alice" not in text and "bob@" not in text and "hunter2" not in text
    assert 'a["db.py"]' in text
    assert red.report()["by_category"] == {"credential_assignment": 1, "email": 1, "home_path": 1}


def test_display_label():
    assert build.display_label('a "b"; c|d #35; <e>') == "a 'b' c/d \u2039e\u203a"
    assert build.display_label("") == "unnamed"
    long = build.display_label("w " * 100)
    assert len(long) <= build.MAX_LABEL and long.endswith("...")


def test_architecture_rules():
    spec = nasty_architecture()
    text = build.render(spec)
    ids = build.id_map(spec)
    assert text.startswith("flowchart LR\n")
    assert "classDef" not in text and "style " not in text
    for lane in ("client", "gateway", "service", "datastore", "external", "async"):
        assert f"subgraph {lane} [" in text
    assert "subgraph service [\"Core 'Services'\"]" in text
    assert ids["service"] != "service" and ids["end"] != "end"
    # merged duplicate client->gateway edge becomes one bidirectional edge
    assert 'web <-->|"HTTPS, WebSocket"| alb' in text
    # async bidirectional -> two dotted edges; external always dotted
    assert 'orders -.->|"events"| queue' in text
    assert 'queue -.->|"events"| orders' in text
    assert 'billing -.->|"Stripe: Charges"| stripe' in text
    # cycle-closing edge is drawn backward
    assert 'auth <---|"invalidate"| billing' in text
    assert "<-.->" not in text


def test_architecture_requires_lanes():
    spec = {"id": "a", "renderer": "architecture", "nodes": [{"id": "x"}], "lanes": {}, "edges": []}
    with pytest.raises(ValueError, match="lane"):
        build.render(spec)


def test_sequence_participants_are_readable_and_unique():
    spec = nasty_sequence()
    ids = build.id_map(spec)
    assert ids["p0"] == "Service"  # service.py
    assert ids["p1"] == "Service2"
    assert ids["p2"] == "EndActor"
    assert ids["p3"] == "NoteActor"
    assert ids["p4"] == "LoopActor"
    assert ids["p7"] == "AuthServer"
    assert ids["undeclared"] == "Undeclared"
    for v in ids.values():
        assert re.fullmatch(r"[A-Z][A-Za-z0-9]*", v), v
    text = build.render(spec)
    assert " as " not in text
    starts = {line.split()[0] for line in text.splitlines()}
    assert not starts & {"Note", "loop", "end", "activate", "autonumber"}
    assert "title Login - flow 1 'quoted'" in text
    assert "Service->>Service2: GET /api/todos/{} - 200 ok 3" in text
    assert "Service2-->>EndActor: return" in text  # empty reply gets a neutral label
    assert "participant P8" in text  # emoji-only label falls back to the spec id
    assert "Service->>Undeclared: implicit" in text
    assert ";" not in text and "#" not in text


def test_sequence_arrows():
    spec = {
        "renderer": "sequence",
        "participants": [{"id": "c", "label": "Client"}, {"id": "s", "label": "Server"}],
        "messages": [
            {"from": "c", "to": "s", "label": "GET /x", "kind": "sync"},
            {"from": "s", "to": "c", "label": "200", "kind": "reply"},
            {"from": "c", "to": "s", "label": "ping", "kind": "async"},
        ],
    }
    assert build.render(spec) == (
        "sequenceDiagram\n"
        "    participant Client\n"
        "    participant Server\n"
        "    Client->>Server: GET /x\n"
        "    Server-->>Client: 200\n"
        "    Client-)Server: ping\n"
    )


def test_erd_render():
    spec = nasty_erd()
    text = build.render(spec)
    ids = build.id_map(spec)
    assert ids["ent:users"] == "users"
    assert ids["ent:end"] == "end_entity"
    assert ids["ent:one"] == "one_entity"
    assert ids["ent:line_item"] == "line_item"
    assert ids["ent:many"] == "many_entity"
    assert "users[\"User 'Account'\"] {" in text
    assert "int id PK" in text
    assert "varchar_255 team_id FK, UK" in text
    assert "string f_1st" in text
    assert "text na_ve_name" in text or "text naive_name" in text
    assert "users ||--o{ orders : \"places 'x' #y\"" in text
    assert 'orders }o..o{ end_entity : ""' in text
    assert 'users ||..|| one_entity : "has"' in text
    assert 'orders }o..o{ line_item : "contains"' in text  # unknown cardinality -> weakest claim
    assert 'line_item }o..|| many_entity : "refs"' in text
    assert "classDef" not in text and "style" not in text


def test_state_render():
    spec = nasty_state()
    text = build.render(spec)
    ids = build.id_map(spec)
    assert ids == {
        "Lonely": "Lonely",
        "Run": "Run",
        "end": "EndState",
        "idle": "Idle",
        "note": "NoteState",
        "s_1": "S1",
        "state": "StateState",
    }
    assert text.startswith("stateDiagram-v2\n    direction TB\n")
    assert "state \"Idle - waiting 'x'\" as Idle" in text
    assert "    Lonely\n" in text
    assert "[*] --> Idle" in text
    assert "Idle --> Run : start - now 1" in text
    assert "EndState --> [*]" in text
    assert 'state "S 1 x" as S1' in text
    starts = {line.split()[0] for line in text.splitlines()}
    assert not starts & {"classDef", "class", "style", "note"}
    assert ":::" not in text


def test_unknown_renderer():
    with pytest.raises(ValueError, match="renderer"):
        build.render({"id": "z", "renderer": "pie"})


def _fuzz_specs(seed: int) -> list[dict]:
    import random

    rng = random.Random(seed)
    alphabet = list("abcXYZ019 _-.,:;|#&%@!?/\\\"'`()[]{}<>=+*~^$") + [
        "\n",
        "\\n",
        "🚀",
        "é",
        "end",
        "<br>",
    ]

    def text() -> str:
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 25)))

    ids = [text() or "empty" for _ in range(8)]
    ids = list(dict.fromkeys(ids))
    pairs = [(rng.choice(ids), rng.choice(ids)) for _ in range(10)]
    return [
        {
            "renderer": "flowchart",
            "groups": [{"id": "g", "label": text()}],
            "nodes": [{"id": i, "label": text(), "group": rng.choice(["g", None])} for i in ids],
            "edges": [{"from": a, "to": b, "label": text()} for a, b in pairs],
        },
        {
            "renderer": "sequence",
            "title": text(),
            "participants": [{"id": i, "label": text()} for i in ids],
            "messages": [
                {
                    "from": a,
                    "to": b,
                    "label": text(),
                    "kind": rng.choice(["sync", "reply", "async"]),
                }
                for a, b in pairs
            ],
        },
        {
            "renderer": "erd",
            "entities": [
                {
                    "id": i,
                    "label": text(),
                    "attributes": [{"name": text(), "type": text(), "key": "PK"}],
                }
                for i in ids
            ],
            "relations": [
                {"from": a, "to": b, "label": text(), "cardinality": "one-to-many"}
                for a, b in pairs
            ],
        },
        {
            "renderer": "state",
            "states": [{"id": i, "label": text()} for i in ids],
            "initial": ids[0],
            "finals": [ids[-1]],
            "transitions": [{"from": a, "to": b, "label": text()} for a, b in pairs],
        },
    ]


@pytest.mark.parametrize("seed", range(5))
def test_fuzzed_labels_lint_clean(seed):
    for spec in _fuzz_specs(seed):
        text = build.render(spec)
        assert errors(lint(text, spec["renderer"])) == [], text


@pytest.mark.skipif(not parsecheck.available(), reason="mermaid-check (node + deps) not installed")
def test_fuzzed_labels_parse_with_mermaid():
    items = [
        {"id": f"{seed}-{spec['renderer']}", "text": build.render(spec)}
        for seed in range(5)
        for spec in _fuzz_specs(seed)
    ]
    result = parsecheck.check(items)
    assert result["status"] == "ok", result
    bad = {k: v for k, v in result["results"].items() if v}
    assert bad == {}, bad
