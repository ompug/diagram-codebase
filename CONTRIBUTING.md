# Contributing to diagram-codebase

Thanks for helping. This project turns repositories into evidence-backed
architecture diagrams, so correctness and honesty matter more than coverage:
a missing edge is better than a wrong one.

## Development setup

Requirements: Python 3.10+, git, and [uv](https://docs.astral.sh/uv/) (for `uvx ruff`).
Node.js 20+ is optional.

```sh
python3 -m pip install pytest          # or: pip install -e '.[dev]'
python3 -m pytest -q                   # all tests
uvx ruff check . && uvx ruff format --check .
uvx ruff format .                      # before committing
```

Optional, to run the real Mermaid parser checks (otherwise those tests are skipped):

```sh
cd skills/diagram-codebase/tools/mermaid-check && npm ci
```

Try the pipeline end to end without Claude or Figma:

```sh
python3 skills/diagram-codebase/scripts/dc.py run --dry-run \
  --repo tests/fixtures/a_python_app --out /tmp/dc-out
```

Runtime code under `skills/diagram-codebase/` is **standard library only**. Do not add
runtime dependencies.

## Architecture tour

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): module map and data flow.
- [docs/DESIGN.md](docs/DESIGN.md): every data contract (model, findings, plan,
  driver actions, ledger, CLI). Read it before changing code; a contract change
  updates DESIGN.md in the same change.
- [docs/PLANNER.md](docs/PLANNER.md): planner heuristics and readability limits.
- [skills/diagram-codebase/SKILL.md](skills/diagram-codebase/SKILL.md): what Claude does
  in each phase.

## Running tests

```sh
python3 -m pytest -q                              # everything
python3 -m pytest -q tests/unit/test_planner.py   # one module
python3 -m pytest -q tests/integration            # fixture pipelines + mocked driver
```

**Never call Figma in development or tests.** The publish loop is tested with the
mocked driver (`tests/integration/test_driver_mock.py`). No test may need network,
Figma, ROS or Node; Node-dependent tests must skip when it is unavailable.

## Adding an analyzer

1. Add a module under `scripts/diagram_codebase/scan/` and call it from `run_scan` in
   `scan/__init__.py`.
2. Emit everything through `ModelBuilder` (`model/builder.py`): `b.ev(...)` for evidence
   (file, lines, symbol, `source` = `ast|regex|manifest`), then `b.node(...)` /
   `b.edge(...)` referencing it. Never create a node or edge without evidence.
3. Use the node id prefixes and edge kinds/directions in DESIGN.md. New vocabulary goes
   into `model/schema.py` and DESIGN.md together.
4. Statuses: `confirmed` only for direct syntactic declarations (a route decorator, a
   `create_publisher` call). Name-based or type-inferred resolution is
   `static_inferred`. Imports never become `calls` edges.
5. Never execute or modify the analyzed repository; read files only.
6. Tests: `tests/unit/test_<module>.py` with inline snippets, plus a small fixture in
   `tests/fixtures/` and a case in `tests/integration/test_scan_fixtures.py` when the
   analyzer affects the pipeline. Fixtures are tiny and contain no real secrets.
7. Document limitations in the README "Limitations" section.

## Adding a diagram type

1. Add the type to `args.DIAGRAM_TYPES` (and aliases), `ROW` and `TIER` in
   `plan/planner.py`, and the DiagramSpec `type` list in DESIGN.md.
2. Write the planner step (see "Adding a diagram type" in
   [docs/PLANNER.md](docs/PLANNER.md)): skip with a reason instead of emitting an empty
   or trivial diagram, respect `--focus`, and list anything omitted in `notes`.
3. If it needs a new renderer, extend `mermaid/build.py` and make sure the output passes
   `mermaid/lint.py` under the official `figma-generate-diagram` rules (no class
   diagrams; Figma's importer is a subset of Mermaid).
4. Update `docs/PLANNER.md`, `references/diagram-types.md` and the README table.
5. Tests in `tests/unit/test_planner.py`, `test_mermaid_build.py` and
   `test_mermaid_lint.py`.

## Adding framework guidance

Frameworks without a deterministic analyzer are handled by Claude using
`skills/diagram-codebase/references/frameworks/<name>.md`. A guide should say what to
look for (entry points, routing, messaging, persistence, config), which node ids and
edge kinds to use, and the common traps (dynamic registration, DI containers). Add its
trigger to the table in SKILL.md Phase 3. Keep guides short; they are loaded into
Claude's context.

## Reporting bugs

Use the bug report template. Include:

- the command and options, Claude Code version, Python version, OS;
- `.diagram-codebase/validation.json` and the relevant part of `manifest.json` or
  `REPORT.md`;
- for Mermaid problems, the `mermaid/<id>.mmd` file.

**Never paste proprietary source code**, secrets or private board links. Reproduce with
a minimal synthetic repository where possible.

## Pull requests

- Follow [CODING_STANDARDS.md](CODING_STANDARDS.md); reviewers enforce it.
- Tests and lint must pass. CI runs on Python 3.10 to 3.13 and runs the Mermaid parser
  check with Node.
- Keep changes focused; update DESIGN.md with any contract change.
- Never claim a check passed that did not run, and never present ledger numbers as
  Figma's quota.
- Add an entry under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).

By contributing you agree that your contributions are licensed under the MIT License.
