# diagram-codebase

Open-source Claude Code skill: repository → evidence-backed architecture model →
Mermaid → editable FigJam diagrams via the official Figma MCP server.

- Design and all data contracts: `docs/DESIGN.md` (read before changing code)
- Rules reviewers enforce: `CODING_STANDARDS.md`
- Skill source: `skills/diagram-codebase/` (SKILL.md, scripts/, hooks/, references/)
- Official Figma diagram rules (local, authoritative for Mermaid constraints):
  `~/.claude/plugins/cache/claude-plugins-official/figma/*/skills/figma-generate-diagram/`

Commands:
- Tests: `python3 -m pytest -q`
- Lint: `uvx ruff check . && uvx ruff format --check .`
- Dry run against a fixture: `python3 skills/diagram-codebase/scripts/dc.py run --dry-run --repo tests/fixtures/a_python_app --out /tmp/dc-out`

Never spend Figma MCP calls in development or tests; use the mocked driver tests.
