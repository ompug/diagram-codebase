# mermaid-check

Optional validator that runs the real Mermaid parser (`mermaid.parse`, under
jsdom) over the Mermaid text `dc.py plan` generates. Figma's importer is a
subset of Mermaid, so passing this check is necessary but not sufficient; the
Figma-specific rules are enforced by `scripts/diagram_codebase/mermaid/lint.py`.

Nothing here is needed at runtime. When Node or the dependencies are missing,
`parsecheck.available()` returns `False` and the check is reported as
"unavailable" (never as passed). Tests that need it are skipped.

## Install

```sh
cd skills/diagram-codebase/tools/mermaid-check
npm install            # exact versions pinned in package.json (Node >= 20)
```

`node_modules/` is git-ignored. To use an install elsewhere, point
`DIAGRAM_CODEBASE_MERMAID_CHECK` at a directory containing `check.mjs` and its
`node_modules/`.

## Protocol

```sh
echo '[{"id":"a","text":"flowchart LR\n  x --> y"}]' | node check.mjs
# {"a":null}
```

stdin is a JSON array of `{"id", "text"}`; stdout is a JSON object mapping each
id to `null` (parsed) or the first lines of the parse error. Mermaid's own
logging goes to stderr. Exit status 2 means the input was not a JSON array.
