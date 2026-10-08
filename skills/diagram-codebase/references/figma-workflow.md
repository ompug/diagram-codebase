# Figma workflow

How the Phase 7 publish loop works. The driver (`dc.py next` / `record`)
decides every Figma call; you execute it faithfully and report what happened.

## Before the first call

- Figma MCP tools are often deferred. Load all their schemas in one `ToolSearch`
  `select:` query (the server prefix varies: `mcp__plugin_figma_figma__*` for the
  plugin, `mcp__figma__*` for a manually added server). Typical set:
  `generate_diagram, use_figma, get_figjam, get_screenshot, whoami`.
- Load the official skill named in the action's `skill` field before the first
  call of that tool in this session:
  - `generate_diagram` → `figma:figma-generate-diagram`
  - `use_figma` → `figma:figma-use` and `figma:figma-use-figjam`
  - `get_figjam` / `get_screenshot` → `figma:figma-use-figjam`
  Their rules are authoritative. The Mermaid in `params` was already linted
  against them by `dc.py plan`; you do not rewrite it.

## The loop

```
DC next                        → action JSON
(kind is a tool) call tool with exactly params
write raw result               → <out>/publish/results/<action_id>.json
DC record --action <id> --result-file <file>     (or --error "<one line>")
repeat
```

- **Exactly `params`.** Do not rename the diagram, edit Mermaid, add or drop a
  `fileKey`, or change `useArchitectureLayoutCode`. The driver has computed the
  content hash, the target file, and the layout row from these values. The one
  allowed addition: if a `use_figma` action lacks `skillNames`, add
  `"skillNames": "figma-use,figma-use-figjam"` (a logging field).
- **Raw result.** Save the tool's full text output unchanged (JSON or text). The
  driver parses URLs, file keys, node ids, and `safeToRetryWithoutCanvasRead`
  from it.
- **One action at a time.** Run `record` before the next `next`. The driver
  persisted the action as pending before printing it; an unrecorded action is an
  uncertain outcome, and the next `next` reconciles it (usually with a
  `get_figjam` action, or `ask_user` if no file exists yet) before any retry.
- **Driver-only retries.** After an error, `record` it and call `next`; the
  driver decides whether to retry, inspect the canvas, or give up on that diagram
  after `figma.max_attempts_per_diagram` attempts.

## Action kinds

| Kind | What it is | What you do |
|---|---|---|
| `generate_diagram` | Create one diagram from Mermaid. First one has no `fileKey` and creates the FigJam file; later ones pass that file's `fileKey`. | Call, save, record. Share the FigJam link once `record` reports it. |
| `use_figma` | FigJam Plugin API script from the driver's templates: wrap a diagram in a section, set shared plugin data (`diagramcodebase`: `diagram_id`, `run_id`, `content_hash`, `row`), place sections in rows, add a title/legend, remove obsolete sections. | Call, save, record. |
| `get_figjam` | Read the board to reconcile state or verify placement. | Call, save, record. |
| `get_screenshot` | Visual verification (only with `--verify-visual`). | Call, save, record. |
| `confirm` | Publishing not yet approved. | Show `dc.py review`, ask once, run `dc.py confirm` on yes. |
| `ask_user` | Driver needs a decision (e.g. a pending action's outcome is unknown and no file exists to inspect). | Ask the question in `explain`; save `{"answer": "<reply>"}`; record. |
| `stop` | Budget exhausted, backoff active, auth missing, or nothing more can be done safely. | End the loop; relay `explain`; give the resume command. |
| `done` | All diagrams handled. | Go to summary. |

## Diagram status

`pending → generated → placed → verified`; `failed` after the attempt limit;
`stale` (content changed under `--update`) → regenerated; `obsolete` (no longer
planned) → its section removed or marked, per the driver. Report the status the
manifest holds; `generated` is not `verified`.

## Verification

Verification is what the driver's `get_figjam` (and, with `--verify-visual`,
`get_screenshot`) actions established, as recorded in the manifest: each
diagram's section exists, carries the expected plugin data and `content_hash`,
and sits in its row. A diagram is verified only if such an action ran and
`record` marked it so. Nothing is verified in `--dry-run` or by mocked tests.

## Error handling

| Symptom in the tool result | Record | What happens next |
|---|---|---|
| Authentication required / not connected | `--error` with the message | Driver emits `stop` (auth). Tell the user to authenticate (troubleshooting.md), then `--resume`. |
| Rate limit / 429 / "too many requests" | `--error` | Hook/driver start a backoff; driver emits `stop` (backoff) or waits. |
| Hook denied the call (budget) | `--error` with the deny reason | Driver emits `stop` (budget). |
| Mermaid / syntax rejection from `generate_diagram` | `--error` | Driver marks the attempt failed; after the limit the diagram is `failed` and listed in the report. Do not hand-edit Mermaid. |
| `use_figma` error with `safeToRetryWithoutCanvasRead: true` | `--result-file` (the error payload) | Driver may retry the same script. |
| `use_figma` error with `safeToRetryWithoutCanvasRead: false` | `--result-file` | Driver inspects the canvas (`get_figjam`) before anything else. |
| Tool call timed out or the session crashed mid-call | nothing (pending stays) | Next `next` reconciles the pending action first. |
| `dc.py` itself fails | — | Show its one-line message; stop; troubleshooting.md. |

## Rules carried over from the official Figma skills

- `generate_diagram` creates its own FigJam file; never call `create_new_file`
  in this flow. Every later call reuses the same `fileKey` (the driver puts it
  in `params`) so the run produces one board, not a pile of drafts.
- Hybrid workflow: `generate_diagram` builds the structure; `use_figma` only
  adds sections, titles, legends, and plugin data around it. It never redraws
  or repositions diagram nodes.
- FigJam has no pages: organize with sections; `figma.createPage()` throws.
- Show the FigJam link as soon as the first diagram exists, before the
  remaining diagrams and extensions.
- If an extension fails after the diagram landed, say exactly what landed and
  what did not; partial progress is still delivered.

## Rate limits during the loop

The PreToolUse hook may pause a call (up to `max_hook_sleep_seconds`) to stay
inside the minute window; that pause is expected, not a hang. When the hook
denies a call or `next` emits `stop`, end the loop cleanly and report remaining
work and the resume command. Details: rate-limits.md.
