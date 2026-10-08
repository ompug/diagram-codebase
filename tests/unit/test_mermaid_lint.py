"""Tests for the Figma Mermaid linter, mostly against hand-written/hostile input."""

from __future__ import annotations

import pytest
from diagram_codebase.mermaid.lint import _Issues, _parse_flow, errors, lint


def rules(
    text: str, renderer: str = "flowchart", config: dict | None = None, severity: str | None = None
):
    return {
        i["rule"]
        for i in lint(text, renderer, config)
        if severity is None or i["severity"] == severity
    }


GOOD_FLOW = """flowchart LR
    dev[/"Developer commit"/]
    ci["CI Build"]
    test{"Tests pass?"}
    subgraph deploy ["Deploy Pipeline"]
        stage["Staging"]
        prod["Production"]
    end
    dev --> ci
    ci -->|"Uses cache"| test
    test -->|"Yes"| stage --> prod
    prod -.->|"Deploy event"| dev
    style deploy fill:#C2E5FF,stroke:#3DADFF
    classDef ok fill:#CDF4D3,stroke:#66D575
    class prod ok
"""

GOOD_ARCH = """flowchart LR
    subgraph client ["Client Apps"]
        web["Web App"]
    end
    subgraph gateway ["API Layer"]
        alb["Load Balancer"]
    end
    subgraph service ["Core Services"]
        auth["Auth Service"]
        orders["Order Service"]
        notify["Notifications"]
    end
    subgraph datastore ["Data Stores"]
        pg["PostgreSQL"]
    end
    subgraph external ["External"]
        stripe["Stripe"]
    end
    subgraph async ["Event Streaming"]
        orderQ["Order Queue"]
    end
    web <-->|"HTTPS"| alb
    alb -->|"Routes /auth"| auth
    alb -->|"Routes /orders"| orders
    auth -->|"Reads Sessions"| pg
    orders -->|"Writes Orders"| pg
    orders -.->|"Produces"| orderQ
    orderQ -.->|"Consumes"| notify
    notify <---|"Ack"| orders
    orders -.->|"Stripe: Charges"| stripe
"""


def test_good_examples_are_clean():
    assert lint(GOOD_FLOW, "flowchart") == []
    assert errors(lint(GOOD_ARCH, "architecture")) == []


def test_flow_parser_handles_chains_and_ampersands():
    text = "flowchart LR\n  a & b --> c & d --> e\n  f -- text label --> g\n  h -. dotted .-> i\n  j ---oK\n"
    issues = _Issues()
    flow = _parse_flow(text.splitlines(), 1, {"max_nesting": 2}, issues)
    pairs = {(e.src, e.dst) for e in flow.edges}
    assert {("a", "c"), ("a", "d"), ("b", "c"), ("b", "d"), ("c", "e"), ("d", "e")} <= pairs
    assert ("f", "g") in pairs and ("h", "i") in pairs and ("j", "K") in pairs
    labels = {(e.src, e.dst): e.label for e in flow.edges}
    assert labels[("f", "g")] == "text label"
    assert issues.items == []


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('flowchart LR\n  a["Ship it 🚀"] --> b', "emoji"),
        ('flowchart LR\n  a["line\\nbreak"] --> b', "literal-newline"),
        ('flowchart LR\n  a["<b>bold</b>"] --> b', "html-tag"),
        ('flowchart LR\n  a["x<br/>y"] --> b', "html-tag"),
        ("flowchart LR\n  end --> b", "reserved-id"),
        ('flowchart LR\n  graph["x"] --> b', "reserved-id"),
        ("flowchart LR\n  user_service --> b", "underscore-id"),
        ("flowchart LR\n  a[Process (main)] --> b", "unquoted-label"),
        ("flowchart LR\n  a -->|O(1) lookup| b", "unquoted-label"),
        ('flowchart LR\n  a["x"] --> b\n  click a href "javascript:alert(1)"', "click"),
        ('flowchart LR\n  subgraph s ["S"]\n  a --> b\n', "unbalanced-subgraph"),
        ("flowchart LR\n  a --> b\n  end", "unbalanced-end"),
        ('flowchart LR\n  a["unterminated] --> b', "unbalanced-quote"),
        ("flowchart LR\n  a --> b\x07", "control-char"),
        ("flowchart LR\n  a‮ --> b", "control-char"),
        ("graph LR\n  a --> b", "header"),
        ('pie\n  "a": 1', "header"),
        ("sequenceDiagram\n  A->>B: x", "header"),
        ("---\ntitle: x\n---\nflowchart LR\n a --> b", "front-matter"),
        ("flowchart LR\n  a --> ]]]", "syntax"),
    ],
)
def test_flowchart_errors(text, rule):
    assert rule in rules(text, "flowchart", severity="error")


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (
            'flowchart LR\n  subgraph s1 ["A"]\n  subgraph s2 ["B"]\n  subgraph s3 ["C"]\n  a --> b\n  end\n  end\n  end',
            "nesting",
        ),
        ('flowchart LR\n  subgraph grp ["G"]\n  a\n  end\n  b --> grp', "edge-to-subgraph"),
        ("flowchart LR\n  a --> b\n  a --> b", "duplicate-edge"),
        ("flowchart LR\n  a --> b\n  style a fill:#fff,stroke-width:4px", "style-props"),
        ("flowchart LR\n  a --> b\n  linkStyle 0 stroke:#f00", "link-style"),
        ("%%{init: {'theme':'dark'}}%%\nflowchart LR\n  a --> b", "directive"),
        ("flowchart LR\n  a --> b\n  class zz foo", "unknown-style-target"),
    ],
)
def test_flowchart_warnings(text, rule):
    assert rule in rules(text, "flowchart", severity="warning")


def test_arrows_are_not_html():
    text = "flowchart LR\n  a <--> b\n  b <--- c\n  c <-.-> d\n  d <==> e\n  e x--x f\n  f o--o g"
    assert "html-tag" not in rules(text)


def test_density_uses_config():
    text = "flowchart LR\n" + "\n".join(f"  n{i} --> n{i + 1}" for i in range(6))
    assert rules(text) == set()
    assert "density" in rules(
        text, config={"plan": {"max_nodes": 5, "max_edges": 5}}, severity="warning"
    )
    assert "density" in rules(text, config={"max_nodes": 3}, severity="warning")


@pytest.mark.parametrize(
    ("mutate", "rule"),
    [
        (lambda t: t.replace("flowchart LR", "flowchart TD"), "arch-direction"),
        (lambda t: t.replace("subgraph service", "subgraph Services"), "arch-lane-id"),
        (lambda t: t + '    loose["Loose"] --> pg\n', "arch-node-outside-lane"),
        (
            lambda t: t.replace('orders -.->|"Produces"| orderQ', 'orders -->|"Produces"| orderQ'),
            "arch-dotted",
        ),
        (lambda t: t + '    web -->|"Direct"| pg\n', "arch-lane-pair"),
        (lambda t: t + '    alb -.->|"Pay"| stripe\n', "arch-lane-pair"),
        (lambda t: t + "    orderQ -.-> stripe\n", "arch-lane-pair"),
        (lambda t: t + '    auth <-.->|"x"| orderQ\n', "arch-bidir-async"),
        (lambda t: t + '    pg -->|"x"| auth\n', "arch-lane-pair"),
        (lambda t: t + "    orders --> auth\n    auth --> orders\n", "arch-cycle"),
        (lambda t: t + "    auth --> service\n", "edge-to-subgraph"),
        (lambda t: t + "    user_db --> pg\n", "underscore-id"),
    ],
)
def test_architecture_errors(mutate, rule):
    assert rule in rules(mutate(GOOD_ARCH), "architecture", severity="error")


def test_architecture_warnings():
    styled = GOOD_ARCH + "    classDef x fill:#fff\n    class auth x\n"
    assert "arch-styling" in rules(styled, "architecture", severity="warning")
    many = GOOD_ARCH + "".join(f'    orders -->|"w{i}"| pg{i}\n' for i in range(12))
    many = many.replace(
        'pg["PostgreSQL"]',
        'pg["PostgreSQL"]\n' + "".join(f'        pg{i}["DB {i}"]\n' for i in range(12)),
    )
    assert "arch-density" in rules(many, "architecture", severity="warning")
    dup = GOOD_ARCH + '    auth -->|"Again"| pg\n'
    assert "arch-duplicate-edge" in rules(dup, "architecture", severity="warning")
    lonely = GOOD_ARCH.replace(
        'notify["Notifications"]', 'notify["Notifications"]\n        idle["Idle"]'
    )
    assert "arch-service-io" in rules(lonely, "architecture", severity="warning")


def test_architecture_opposite_dotted_pair_is_allowed():
    text = GOOD_ARCH + '    orderQ -.->|"Consume"| orders\n'
    assert "arch-duplicate-edge" not in rules(text, "architecture")


GOOD_SEQ = """sequenceDiagram
    title OAuth flow
    participant User
    participant ClientApp
    participant AuthServer
    User->>ClientApp: Click Sign in
    ClientApp->>AuthServer: GET /authorize
    AuthServer-->>ClientApp: 302 with code
    ClientApp-)AuthServer: audit event
"""


def test_good_sequence_clean():
    assert lint(GOOD_SEQ, "sequence") == []


@pytest.mark.parametrize(
    ("extra", "rule"),
    [
        ("Note over User: hi", "seq-forbidden"),
        ("note right of User: hi", "seq-forbidden"),
        ("loop every minute\n    User->>ClientApp: poll\n    end", "seq-forbidden"),
        (
            "alt ok\n    User->>ClientApp: a\n    else bad\n    User->>ClientApp: b\n    end",
            "seq-forbidden",
        ),
        ("opt maybe", "seq-forbidden"),
        ("par a\n  User->>ClientApp: x\n  and b\n  User->>AuthServer: y\n  end", "seq-forbidden"),
        ("critical c", "seq-forbidden"),
        ("break b", "seq-forbidden"),
        ("rect rgb(0,0,0)", "seq-forbidden"),
        ("activate User", "seq-forbidden"),
        ("deactivate User", "seq-forbidden"),
        ("autonumber", "seq-forbidden"),
        ("link User: Dash @ https://x", "seq-forbidden"),
        ("links User: {}", "seq-forbidden"),
        ("box Group", "seq-forbidden"),
        ("User->>+ClientApp: call", "seq-activation"),
        ("ClientApp-->>-User: done", "seq-activation"),
        ('participant api as "API Service"', "seq-alias"),
        ("participant Auth Server", "seq-participant-id"),
        ("classDef x fill:#fff", "styling-unsupported"),
    ],
)
def test_sequence_errors(extra, rule):
    assert rule in rules(GOOD_SEQ + "    " + extra + "\n", "sequence", severity="error")


@pytest.mark.parametrize(
    ("extra", "rule"),
    [
        ("User->>ClientApp:", "seq-unlabeled"),
        ("User->>ClientApp: a; b", "seq-semicolon"),
        ("participant a", "seq-cryptic-id"),
        ('participant DB@{"type": "database"}', "seq-type-annotation"),
        ("this is not mermaid", "seq-unparsed"),
    ],
)
def test_sequence_warnings(extra, rule):
    assert rule in rules(GOOD_SEQ + "    " + extra + "\n", "sequence", severity="warning")


GOOD_STATE = """stateDiagram-v2
    direction LR
    state "Waiting for review" as Review
    [*] --> Draft
    Draft --> Review : submit
    Review --> Draft : reject
    state Review {
        [*] --> Pending
        Pending --> [*]
    }
    Review --> Published
    Published: Live (public)
    Published --> [*]
"""


def test_good_state_clean():
    assert lint(GOOD_STATE, "state") == []


@pytest.mark.parametrize(
    ("extra", "rule"),
    [
        ("classDef bad fill:#f00", "state-styling"),
        ("class Draft bad", "state-styling"),
        ("style Draft fill:#f00", "state-styling"),
        ("Draft:::bad --> Review", "state-styling"),
        ("note right of Draft : hi", "state-note"),
        ("[*]", "state-bare-marker"),
        ("Draft -> Review", "state-arrow"),
        ("Active.Working --> Suspended.Held", "state-cross-composite"),
        ("}", "unbalanced-brace"),
        ("state Open {", "unbalanced-brace"),
    ],
)
def test_state_errors(extra, rule):
    assert rule in rules(GOOD_STATE + "    " + extra + "\n", "state", severity="error")


def test_state_legacy_header_and_nesting_warn():
    assert "header" in rules(
        GOOD_STATE.replace("stateDiagram-v2", "stateDiagram"), "state", severity="warning"
    )
    deep = (
        GOOD_STATE
        + "    state A {\n    state B {\n    state C {\n    X --> Y\n    }\n    }\n    }\n"
    )
    assert "nesting" in rules(deep, "state", severity="warning")


GOOD_ERD = """erDiagram
    direction LR
    CUSTOMER ||--o{ ORDER : places
    ORDER ||--|{ LINE_ITEM : "contains"
    ORDER }|..|{ PROMO_CODE : applied_with
    CUSTOMER {
        string id PK
        string email UK
        string teamId FK
        string refCode PK, UK "Primary + unique alt"
    }
    PROMO_CODE["Promo Code"] {
        string code PK
    }
    LINE_ITEM
"""


def test_good_erd_clean():
    assert lint(GOOD_ERD, "erd") == []


@pytest.mark.parametrize(
    ("extra", "rule"),
    [
        ('USER["User"] ||--o{ ORDER : places', "erd-alias-in-relation"),
        ("CUSTOMER |x--o{ ORDER : places", "erd-cardinality"),
        ("CUSTOMER ||--o{ ORDER", "erd-relation-label"),
        ("note CUSTOMER", "erd-note"),
        ("end ||--o{ ORDER : x", "reserved-id"),
        ("THING {\n    not a valid attribute line here\n    }", "erd-attribute"),
        ("THING {", "unbalanced-brace"),
        ("Bad.Name", "erd-entity-name"),
    ],
)
def test_erd_errors(extra, rule):
    assert rule in rules(GOOD_ERD + "    " + extra + "\n", "erd", severity="error")


def test_erd_styling_is_warning():
    found = lint(GOOD_ERD + "    style CUSTOMER fill:#f00\n", "erd")
    assert [i["severity"] for i in found if i["rule"] == "erd-styling"] == ["warning"]


def test_unknown_renderer_and_empty():
    assert rules("flowchart LR\n a --> b", "pie") == {"renderer"}
    assert "header" in rules("", "flowchart", severity="error")
    assert "seq-empty" in rules("sequenceDiagram\n  participant A", "sequence", severity="error")


def test_issue_shape_and_order():
    issues = lint('flowchart LR\n  end --> a_b\n  x["🚀"]', "flowchart")
    assert issues == sorted(issues, key=lambda i: (i["line"], i["severity"] != "error", i["rule"]))
    for i in issues:
        assert set(i) == {"severity", "rule", "line", "message"}
        assert i["severity"] in ("error", "warning")
