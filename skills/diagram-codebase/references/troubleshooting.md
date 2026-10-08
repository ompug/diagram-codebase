# Troubleshooting

## Figma not authenticated

Symptoms: Figma tools missing, "authentication required", or `next` emits
`stop` with an auth reason.

1. Ask the user to run `/mcp`, select the Figma server, and authenticate (or, with
   the Figma plugin installed, call its `authenticate` tool and then
   `complete_authentication` with the callback URL the user pastes back).
2. Check with `whoami` (exempt from rate limits).
3. Continue with `/diagram-codebase --resume`. Completed phases are not redone.

## Rate limited

Symptoms: hook denies a call, Figma returns 429 / "rate limit", `next` emits
`stop` (budget or backoff).

- Run `dc.py budget` for this skill's local estimates (not Figma's quota).
- Minute window: resume after about a minute. Day window or Figma-side limit:
  resume later; the report lists remaining diagrams.
- Lower the budget in `<out>/config.json` if Figma limits earlier than the
  defaults (rate-limits.md).

## Hooks not firing

Symptoms: `budget` shows no hook entries although Figma calls were made; the
driver reports estimating from its own records.

- Hooks register when the skill is invoked and stay for the rest of the session;
  a session started before the skill was installed or edited may need a restart.
- The hook command looks for `${CLAUDE_SKILL_DIR}/hooks/figma_gate.py`, then the
  copy in `~/.cache/diagram-codebase/figma_gate.py` (written by `dc.py init`). If
  neither exists it exits 0 and gating is off; the driver's own ledger check
  still applies.
- The matcher is `mcp__.*figma.*`; a Figma server registered under a name without
  "figma" is not matched.
- `python3` must be on the hook's PATH.

UNVERIFIED: whether Claude Code substitutes `${CLAUDE_SKILL_DIR}` inside skill
hook commands. The docs state the substitution for skill markdown and
`allowed-tools` Bash rules only. If it is not substituted, the shell expands the
unset variable to an empty string, the first path does not exist, and the cache
copy is used. To be confirmed in a live session.

## Invalid Mermaid

Symptoms: `plan` reports lint errors, or `generate_diagram` rejects a diagram.

- `plan` lints and sanitizes every diagram against the official
  figma-generate-diagram rules; unresolved problems are listed in its output and
  in `publish/review.md`.
- Inspect the text in `mermaid/<id>.mmd`. Fix the cause in the model (an odd
  label, an oversized diagram) and re-run `plan`; do not hand-edit Mermaid sent
  to Figma.
- A diagram that fails `figma.max_attempts_per_diagram` times is `failed` and
  named in the report; the rest of the run continues.

## Duplicate files or diagrams

Symptoms: several FigJam drafts, or the same diagram twice on a board.

- The driver reuses one `fileKey` per run and tags every section with
  `diagramcodebase` plugin data (`diagram_id`, `run_id`, `content_hash`), so
  `get_figjam` reconciliation detects existing diagrams before regenerating.
- Extra drafts usually come from tool calls made outside the loop (calling
  `create_new_file`, or `generate_diagram` without the driver's `fileKey`).
  Delete them in Figma; `manifest.json` names the file this run uses.

## Resume after a crash

- `/diagram-codebase --resume`: `init` reads `manifest.json`, reports the phase to
  continue from, and skips completed phases.
- A Figma call that was in flight when the session died stays as the pending
  action; the next `next` reconciles it (board inspection, or a question to the
  user) before any retry, so it is not blindly repeated.
- `dc.py status` shows phases, per-diagram status, and the pending action.
- A stale `active.json` (older than its freshness window) is ignored by the hook,
  so a crashed run never gates unrelated Figma use.
