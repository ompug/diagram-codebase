"""diagram-codebase: deterministic helpers for the /diagram-codebase Claude Code skill.

The package is standard-library only so the skill can be installed by copying a
directory. Claude does the interpretive analysis; this package does everything
that can be done deterministically: scanning, validation, planning, Mermaid
generation, sanitizing, and driving the Figma MCP call sequence.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = "1.0"
