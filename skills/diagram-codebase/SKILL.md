---
name: diagram-codebase
description: Analyze the current repository into an evidence-backed architecture model (execution, data flow, algorithms, infrastructure) and publish editable FigJam diagrams through the official Figma MCP server. Run by hand with /diagram-codebase; supports dry runs, resuming a paused run, and incremental updates after code changes.
argument-hint: "[--depth overview|standard|deep] [--focus X] [--type T] [--dry-run] [--resume] [--update] [--yes] [--help]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/dc.py *)
  - Read
  - Grep
  - Glob
hooks:
  PreToolUse:
    - matcher: "mcp__.*figma.*"
      hooks:
        - type: command
          command: 'for f in "${CLAUDE_SKILL_DIR}/hooks/figma_gate.py" "${XDG_CACHE_HOME:-$HOME/.cache}/diagram-codebase/figma_gate.py"; do [ -f "$f" ] && exec python3 "$f" pre; done; exit 0'
          timeout: 120
  PostToolUse:
    - matcher: "mcp__.*figma.*"
      hooks:
        - type: command
          command: 'for f in "${CLAUDE_SKILL_DIR}/hooks/figma_gate.py" "${XDG_CACHE_HOME:-$HOME/.cache}/diagram-codebase/figma_gate.py"; do [ -f "$f" ] && exec python3 "$f" post; done; exit 0'
          timeout: 120
  PostToolUseFailure:
    - matcher: "mcp__.*figma.*"
      hooks:
        - type: command
          command: 'for f in "${CLAUDE_SKILL_DIR}/hooks/figma_gate.py" "${XDG_CACHE_HOME:-$HOME/.cache}/diagram-codebase/figma_gate.py"; do [ -f "$f" ] && exec python3 "$f" post_failure; done; exit 0'
          timeout: 120
---

# diagram-codebase

Turn this repository into an **evidence**-backed architecture model, then into
editable FigJam diagrams. You supply meaning (tracing execution, data flow,
algorithms); `dc.py` supplies everything checkable (inventory, evidence checks,
planning, Mermaid, budgets, run state). You never decide what goes to Figma: the
driver does.

`DC` below means `python3 ${CLAUDE_SKILL_DIR}/scripts/dc.py`. Every command prints
JSON (or Markdown where noted) and exits non-zero with a one-line message on
failure; show that message to the user and stop unless the step says otherwise.
When the parsed args carry `out`, pass `--out <out>` to every `DC` command.

Report progress in one short line at each phase boundary ("Phase 2/8: scanned
412 files, 6 subsystems"), not between individual calls.

## Phase 0: Parse arguments

Pass the raw arguments on stdin through a quoted heredoc (safe for any quotes):

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/dc.py args <<'DC_ARGS'
$ARGUMENTS
DC_ARGS
```
- `help: true` → print the usage text it returns verbatim and stop.
- `errors` non-empty → show them with the usage text and stop.
- `warnings` → mention them once and continue.
- `hint` (free text) → treat it as the user's focus question during analysis.

## Phase 1: Init

Run `DC init --args-json '<the args JSON>'` (escape any single quote in it as
`'\''`). It reports `mode` (`fresh | resume | update`), the output directory, and
the next phase. On `resume`, jump straight to the reported phase and skip every
completed one; findings files that already pass `check-findings` are kept.
On `update`, go to [Update mode](#update-mode).

## Phase 2: Scan

Run `DC scan` and read the brief: languages, `frameworks`, subsystems, entry
points, hubs, `source_files`, `use_parallel_agents`. Scanned facts become the
model's skeleton; your analysis adds what syntax alone cannot show.

## Phase 3: Analysis

Read [references/analysis-guide.md](references/analysis-guide.md) now, plus each
framework guide whose trigger matches the brief (read only those):

| Brief shows | Read |
|---|---|
| language `python` | [frameworks/python.md](references/frameworks/python.md) |
| language `javascript`/`typescript` | [frameworks/node.md](references/frameworks/node.md) |
| any web framework (fastapi, flask, django, express, nextjs, react, vue, ...) | [frameworks/web.md](references/frameworks/web.md) |
| language `java`/`kotlin` or framework `spring` | [frameworks/java-spring.md](references/frameworks/java-spring.md) |
| language `cpp`/`c` | [frameworks/cpp.md](references/frameworks/cpp.md) |
| framework `ros2` | [frameworks/ros2.md](references/frameworks/ros2.md) |
| pytorch, tensorflow, jax, sklearn | [frameworks/ml.md](references/frameworks/ml.md) |
| celery, kafka, rabbitmq, mqtt, redis, websocket, or any queue/bus | [frameworks/events.md](references/frameworks/events.md) |

Run `DC findings-template` for the exact JSON shape; the full contract is in
[references/findings-format.md](references/findings-format.md).

**Choose the fan-out.** If `use_parallel_agents` is true or `--depth deep`, launch
Explore subagents in a single message, one per area: `execution`, `dataflow`,
`algorithms`, `infrastructure` (build, deployment, config), plus one per large
subsystem (`sub-<slug>`). Build each prompt from
[references/explore-agent-brief.md](references/explore-agent-brief.md). Subagents
are read-only and **return** findings JSON in their final message. Otherwise do
the analysis yourself in this session, area by area. With `--focus X`, analyze
subsystem X deeply and its direct neighbours only enough to label the edges.

**Write and check.** You alone write `<out>/findings/<area>.json` (never
`scan.json`, which belongs to the scanner). For each file run
`DC check-findings <file>`. For each rejection, re-open the cited code and either
correct `file`/`lines`/`symbol` to what the code actually says, or delete the
claim. A claim you cannot point at does not go in; record the gap in
`uncertainties` instead.

Phase 3 is done when every findings file passes `check-findings` with zero
rejections, and each area's trace reached every entry point listed in the brief
or names the unreached ones in `uncertainties`.

## Phase 4: Merge

Run `DC merge`. Note node/edge counts, rejected items, and warnings for the final
report. If rejections appear that `check-findings` did not show, fix them the
same way and merge again.

## Phase 5: Plan and review

Run `DC plan`, then `DC review` and show its Markdown to the user: the diagrams,
what each answers, and the estimated Figma calls. If `plan` reports Mermaid lint
or sanitize problems it could not fix, list them.

With `--dry-run`, stop here. Tell the user where the artifacts are
(`model.json`, `plan.json`, `mermaid/*.mmd`, `publish/review.md`) and that no
Figma calls were made.

## Phase 6: Confirm

Unless `--yes` was given, ask once: publish these diagrams to Figma? Point to
`<out>/publish/review.md` as the exact list of what will leave the machine. On
approval run `DC confirm`. On refusal, stop and give the `--resume` command.

## Phase 7: Publish loop

Read [references/figma-workflow.md](references/figma-workflow.md) before the first
iteration. Then loop:

1. `DC next` → one action JSON.
2. By `kind`:
   - `generate_diagram`, `use_figma`, `get_figjam`, `get_screenshot`: load the
     official skill the action names, once per session, before its first call
     (`figma:figma-generate-diagram` for `generate_diagram`; `figma:figma-use`
     and `figma:figma-use-figjam` for `use_figma`). Call the Figma MCP tool named
     in `tool` with exactly `params`. Write the raw result to
     `<out>/publish/results/<action_id>.json`, then run
     `DC record --action <action_id> --result-file <that file>`. If the tool
     call errors, run `DC record --action <action_id> --error "<one-line error>"`.
   - `confirm`: handle as Phase 6, then continue the loop.
   - `ask_user`: ask the question in `explain`; save the answer as
     `{"answer": "<reply>"}` to the result file and `record` it.
   - `stop`: end the loop. Explain why (`explain`: budget, authentication,
     backoff, repeated failure) and tell the user the exact resume command,
     `/diagram-codebase --resume`. For auth stops see
     [references/troubleshooting.md](references/troubleshooting.md).
   - `done`: go to Phase 8.
3. As soon as `record` reports the FigJam file URL (after the first diagram),
   share the link with the user, then keep looping.

Only the driver retries. Never call `create_new_file` in this flow:
`generate_diagram` creates the file and the driver reuses its `fileKey`.

## Phase 8: Summary

Run `DC summary` (writes `REPORT.md`) and report to the user:
- FigJam link(s).
- A short architecture summary: subsystems, main runtime flow, key data paths.
- Diagram inventory: each diagram's title, type, and status
  (`generated | placed | verified | failed | skipped`).
- Model revision (git short hash, dirty or clean).
- Coverage with denominators ("analyzed 380 of 412 source files", "traced 4 of 5
  entry points") and its limitations (unsupported languages, parse errors,
  dynamic dispatch).
- Uncertainties from findings and merge warnings.
- Local artifact paths (`REPORT.md`, `model.json`, `plan.json`, `mermaid/`).
- How to continue: `/diagram-codebase --update` after code changes;
  `/diagram-codebase --resume` if anything is pending.

## Update mode

1. Run `DC update-diff`. It lists changed files, affected nodes and diagrams, the
   affected areas, and a recommendation (`partial` or `full`).
2. Re-analyze only the listed areas (Phase 3 rules apply), overwriting those
   `findings/<area>.json` files; on `full`, redo all of Phase 3.
3. Continue with Phases 4–8. The driver regenerates only diagrams whose content
   changed and marks removed ones obsolete.

## Honesty rules

- Report a check as passed only if it ran in this session and its output said
  so. Verification status comes from the manifest, never from your expectation.
- Ledger and `budget` numbers are local estimates of this skill's own usage, not
  Figma's quota. Say "estimated".
- Mocked or dry-run results say nothing about live Figma behavior; never present
  them as a live publish.
- Keep evidence statuses as recorded; `static_inferred` is never "confirmed".
