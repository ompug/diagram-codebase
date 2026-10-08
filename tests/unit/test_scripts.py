"""figma/scripts.py: generated use_figma JavaScript."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
from diagram_codebase.figma import scripts

FORBIDDEN = ("figma.notify", "createPage", "closePlugin", "console.log", "(async () =>")


def all_scripts():
    return {
        "place": scripts.place_section(
            diagram_id="master",
            title='System "overview" </script>',
            run_id="r1",
            content_hash="abc",
            row=1,
            ignore_ids=["9:9", "1:2"],
            replace_section_id="5:5",
        ),
        "legend": scripts.legend_and_index(
            run_id="r1",
            categories=["data", "app", "unknown-cat"],
            diagrams=[{"id": "master", "title": "System overview", "type": "master"}],
            repo_name="shop",
            revision="abcdef1234567890",
            date="2026-10-08",
        ),
        "delete": scripts.delete_sections(["3:3", "1:1", "3:3"]),
    }


def params_of(code: str) -> dict:
    return json.loads(re.match(r"const P = (\{.*?\});\n", code).group(1))


@pytest.mark.parametrize("name", ["place", "legend", "delete"])
def test_no_forbidden_apis_and_returns(name):
    code = all_scripts()[name]
    for bad in FORBIDDEN:
        assert bad not in code, bad
    assert "\nreturn {" in code
    assert len(code.encode()) < 20_000


def test_ids_embedded_as_json_string_literals():
    code = all_scripts()["place"]
    p = params_of(code)
    assert p["diagramId"] == "master" and p["replaceSectionId"] == "5:5"
    assert p["ignoreIds"] == ["1:2", "9:9"]
    assert '"replaceSectionId": "5:5"' in code
    assert p["title"] == 'System "overview" </script>'
    assert params_of(all_scripts()["delete"])["ids"] == ["1:1", "3:3"]


def test_fonts_loaded_before_text_and_tags_written():
    code = all_scripts()["place"]
    assert code.index("await loadFontsIn(content)") < code.index("section.appendChild(n)")
    make_text = code[code.index("async function makeText") :]
    assert make_text.index("loadFontAsync") < make_text.index("t.characters")
    for key in ("diagram_id", "run_id", "content_hash", "row"):
        assert key in code
    assert '"diagramcodebase"' in code


def test_legend_content():
    p = params_of(all_scripts()["legend"])
    assert [c["key"] for c in p["categories"]] == ["app", "data"]
    assert p["categories"][0]["color"] == "A8DAFF"
    assert "abcdef1234" in p["meta"] and "abcdef12345" not in p["meta"]
    assert "Evidence: see local .diagram-codebase/model.json" in p["meta"]
    assert p["index"] == "1. System overview (master)"


def test_use_figma_params():
    p = scripts.use_figma_params("K", "return 1", "desc")
    assert p == {
        "fileKey": "K",
        "code": "return 1",
        "description": "desc",
        "skillNames": "figma-use,figma-use-figjam",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("name", ["place", "legend", "delete"])
def test_syntax_valid_with_node(tmp_path, name):
    # use_figma wraps the code in an async function; emulate that for the syntax check.
    path = tmp_path / f"{name}.js"
    path.write_text("async function __run(figma) {\n" + all_scripts()[name] + "\n}\n")
    res = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
