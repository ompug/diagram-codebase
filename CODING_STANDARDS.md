# Coding standards

Reviewers enforce these. Deviations are fixed before new feature work.

1. **Contracts first.** `docs/DESIGN.md` defines module ownership and every data
   contract (model, findings, plan, driver actions, ledger). Change it in the same
   commit as any contract change.
2. **Standard library only** in `skills/diagram-codebase/`. Python ≥ 3.10 syntax
   (`from __future__ import annotations`). No new runtime dependencies.
3. **Never execute or modify the analyzed repository.** Read files, run read-only
   `git` plumbing only. Write only inside the output directory or the cache dir.
4. **Evidence or nothing.** Code that creates nodes/edges must attach evidence
   (file + lines). Scanners emit `confirmed` only for direct syntactic declarations;
   name-based or type-inferred resolution is `static_inferred`. Imports never become calls.
5. **Atomic state.** All JSON state goes through `common.write_json` / `write_text`.
6. **Honest reporting.** Never print a number as Figma's quota. Never mark a
   verification as passed if it did not run. Unknown stays "unknown".
7. **No silent truncation.** Anything dropped from a diagram is listed in the
   spec's `notes`; anything rejected from findings is listed in `validation.json`.
8. **Deterministic output.** Sort collections before writing; ids derive from
   stable inputs (paths, names), never from time or randomness (except run ids).
9. **Errors.** User-facing failures raise `common.DCError` with an actionable
   message; the CLI prints it without a traceback.
10. **Tests.** Every module gets unit tests in `tests/unit/test_<module>.py`;
    pipeline behavior gets integration tests against fixtures. No network, Figma,
    ROS, or Node required (skip Node checks when absent).
11. **Lint.** `uvx ruff check . && uvx ruff format --check .` must pass
    (config in `pyproject.toml`). Run `uvx ruff format .` before committing.
12. **Style.** Match surrounding code: small functions, docstrings on modules and
    non-obvious functions, comments only where intent is not obvious.
