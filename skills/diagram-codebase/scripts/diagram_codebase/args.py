"""Parse the raw `/diagram-codebase ...` argument string.

Claude Code hands a skill its arguments as one string ($ARGUMENTS). We parse
flags deterministically and pass any leftover free text to Claude as a hint.
"""

from __future__ import annotations

import shlex
from typing import Any

DEPTHS = ("overview", "standard", "deep")

DIAGRAM_TYPES = (
    "master",
    "subsystem",
    "dataflow",
    "execution",
    "sequence",
    "algorithm",
    "dependency",
    "infrastructure",
    "ros2",
    "erd",
    "state",
)

TYPE_ALIASES = {
    "overview": "master",
    "architecture": "master",
    "arch": "master",
    "subsystems": "subsystem",
    "data-flow": "dataflow",
    "data": "dataflow",
    "flow": "execution",
    "exec": "execution",
    "runtime": "execution",
    "seq": "sequence",
    "algo": "algorithm",
    "algorithms": "algorithm",
    "deps": "dependency",
    "dependencies": "dependency",
    "infra": "infrastructure",
    "deployment": "infrastructure",
    "ros": "ros2",
    "er": "erd",
    "entities": "erd",
    "states": "state",
    "state-machine": "state",
}

HELP = """\
/diagram-codebase [options] [free-text hint]

Analyze the current repository and publish editable FigJam architecture diagrams.

Options:
  --depth overview|standard|deep   How much to analyze and draw (default: standard)
  --focus <subsystem>              Concentrate on one subsystem (keeps 1-hop context)
  --type <t>[,<t>...]              Only these diagram types:
                                   master, subsystem, dataflow, execution, sequence,
                                   algorithm, dependency, infrastructure, ros2, erd, state
  --dry-run                        Analyze and write diagram specs locally; no Figma calls
  --resume                         Continue an interrupted or budget-paused run
  --update                         Re-analyze what changed since the last run and
                                   regenerate only affected diagrams
  --yes                            Skip the one confirmation before publishing to Figma
  --verify-visual                  Also take screenshots during verification (costs calls)
  --out <dir>                      Output directory (default: <repo>/.diagram-codebase)
  --help                           Show this help

Examples:
  /diagram-codebase
  /diagram-codebase --depth deep
  /diagram-codebase --focus localization --type dataflow,algorithm
  /diagram-codebase --dry-run
"""

_BOOL_FLAGS = {
    "--dry-run": "dry_run",
    "--resume": "resume",
    "--update": "update",
    "--yes": "yes",
    "-y": "yes",
    "--verify-visual": "verify_visual",
    "--help": "help",
    "-h": "help",
}
_VALUE_FLAGS = {"--depth": "depth", "--focus": "focus", "--type": "types", "--out": "out"}


def _split(raw: str) -> list[str]:
    try:
        return shlex.split(raw)
    except ValueError:
        # Unbalanced quotes: fall back to whitespace splitting rather than failing.
        return raw.split()


def normalize_type(name: str) -> str | None:
    key = name.strip().lower()
    key = TYPE_ALIASES.get(key, key)
    return key if key in DIAGRAM_TYPES else None


def parse_args(raw: str | None) -> dict[str, Any]:
    tokens = _split(raw or "")
    result: dict[str, Any] = {
        "depth": "standard",
        "focus": None,
        "types": [],
        "out": None,
        "dry_run": False,
        "resume": False,
        "update": False,
        "yes": False,
        "verify_visual": False,
        "help": False,
        "hint": "",
        "errors": [],
        "warnings": [],
    }
    hint: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        flag, eq, inline = tok.partition("=")
        if flag in _BOOL_FLAGS and not eq:
            result[_BOOL_FLAGS[flag]] = True
        elif flag in _VALUE_FLAGS:
            if eq:
                value = inline
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 1
                value = tokens[i]
            else:
                result["errors"].append(f"{flag} needs a value")
                i += 1
                continue
            key = _VALUE_FLAGS[flag]
            if key == "types":
                for part in value.split(","):
                    if not part.strip():
                        continue
                    t = normalize_type(part)
                    if t is None:
                        result["errors"].append(
                            f"unknown diagram type '{part}' (valid: {', '.join(DIAGRAM_TYPES)})"
                        )
                    elif t not in result["types"]:
                        result["types"].append(t)
            elif key == "depth":
                v = value.lower()
                if v not in DEPTHS:
                    result["errors"].append(f"--depth must be one of {', '.join(DEPTHS)}")
                else:
                    result["depth"] = v
            else:
                result[key] = value
        elif tok.startswith("--"):
            result["warnings"].append(f"unknown option {tok} (ignored)")
        else:
            hint.append(tok)
        i += 1
    if result["resume"] and result["update"]:
        result["errors"].append("--resume and --update cannot be combined")
    result["hint"] = " ".join(hint)
    return result
