"""Configuration defaults and loading.

Users override any key in `<out>/config.json` (deep-merged over DEFAULTS).
Budgets are this skill's own operating limits, deliberately below Figma's
documented limits; they are not a statement of how Figma enforces quota.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .common import read_json

DEFAULTS: dict[str, Any] = {
    "budget": {
        # Rolling windows. Figma documents 10/min and 200/day for Professional
        # Dev/Full seats (Education uses the same limits); we stay below that.
        "per_minute": 8,
        "per_day": 160,
        "minute_window_seconds": 60,
        "day_window_seconds": 86400,
        # Tools documented as exempt from Figma MCP rate limits. Everything else
        # is counted, including write tools whose exemption is not explicit.
        "exempt_tools": [
            "whoami",
            "create_new_file",
            "add_code_connect_map",
            "authenticate",
            "complete_authentication",
        ],
        "max_hook_sleep_seconds": 65,
        "backoff_base_seconds": 30,
        "backoff_max_seconds": 900,
    },
    "scan": {
        "exclude": [],  # extra glob patterns, matched against repo-relative posix paths
        "max_file_bytes": 1_000_000,
        "include_tests": False,
    },
    "plan": {
        "max_nodes": 25,
        "max_edges": 30,
        "master_target_min": 12,
        "master_target_max": 20,
        "max_nesting": 2,
        "architecture_max_edges": 20,
        "caps": {"overview": 2, "standard": 10, "deep": 25},
        "max_subsystem_diagrams": {"overview": 0, "standard": 5, "deep": 12},
    },
    "analysis": {
        # Above this many source files, the skill fans out Explore subagents.
        "parallel_file_threshold": 150,
    },
    "update": {
        "full_reanalysis_churn": 0.4,
    },
    "figma": {
        "max_attempts_per_diagram": 2,
        "section_gap": 200,
        "plugin_data_namespace": "diagramcodebase",
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(out_dir: Path | None) -> dict[str, Any]:
    if out_dir is None:
        return copy.deepcopy(DEFAULTS)
    user = read_json(Path(out_dir) / "config.json", default={})
    return deep_merge(DEFAULTS, user if isinstance(user, dict) else {})
