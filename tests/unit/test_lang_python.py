"""Tests for Python structural analysis and conservative call resolution."""

from __future__ import annotations

from diagram_codebase.model.builder import ModelBuilder
from diagram_codebase.scan.lang_python import analyze_python, literal, module_name_for


def analyze(make_repo, files):
    root = make_repo(files)
    inv = [
        {"path": p, "language": "python", "analyzed": True}
        for p in sorted(files)
        if p.endswith(".py")
    ]
    b = ModelBuilder()
    idx = analyze_python(root, inv, b)
    return b, idx


def edge(b, kind, src, dst):
    return b.edges.get(f"e:{kind}:{src}>{dst}")


def status(b, e):
    return {b.evidence[i]["status"] for i in e["evidence_ids"]}


def calls(b):
    return {(e["from"], e["to"]) for e in b.edges.values() if e["kind"] == "calls"}


PKG = {
    "pkg/__init__.py": "",
    "pkg/models.py": '"""Models."""\n\nclass Base:\n    def save(self):\n        return 1\n\n\nclass Item(Base):\n    """An item.\n\n    Long text."""\n\n    def label(self) -> str:\n        return "x"\n',
    "pkg/repo.py": "from .models import Item\n\n\nclass Repo:\n    def get(self) -> Item:\n        return Item()\n\n\ndef make_repo() -> Repo:\n    return Repo()\n",
    "pkg/sub/__init__.py": "",
    "pkg/sub/svc.py": (
        "from ..repo import Repo, make_repo\n"
        "from .. import models\n"
        "import pkg.models as pm\n"
        "import json\n"
        "\n"
        "\n"
        "class Service:\n"
        "    def __init__(self, repo: Repo, other) -> None:\n"
        "        self.repo = repo\n"
        "        self.other = other\n"
        "        self.helper = make_repo()\n"
        "        self.setup()\n"
        "\n"
        "    def setup(self):\n"
        "        return json.dumps({})\n"
        "\n"
        "    def run(self):\n"
        "        item = self.repo.get()\n"
        "        self.helper.get()\n"
        "        self.other.get()\n"
        "        item.label()\n"
        "        item.save()\n"
        "        pm.Item().label()\n"
        "        models.Item()\n"
        "        super().run()\n"
        "\n"
        "\n"
        "async def main():\n"
        "    r = make_repo()\n"
        "    r.get()\n"
        "    s = Service(r, None)\n"
        "    s.run()\n"
    ),
}


def test_definitions_and_metadata(make_repo):
    b, idx = analyze(make_repo, PKG)
    item = b.nodes["cls:pkg/models.py:Item"]
    assert item["parent"] == "mod:pkg/models.py"
    assert item["description"] == "An item."
    label = b.nodes["fn:pkg/models.py:Item.label"]
    assert label["parent"] == "cls:pkg/models.py:Item" and label["outputs"] == ["str"]
    assert "async" in b.nodes["fn:pkg/sub/svc.py:main"]["tags"]
    assert b.nodes["fn:pkg/sub/svc.py:Service.__init__"]["inputs"] == ["repo", "other"]
    assert b.nodes["mod:pkg/models.py"]["description"] == "Models."
    assert idx.by_module["pkg.sub.svc"].path == "pkg/sub/svc.py"
    assert edge(b, "inherits", "cls:pkg/models.py:Item", "cls:pkg/models.py:Base")


def test_imports_absolute_relative_and_external(make_repo):
    b, idx = analyze(make_repo, PKG)
    assert edge(b, "imports", "mod:pkg/repo.py", "mod:pkg/models.py")
    assert edge(b, "imports", "mod:pkg/sub/svc.py", "mod:pkg/repo.py")  # from ..repo
    assert edge(b, "imports", "mod:pkg/sub/svc.py", "mod:pkg/models.py")  # from .. import models
    assert idx.files["pkg/sub/svc.py"].external_imports == {"json"}


def test_imports_are_never_calls(make_repo):
    b, _ = analyze(
        make_repo,
        {
            "a.py": "from b import helper, Thing\n\n\ndef f():\n    return helper\n",
            "b.py": "def helper():\n    pass\n\n\nclass Thing:\n    pass\n",
        },
    )
    assert edge(b, "imports", "mod:a.py", "mod:b.py")
    assert not [e for e in b.edges.values() if e["kind"] in ("calls", "instantiates")]


def test_call_resolution_statuses(make_repo):
    b, _ = analyze(make_repo, PKG)
    svc = "fn:pkg/sub/svc.py:Service"
    # self.method -> confirmed
    e = edge(b, "calls", f"{svc}.__init__", f"{svc}.setup")
    assert e and status(b, e) == {"confirmed"} and e["phase"] == "init"
    # self.attr.method via parameter annotation -> static_inferred
    e = edge(b, "calls", f"{svc}.run", "fn:pkg/repo.py:Repo.get")
    assert e and status(b, e) == {"static_inferred"}
    # factory return annotation: r = make_repo() -> Repo.get
    e = edge(b, "calls", "fn:pkg/sub/svc.py:main", "fn:pkg/repo.py:Repo.get")
    assert e and status(b, e) == {"static_inferred"}
    # constructor -> instantiates, then method on the instance
    assert edge(b, "instantiates", "fn:pkg/sub/svc.py:main", "cls:pkg/sub/svc.py:Service")
    assert edge(b, "calls", "fn:pkg/sub/svc.py:main", f"{svc}.run")
    # imported function -> confirmed
    e = edge(b, "calls", f"{svc}.__init__", "fn:pkg/repo.py:make_repo")
    assert e and status(b, e) == {"confirmed"}
    # module alias import: models.Item() -> instantiates
    assert edge(b, "instantiates", f"{svc}.run", "cls:pkg/models.py:Item")


def test_unresolvable_calls_produce_no_edges(make_repo):
    b, _ = analyze(make_repo, PKG)
    run_targets = {dst for src, dst in calls(b) if src == "fn:pkg/sub/svc.py:Service.run"}
    # self.other is untyped; chained pm.Item().label() and super().run() are unknown;
    # `item` comes from an unannotated call result.
    assert run_targets == {"fn:pkg/repo.py:Repo.get"}


def test_self_attr_from_factory_annotation(make_repo):
    b, _ = analyze(
        make_repo,
        {
            "f.py": (
                "class Engine:\n"
                "    def start(self):\n"
                "        pass\n"
                "\n"
                "\n"
                "def build() -> 'Engine':\n"
                "    return Engine()\n"
                "\n"
                "\n"
                "class Car:\n"
                "    engine: Engine\n"
                "\n"
                "    def __init__(self):\n"
                "        self.spare = build()\n"
                "\n"
                "    def go(self):\n"
                "        self.engine.start()\n"
                "        self.spare.start()\n"
            )
        },
    )
    assert edge(b, "calls", "fn:f.py:Car.go", "fn:f.py:Engine.start")
    assert b.nodes["fn:f.py:build"]["outputs"] == ["'Engine'"]


def test_module_level_instance_imported_and_shadowed(make_repo):
    b, _ = analyze(
        make_repo,
        {
            "bus.py": "class Bus:\n    def send(self, x):\n        pass\n\n\nbus = Bus()\n",
            "use.py": (
                "from bus import bus\n"
                "\n"
                "\n"
                "def ok():\n"
                "    bus.send(1)\n"
                "\n"
                "\n"
                "def shadowed(bus):\n"
                "    bus.send(2)\n"
                "\n"
                "\n"
                "def rebound():\n"
                "    bus = object()\n"
                "    bus.send(3)\n"
            ),
        },
    )
    e = edge(b, "calls", "fn:use.py:ok", "fn:bus.py:Bus.send")
    assert e and status(b, e) == {"static_inferred"}
    assert not edge(b, "calls", "fn:use.py:shadowed", "fn:bus.py:Bus.send")
    assert not edge(b, "calls", "fn:use.py:rebound", "fn:bus.py:Bus.send")
    assert edge(b, "instantiates", "mod:bus.py", "cls:bus.py:Bus")["phase"] == "init"


def test_class_body_names_are_not_module_instances(make_repo):
    _, idx = analyze(
        make_repo,
        {"m.py": "class Cfg:\n    x = dict()\n\n\ny = list()\n"},
    )
    assert idx.files["m.py"].module_types == {"y": "list"}


def test_main_block_tags_module_entrypoint(make_repo):
    b, idx = analyze(
        make_repo,
        {"tool.py": "def main():\n    pass\n\n\nif __name__ == '__main__':\n    main()\n"},
    )
    assert "entrypoint" in b.nodes["mod:tool.py"]["tags"]
    assert idx.files["tool.py"].main_block == (5, 6)
    assert edge(b, "calls", "mod:tool.py", "fn:tool.py:main")


def test_syntax_error_is_recorded_not_raised(make_repo):
    b, idx = analyze(make_repo, {"bad.py": "def broken(:\n    pass\n", "ok.py": "x = 1\n"})
    assert idx.files["bad.py"].error.startswith("SyntaxError")
    assert b.nodes["mod:bad.py"]["metadata"]["parse_error"].startswith("SyntaxError")
    assert "mod:ok.py" in b.nodes


def test_env_reads_and_strings_recorded(make_repo):
    _, idx = analyze(
        make_repo,
        {"c.py": "import os\n\n\ndef f():\n    a = os.environ['API_URL']\n    b = 'hello world'\n"},
    )
    pf = idx.files["c.py"]
    assert pf.env_reads == [("API_URL", 5, "fn:c.py:f")]
    assert ("hello world", 6, "fn:c.py:f") in pf.strings
    assert ("b", "hello world", 6, "fn:c.py:f") in pf.str_assigns


def test_resolve_callable_variants(make_repo):
    b, idx = analyze(
        make_repo,
        {
            "cb.py": (
                "from functools import partial\n"
                "\n"
                "\n"
                "def handler(x):\n"
                "    pass\n"
                "\n"
                "\n"
                "class W:\n"
                "    def on(self):\n"
                "        pass\n"
            )
        },
    )
    pf = idx.files["cb.py"]
    assert idx.resolve_callable(pf, {"name": "handler"}, None) == "fn:cb.py:handler"
    assert idx.resolve_callable(pf, {"name": "self.on"}, "W") == "fn:cb.py:W.on"
    part = {"call": "partial", "args": [{"name": "handler"}, 1], "kwargs": {}}
    assert idx.resolve_callable(pf, part, None) == "fn:cb.py:handler"
    assert idx.resolve_callable(pf, {"name": "w.on"}, None, {"w": "W"}) == "fn:cb.py:W.on"
    assert idx.resolve_callable(pf, {"name": "W"}, None) is None  # classes are not callables here
    assert idx.resolve_class(pf, "W") == "cls:cb.py:W"
    assert idx.resolve_callable(pf, "handler", None) is None


def test_module_name_for(make_repo):
    root = make_repo(
        {"a/__init__.py": "", "a/b/__init__.py": "", "a/b/c.py": "", "scripts/run.py": ""}
    )
    assert module_name_for("a/b/c.py", root) == "a.b.c"
    assert module_name_for("a/b/__init__.py", root) == "a.b"
    assert module_name_for("scripts/run.py", root) == "run"


def test_literal_rendering():
    import ast

    def lit(src):
        return literal(ast.parse(src, mode="eval").body)

    assert lit("'x'") == "x" and lit("3") == 3 and lit("None") is None
    assert lit("f'/a/{x}/b'") == "/a/{}/b"
    assert lit("self.cb") == {"name": "self.cb"}
    assert lit("[('a', 'b')]") == [["a", "b"]]
    assert lit("{'k': 1, **rest}") == {"dict": {"k": 1}}
    assert lit("f(1, k=2)") == {"call": "f", "args": [1], "kwargs": {"k": 2}}
    assert lit("b'bytes'") is None
