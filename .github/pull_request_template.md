## Summary

<!-- What changes and why. Link the issue if there is one. -->

## Checklist

- [ ] `python3 -m pytest -q` passes
- [ ] `uvx ruff check . && uvx ruff format --check .` passes
- [ ] New or changed modules have unit tests (`tests/unit/test_<module>.py`)
- [ ] Data contract changes are reflected in `docs/DESIGN.md` in this PR
- [ ] Runtime code stays standard-library only
- [ ] No test or dev step calls Figma (mocked driver only)
- [ ] New nodes/edges carry evidence; `confirmed` only for direct syntactic declarations
- [ ] Nothing dropped from diagrams or findings without being listed in `notes` / `validation.json`
- [ ] CHANGELOG.md "Unreleased" updated
- [ ] No proprietary code, real secrets, or personal paths in fixtures or docs

## Verification

<!-- What you ran and what it showed. Say explicitly if something was not run
(for example the Node parser check or a live Figma run). -->
