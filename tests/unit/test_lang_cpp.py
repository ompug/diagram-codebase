"""Tests for regex-based C/C++ analysis."""

from __future__ import annotations

from diagram_codebase.model.builder import ModelBuilder
from diagram_codebase.scan.cpp_text import line_of, match_brace, strip_comments
from diagram_codebase.scan.inventory import LANGUAGES
from diagram_codebase.scan.lang_cpp import analyze_cpp


def run(make_repo, files):
    root = make_repo(files)
    inv = []
    for p in sorted(files):
        lang = LANGUAGES.get("." + p.rsplit(".", 1)[-1])
        inv.append({"path": p, "language": lang, "analyzed": lang in ("c", "cpp")})
    b = ModelBuilder()
    return b, analyze_cpp(root, inv, b)


def edge(b, kind, src, dst):
    return b.edges.get(f"e:{kind}:{src}>{dst}")


FILES = {
    "include/geom/vec.hpp": (
        "#pragma once\n"
        "namespace geom {\n"
        "struct Vec { double x, y; };\n"
        "double dot(const Vec& a, const Vec& b);\n"
        "}\n"
    ),
    "src/vec.cpp": (
        '#include "geom/vec.hpp"\n'
        "#include <cmath>\n"
        "double dot(const Vec& a, const Vec& b) { return a.x * b.x + a.y * b.y; }\n"
        "double max(double a, double b) { return a > b ? a : b; }\n"
    ),
    "src/shape.hpp": (
        "class Shape {\n"
        " public:\n"
        "  virtual double area() const = 0;\n"
        "};\n"
        "class Circle final : public Shape, private Base<int> {\n"
        " public:\n"
        "  double area() const override { return 3.14 * r_ * r_; }\n"
        "  double scale(double k) { return helper(k) * area(); }\n"
        "  double helper(double k) { return k; }\n"
        " private:\n"
        "  double r_ = 1;\n"
        "};\n"
    ),
    "src/main.cpp": (
        "// entry point: dot( in a comment must not count\n"
        '#include "shape.hpp"\n'
        '#include "../include/geom/vec.hpp"\n'
        "#include <vector>\n"
        "\n"
        "static int run(Circle& c) {\n"
        '  const char* s = "dot(fake)";\n'
        "  double m = std::max(1.0, 2.0);\n"
        "  if (c.scale(2.0) > 1) { return 1; }\n"
        "  helper(3);\n"
        "  return static_cast<int>(dot({1, 2}, {3, 4}));\n"
        "}\n"
        "\n"
        "int main(int argc, char** argv) {\n"
        "  Circle c;\n"
        "  return run(c);\n"
        "}\n"
    ),
}


def test_modules_includes_and_system_includes(make_repo):
    b, info = run(make_repo, FILES)
    assert {n for n in b.nodes if n.startswith("mod:")} == {f"mod:{p}" for p in FILES}
    assert edge(b, "imports", "mod:src/vec.cpp", "mod:include/geom/vec.hpp")  # unique suffix
    assert edge(b, "imports", "mod:src/main.cpp", "mod:src/shape.hpp")
    assert edge(b, "imports", "mod:src/main.cpp", "mod:include/geom/vec.hpp")  # relative ../
    assert edge(b, "imports", "mod:src/main.cpp", "mod:include/geom/vec.hpp")["phase"] == "build"
    assert info["system_includes"] == ["cmath", "vector"]


def test_classes_bases_methods_and_main(make_repo):
    b, _ = run(make_repo, FILES)
    circle = b.nodes["cls:src/shape.hpp:Circle"]
    assert circle["metadata"]["bases"] == ["Shape", "Base<int>"]
    assert edge(b, "inherits", "cls:src/shape.hpp:Circle", "cls:src/shape.hpp:Shape")
    assert b.nodes["fn:src/shape.hpp:Circle.area"]["parent"] == "cls:src/shape.hpp:Circle"
    assert "fn:src/shape.hpp:Shape.area" not in b.nodes  # pure virtual declaration, no body
    assert "entrypoint" in b.nodes["fn:src/main.cpp:main"]["tags"]
    assert "entrypoint" in b.nodes["mod:src/main.cpp"]["tags"]
    assert "cls:include/geom/vec.hpp:Vec" in b.nodes
    assert not any(n.endswith((":if", ":static_cast")) for n in b.nodes)


def test_calls_unique_names_are_static_inferred(make_repo):
    b, _ = run(make_repo, FILES)
    e = edge(b, "calls", "fn:src/main.cpp:main", "fn:src/main.cpp:run")
    assert e and {b.evidence[i]["status"] for i in e["evidence_ids"]} == {"static_inferred"}
    assert b.evidence[e["evidence_ids"][0]]["line_start"] == 16
    assert edge(b, "calls", "fn:src/main.cpp:run", "fn:src/shape.hpp:Circle.scale")  # obj.method
    assert edge(b, "calls", "fn:src/main.cpp:run", "fn:src/vec.cpp:dot")
    assert edge(b, "calls", "fn:src/shape.hpp:Circle.scale", "fn:src/shape.hpp:Circle.helper")


def test_calls_rejected_when_shape_does_not_fit(make_repo):
    b, _ = run(make_repo, FILES)
    run_calls = {
        e["to"]
        for e in b.edges.values()
        if e["kind"] == "calls" and e["from"] == "fn:src/main.cpp:run"
    }
    # std::max is not the repository's max(); a bare helper() outside Circle is not Circle::helper;
    # strings/comments mentioning dot( are ignored (only the real call line counts).
    assert "fn:src/vec.cpp:max" not in run_calls
    assert "fn:src/shape.hpp:Circle.helper" not in run_calls
    dot = edge(b, "calls", "fn:src/main.cpp:run", "fn:src/vec.cpp:dot")
    assert [b.evidence[i]["line_start"] for i in dot["evidence_ids"]] == [11]


def test_ambiguous_names_are_not_resolved(make_repo):
    b, _ = run(
        make_repo,
        {
            "a.cpp": "int init() { return 1; }\nint go() { return init(); }\n",
            "b.cpp": "int init() { return 2; }\n",
        },
    )
    assert not [e for e in b.edges.values() if e["kind"] == "calls"]


def test_cpp_text_helpers():
    src = 'int a; // c "x"\n/* b\n c */ s = "/*not*/"; t = \'{\';\n'
    out = strip_comments(src, keep_strings=True)
    assert len(out) == len(src) and out.count("\n") == src.count("\n")
    assert "// c" not in out and "b\n c" not in out and '"/*not*/"' in out
    blank = strip_comments(src, keep_strings=False)
    assert '"/*not*/"' not in blank and "{" not in blank
    js = strip_comments("const s = `a // b`; // gone\n", js=True)
    assert "a // b" in js and "gone" not in js
    assert line_of("a\nb\nc", 4) == 3
    text = "f() { if (x) { y(); } }"
    assert match_brace(text, 4) == len(text) - 1
    assert match_brace("{ {", 0) == 2
