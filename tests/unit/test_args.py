"""Tests for `/diagram-codebase` argument parsing."""

from __future__ import annotations

import pytest
from diagram_codebase.args import DIAGRAM_TYPES, HELP, normalize_type, parse_args


def test_defaults_for_empty_input():
    for raw in (None, "", "   "):
        r = parse_args(raw)
        assert r["depth"] == "standard"
        assert r["types"] == [] and r["focus"] is None and r["out"] is None
        assert not any(
            r[k] for k in ("dry_run", "resume", "update", "yes", "verify_visual", "help")
        )
        assert r["hint"] == "" and r["errors"] == [] and r["warnings"] == []


def test_value_flags_space_and_equals_forms():
    r = parse_args("--depth deep --focus localization --out /tmp/x")
    assert (r["depth"], r["focus"], r["out"]) == ("deep", "localization", "/tmp/x")
    r = parse_args("--depth=overview --focus=api --out=out")
    assert (r["depth"], r["focus"], r["out"]) == ("overview", "api", "out")


def test_bool_flags_and_short_aliases():
    r = parse_args("--dry-run --yes --verify-visual -h")
    assert r["dry_run"] and r["yes"] and r["verify_visual"] and r["help"]
    assert parse_args("-y")["yes"]


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("overview", "master"),
        ("arch", "master"),
        ("data-flow", "dataflow"),
        ("exec", "execution"),
        ("seq", "sequence"),
        ("algo", "algorithm"),
        ("deps", "dependency"),
        ("infra", "infrastructure"),
        ("ros", "ros2"),
        ("er", "erd"),
        ("state-machine", "state"),
        ("ERD", "erd"),
        (" Sequence ", "sequence"),
    ],
)
def test_type_aliases(alias, canonical):
    assert normalize_type(alias) == canonical


def test_canonical_types_normalize_to_themselves():
    assert all(normalize_type(t) == t for t in DIAGRAM_TYPES)
    assert normalize_type("pie") is None


def test_type_list_dedupes_and_keeps_order():
    r = parse_args("--type algo,dataflow,algorithm,,data")
    assert r["types"] == ["algorithm", "dataflow"]
    assert r["errors"] == []


def test_unknown_type_error_lists_valid_types():
    r = parse_args("--type flowchartz,erd")
    assert r["types"] == ["erd"]
    assert len(r["errors"]) == 1
    assert "flowchartz" in r["errors"][0]
    assert all(t in r["errors"][0] for t in DIAGRAM_TYPES)


def test_invalid_depth_is_an_error_and_keeps_default():
    r = parse_args("--depth extreme")
    assert r["depth"] == "standard"
    assert r["errors"] and "--depth" in r["errors"][0]


def test_missing_value_is_an_error():
    for raw in ("--focus", "--type --dry-run", "--focus -y"):
        r = parse_args(raw)
        assert any("needs a value" in e for e in r["errors"]), raw
    # The flag after the valueless option is still honored.
    assert parse_args("--type --dry-run")["dry_run"]
    assert parse_args("--focus -y")["yes"]


def test_resume_and_update_conflict():
    r = parse_args("--resume --update")
    assert any("cannot be combined" in e for e in r["errors"])


def test_unknown_option_warns_and_free_text_becomes_hint():
    r = parse_args('--depth deep focus on "the ICP loop" --frobnicate please')
    assert r["warnings"] == ["unknown option --frobnicate (ignored)"]
    assert r["hint"] == "focus on the ICP loop please"
    assert r["errors"] == []


def test_bool_flag_with_value_is_not_silently_accepted():
    r = parse_args("--dry-run=1")
    assert not r["dry_run"]
    assert r["warnings"]


def test_unbalanced_quotes_fall_back_to_whitespace_split():
    r = parse_args('--focus "backend api --dry-run')
    assert r["focus"] == '"backend'
    assert r["dry_run"]
    assert r["hint"] == "api"


def test_help_text_mentions_every_option_and_type():
    for opt in (
        "--depth",
        "--focus",
        "--type",
        "--dry-run",
        "--resume",
        "--update",
        "--yes",
        "--out",
    ):
        assert opt in HELP
    assert all(t in HELP for t in DIAGRAM_TYPES)
