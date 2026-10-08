# Integration TODO (temporary — delete when done)

Collected from component agents' reports. The integration agent owns resolving these.

## DESIGN.md contract updates
- Driver actions: fields `purpose`, `skills` (list — load ALL), `notes`; stop: `reason`, `instructions`,
  `remaining`, `usage`; `confirm`/`done` have `action_id: null`; generate `record` returns `show_user` (show at once).
- Manifest keys: `uncertain`, `plan_sig`, `action_seq`, `stop`, `verify`, `hooks_inactive`, `previous_revision`,
  `figma.ignore_ids`, `figma.legend_hash`; diagram: `place_attempts`, `next_content_hash`,
  `verify{status: ok|mismatch|missing|unknown|visual_ok, visual}`.
- Ledger entries carry `source: hook|driver`, `tool_use_id`.
- `mermaid/<id>.fallback.mmd` (plain flowchart) for `renderer == "architecture"`; plugin-data key `kind` (diagram|legend);
  verification threshold 60% of expected labels.
- get_figjam params are unverified: driver sends `{fileKey, nodeId: "0:1"}`, retries without nodeId once.

## CLI (cli.py / dc.py) wiring
- `next` → `driver.next_action(out, load_config(out))` (may sleep ≤65 s itself; no short timeout).
- `record --action ID [--result-file f|-] [--error text]` → `driver.record`.
- `confirm`/`status`/`budget` → `driver.confirm/status/budget_report`; `update-diff` → `state.update.affected`.
- `init` → `manifest.init_run` + `manifest.save`; also `ledger` copies hook (activate does it; init should too).
- `plan`: pass `options.generated_at`; fill `content_hash` from rendered Mermaid + title; write `.fallback.mmd`
  for architecture specs; lint + sanitize + optional parsecheck; write `publish/review.md`.
- See DESIGN.md "CLI behavior details" for the rest (args via stdin, init output, result file paths).

## SKILL.md
- Tell Claude to load every skill in the action's `skills` list; show `show_user` from generate records immediately.
- Hook PreToolUse timeout must exceed 65 s (currently 120 — OK).

## Review-pass items
- Planner: apply redundancy (Jaccard) check to execution diagrams too (fixture A execution ≈ subsystem diagram).
- Planner: validate derived sequence on b_web_app and c_events fixtures.

## From fixtures/scanner agent
- DESIGN.md id table: add `chan:<Class>.<attr>` / `chan:<module>.<var>` (queues), `chan:SIGTERM` (os signals,
  phase shutdown), `state:<name>` (Redux slice / React context client state).
- inventory.json: new `excluded_dirs_unit`; JS function nodes carry `metadata.end_line`.
- Scanner limitations to document in README "Limitations" (see agent list): Python no return-value flow /
  Optional unions; JS no nested fns or class methods; C++ regex limits; ROS launch namespace only for remaps,
  no `parameters=`/`<include>` following; TF lookups recorded as "looked up" edge; client-state nodes orphaned.
- Repo-wide ruff currently red in mermaid/build.py, sanitize.py, mermaid tests, figma/ledger.py (recheck).

## From mermaid/sanitize agent
- parsecheck.check returns `{status: ok|unavailable|error, results, detail}` — document in DESIGN.md.
- plan command: render → lint gate (`errors(lint(...)) == []`) → write .mmd; one shared `Redactor` across specs
  (report in publish/review.md, never the redacted values); architecture specs need `lanes` for every node.
- Driver: verify labels with `build.display_label(label)`; sequence participant names via `build.id_map(spec)`.
- archmode.py: import LANES / pair tables from mermaid/lint.py (single source); async produce/consume pair
  must not count as duplicate.
- CI: `npm ci` in skills/diagram-codebase/tools/mermaid-check so parser tests run.
- Builder caps labels at 80 chars ("...") — reconcile with "no truncation of important labels" (note it in spec notes).
