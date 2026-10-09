#!/usr/bin/env python3
"""CLI entry point for the diagram-codebase skill: `python3 dc.py <command>`.

Thin wrapper: puts this directory on sys.path and runs diagram_codebase.cli.main.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diagram_codebase.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
