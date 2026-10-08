# diagram-codebase — Design and Contracts

This is the source of truth for module responsibilities and the data contracts
between them. Code must match it; if a contract has to change, update this file
in the same change.

## Pipeline

```
Repository
  └─ dc.py scan       (deterministic)  → inventory.json, findings/scan.json
  └─ Claude analysis  (interpretive)   → findings/<area>.json   (main session writes these)
  └─ dc.py merge      (deterministic)  → model.json, validation.json
  └─ dc.py plan       (deterministic)  → plan.json, mermaid/<diagram-id>.mmd, publish/review.md
  └─ dc.py next/record loop            → Claude calls Figma MCP tools; manifest.json tracks state
  └─ dc.py summary                     → REPORT.md
```

Claude is responsible for meaning: execution tracing, data flow, algorithm
stages, and naming subsystems. Python is responsible for everything that can be
checked or computed: inventory, structural facts, evidence checking, validation,
planning, Mermaid generation, sanitizing, budgeting and state.

## Source layout (one owner per module)

```
skills/diagram-codebase/
  SKILL.md                     orchestration instructions for Claude
  references/                  progressive-disclosure docs loaded by SKILL.md
  hooks/figma_gate.py          PreToolUse/PostToolUse/PostToolUseFailure hook (stdlib, standalone)
  tools/mermaid-check/         optional Node validator (mermaid + jsdom), used by CI
  scripts/dc.py                CLI entry: `python3 dc.py <command>`
  scripts/diagram_codebase/
    common.py config.py args.py
    scan/      inventory gitinfo manifests lang_python patterns_py lang_js lang_cpp cpp_text ros2
    model/     schema (vocabularies + validator)  builder  merge
    plan/      view (ModelView, aggregation)  graph  archmode  planner
    mermaid/   build (spec -> Mermaid)  lint (Figma rules)  parsecheck (optional node check)
    sanitize.py
    figma/     ledger  ratelimit  driver  scripts (use_figma JS templates)
    state/     manifest  update
    cli.py
```

Runtime code is **standard library only** (Python >= 3.10). Tests use pytest.

## Output directory (`<repo>/.diagram-codebase/` by default)

```
.gitignore            contains "*" so the analyzed repo is never modified
config.json           user overrides (deep-merged over config.DEFAULTS)
inventory.json        scan inventory
findings/scan.json    deterministic findings (model fragment)
findings/<area>.json  Claude findings (one file per analysis area/agent)
model.json            merged, validated architecture model
validation.json       {errors, warnings, rejected, stats}
plan.json             diagram plan (list of DiagramSpec)
mermaid/<id>.mmd      exact Mermaid text that will be sent to Figma
publish/review.md     human-readable list of everything that will leave the machine
manifest.json         run state (phases, diagrams, Figma ids, pending action, events)
REPORT.md             final summary
```

The rate-limit ledger is global (Figma quota is per account, not per repo):
`$XDG_CACHE_HOME/diagram-codebase/ledger.jsonl` (default `~/.cache/...`), plus
`active.json` (the run marker) and a copy of `figma_gate.py`.

## Model (`model.json`)

Vocabularies and the validator live in `model/schema.py` (source of truth).
Shape: `{schema_version, meta, subsystems[], nodes[], edges[], evidence[], flows[],
dataflows[], algorithms[], state_machines[], erd{entities[], relations[]}}`.

- **Evidence** is a separate table: `{id, file, line_start, line_end, symbol, detail,
  source: ast|regex|manifest|claude|doc|runtime, status}`. Status is one of
  `confirmed | dynamic_observed | static_inferred | documented_only | unknown` and is
  never collapsed. Nodes/edges reference evidence by id; their `evidence_status`
  (strongest) and `confidence` are **derived** by `schema.apply_derived`.
- **Imports are never calls.** `imports` edges come only from import statements.
  `calls` require a resolvable call site.
- **Phase** on edges: `init | runtime | shutdown | error | build | unknown`.

### Node id conventions

| Prefix | Meaning | Example |
|---|---|---|
| `mod:<path>` | source file/module | `mod:app/service.py` |
| `cls:<path>:<Qual>` | class | `cls:app/service.py:InventoryService` |
| `fn:<path>:<Qual>` | function/method (`Class.method`) | `fn:app/service.py:InventoryService.restock` |
| `api:<METHOD> <norm path>` | HTTP endpoint (path params → `{}`) | `api:GET /api/todos/{}` |
| `ros:<name>` | ROS 2 node | `ros:scan_filter` |
| `topic:/<name>` `srv:/<name>` `action:/<name>` | ROS 2 interfaces | `topic:/scan` |
| `tf:<frame>` | TF frame | `tf:map` |
| `chan:<name>` | queue/topic/event channel | `chan:order.created` |
| `store:<slug>` | datastore | `store:sqlite` |
| `ent:<table>` | DB entity | `ent:items` |
| `ext:<host or vendor>` | external service | `ext:hooks.example.com` |
| `cfg:env:<VAR>` `cfg:file:<name>` `param:<node>/<name>` | configuration | `cfg:env:INVENTORY_DB` |
| `deploy:<name>` `launch:<path>` | deployment units / launch files | `deploy:db` |
| `sub:<slug>` | subsystem | `sub:backend` |
| anything else | Claude-defined concept (`algo:icp`, `data:point-cloud`) | |

### Edge direction semantics

Edges point in the direction of control or data: `publishes` producer→channel,
`subscribes` channel→consumer, `triggers` event source→callback, `handles`
endpoint→handler, `api_request` client→endpoint, `db_read`/`db_write`
code→datastore (label says which), `tf_transform` parent frame→child frame,
`launches` launch file→node, `depends_on` dependent→dependency.

## Claude findings (`findings/<area>.json`)

Same shape as a model fragment, but evidence is **inline** and is checked by
`merge.EvidenceChecker` (file exists in the inventory or is a doc, lines exist,
cited `symbol` appears within ±3 lines). Items left without accepted evidence are
rejected and listed in `validation.json`.

```json
{
  "agent": "execution", "area": "request handling",
  "subsystems": [{"id": "sub:backend", "name": "Backend API", "description": "...", "paths": ["backend"]}],
  "nodes": [{"id": "algo:icp", "name": "ICP registration", "kind": "algorithm", "subsystem": "sub:pipeline",
             "description": "...", "evidence": [{"file": "pipeline/icp.py", "lines": "12-80", "symbol": "register", "status": "confirmed"}]}],
  "edges": [{"from": "fn:a.py:x", "to": "fn:b.py:y", "kind": "calls", "label": "per frame", "phase": "runtime",
             "evidence": [{"file": "a.py", "lines": "40", "symbol": "y", "status": "confirmed"}]}],
  "flows": [{"id": "flow-restock", "name": "Restock command", "kind": "execution", "trigger": "CLI restock",
             "steps": [{"from": "...", "to": "...", "label": "...", "kind": "call|return|async|event|decision|loop|error|io",
                        "condition": "optional", "phase": "init|runtime|..."}], "evidence": [...]}],
  "dataflows": [{"id": "...", "name": "...", "stages": [{"node": "...", "role": "input|transform|intermediate|storage|output",
                 "representation": "PointCloud2"}], "links": [{"from": "...", "to": "...", "label": "..."}], "evidence": [...]}],
  "algorithms": [{"id": "algo-icp", "name": "ICP", "node": "algo:icp", "purpose": "...", "inputs": [], "outputs": [],
                  "stages": [{"id": "s1", "name": "Find correspondences", "kind": "step|decision|loop|io|terminal|error",
                              "functions": ["fn:..."], "evidence": [...]}],
                  "transitions": [{"from": "s1", "to": "s2", "label": "..."}], "evidence": [...]}],
  "state_machines": [{"id": "...", "name": "...", "states": [{"id": "Idle", "name": "Idle"}], "initial": "Idle",
                      "transitions": [{"from": "Idle", "to": "Run", "label": "start"}], "evidence": [...]}],
  "erd": {"entities": [{"id": "users", "name": "users", "attributes": [{"name": "id", "type": "int", "key": "PK"}], "evidence": [...]}],
          "relations": [{"from": "users", "to": "orders", "cardinality": "one-to-many", "label": "places"}]},
  "uncertainties": ["Plugin loading via entry points is dynamic; consumers not traced."]
}
```

An entry in `nodes` whose id already exists (e.g. a scanned function) is an
**update**: it may add description/subsystem/tags without new evidence.

## Diagram plan (`plan.json`)

`{"generated_at", "model_revision", "options", "diagrams": [DiagramSpec...], "skipped": [{type, reason}], "coverage": {...}}`

DiagramSpec (renderer-independent; `mermaid/build.py` turns it into text):

```json
{
  "id": "master",                      // stable, slug-like; splits get "-part-2"
  "title": "System overview",
  "type": "master|subsystem|dataflow|execution|sequence|algorithm|dependency|infrastructure|ros2|erd|state",
  "purpose": "one sentence: what question this diagram answers",
  "renderer": "flowchart|architecture|sequence|erd|state",
  "direction": "LR|TD",
  "row": 0,                            // FigJam layout row (0 overview, 1 subsystems, 2 flows, 3 algorithms/state, 4 structure)
  "priority": 0,                       // lower = generated earlier
  "groups": [{"id": "g1", "label": "Data stores", "parent": null, "category": "data"}],
  "nodes": [{"id": "<unit id>", "label": "service.py", "shape": "rect", "category": "app", "group": "g1",
             "model_ids": ["mod:app/service.py", "fn:..."]}],
  "edges": [{"from": "<unit id>", "to": "<unit id>", "label": "reads items", "style": "solid|dotted|thick",
             "end": "arrow|cross|circle|none", "bidir": false, "model_edges": ["e:..."], "evidence_status": "confirmed"}],
  "lanes": {"<unit id>": "service"},  // renderer=architecture only
  "participants": [{"id": "...", "label": "..."}],                 // renderer=sequence
  "messages": [{"from": "...", "to": "...", "label": "...", "kind": "sync|reply|async"}],
  "entities": [{"id": "...", "attributes": [{"name","type","key"}]}],      // renderer=erd
  "relations": [{"from","to","cardinality","label","identifying"}],
  "states": [{"id","label"}], "transitions": [{"from","to","label"}], "initial": "...", "finals": [],  // renderer=state
  "notes": ["12 configuration parameters omitted; see model.json"],
  "model_nodes": ["...every model node id represented..."],
  "content_hash": "sha256 of the generated Mermaid + title (set by `dc.py plan`)"
}
```

Planner additions (see `docs/PLANNER.md` for heuristics): each spec also has `level`
(`subsystem|module|symbol`); `skipped[]` entries are `{type, id?, reason}` (focus misses use
`type: "focus"`); `coverage` = `{definition, model_nodes, master, detail, any_diagram,
drawn_individually: {count, of}, only_in_model: {count, of, by_kind, ids}}`. Split ids use
`-part-N`; a derived data-flow diagram is `dataflow-derived`; deep mode adds
`dependency-<subsystem>`; algorithm stage nodes are `stage:<algo id>:<stage id>`. The
planner never reads the clock: `dc.py plan` passes `options.generated_at` and fills
`content_hash`.

Shapes: `rect rounded stadium circle diamond hexagon subroutine cylinder lean_r lean_l odd`.
Categories (legend colors): `app` blue, `data` green, `processing` purple,
`external` orange, `infra` gray, `error` red, `messaging` teal, `context` light gray.

Readability thresholds come from `config.DEFAULTS["plan"]` (25 nodes / 30 edges per
diagram, master 12–20, nesting ≤ 2). Oversized diagrams are split, never truncated
silently: anything omitted is listed in `notes`.

## Mermaid rules (Figma `generate_diagram`)

Authoritative: the official skill `figma-generate-diagram` and its `references/`
(installed with the Figma Claude Code plugin). Summary enforced by `mermaid/lint.py`:
supported headers only (`flowchart`, `sequenceDiagram`, `stateDiagram-v2`, `erDiagram`);
no emoji; no `\n`; no HTML tags; node ids camelCase without underscores and never
`end|subgraph|graph`; quote labels containing special characters; only `fill`/`stroke`
styling and only on flowcharts; no styling on state/ER; sequence diagrams have no
`Note`, `loop`, `alt`, `opt`, `par`, `rect`, `activate`, `autonumber`, `as` aliases
(participant ids render as-is, so they must be readable PascalCase); architecture
mode obeys `plan/archmode.py`.

## Figma driver protocol

Claude never decides what to send to Figma. It loops:

1. `python3 dc.py next` prints one JSON action:
   ```json
   {"action_id": "a-0007", "kind": "generate_diagram|use_figma|get_figjam|get_screenshot|confirm|ask_user|stop|done",
    "tool": "generate_diagram", "params": {...exact tool arguments...},
    "skill": "figma:figma-generate-diagram", "diagram_id": "master", "explain": "one line for the user",
    "record_hint": "how to call record afterwards"}
   ```
2. Claude loads the named official skill (first time per tool), calls the tool with
   exactly `params`, saves the raw tool result to a file.
3. `python3 dc.py record --action a-0007 --result-file <file> [--error "<text>"]`.

The driver persists `pending_action` **before** emitting it. A pending action
without a recorded result means its outcome is uncertain; the driver reconciles
(via `get_figjam`, or by asking the user when no file exists yet) before any retry.
Diagram status: `pending → generated → placed → verified`, or `failed` after
`figma.max_attempts_per_diagram` attempts, or `stale` (update) / `obsolete`.

Every section the skill creates carries shared plugin data in namespace
`diagramcodebase` (`diagram_id`, `run_id`, `content_hash`, `row`) so state can be
reconciled from the canvas.

## CLI (`python3 ${CLAUDE_SKILL_DIR}/scripts/dc.py <command>`)

All commands accept `--repo <path>` (default: git toplevel of cwd) and `--out <dir>`
(default `<repo>/.diagram-codebase`), print JSON (or Markdown where noted) to stdout,
and exit non-zero with a one-line message on `DCError`.

| Command | Purpose |
|---|---|
| `args "<raw $ARGUMENTS>"` | parse options → JSON (`help: true` → print usage) |
| `init --args-json '<json>'` | create/resume run: out dir, `.gitignore`, config, manifest; reports mode fresh/resume/update and next step |
| `scan` | inventory + deterministic findings; prints the analysis brief |
| `findings-template` | prints the findings JSON template + id conventions for analysis agents |
| `check-findings <file>` | validate one findings file (evidence check) without merging |
| `merge` | merge all findings → model.json + validation.json; prints summary |
| `plan` | plan.json + mermaid/*.mmd + lint + sanitize + optional parser check + publish/review.md |
| `review` | Markdown summary of what will be published and estimated Figma calls |
| `confirm` | record user approval to publish |
| `next` / `record --action <id> [--result-file f] [--error text]` | driver loop |
| `status` | manifest summary (phases, diagrams, pending action) |
| `budget` | local ledger estimates (never Figma's quota) |
| `update-diff` | `--update`: affected files/nodes/diagrams + recommendation |
| `summary` | write and print REPORT.md |
| `run --dry-run` | init + scan + merge + plan without Claude findings (CI/examples) |

CLI behavior details (consumed by SKILL.md):
- `args` also reads the raw string from stdin when no positional is given, so SKILL.md
  can pass `$ARGUMENTS` through a quoted heredoc (safe for any quotes in user input).
- `init` prints `{out_dir, mode: fresh|resume|update, resume_from_phase, next_step}`,
  writes the resolved out dir into the manifest, and copies `hooks/figma_gate.py`
  into the cache dir. Later commands find the out dir via `--out`, else
  `<repo>/.diagram-codebase`.
- Raw Figma tool results are saved by Claude to `<out>/publish/results/<action_id>.txt`;
  `record --result-file -` reads stdin. `ask_user` answers are recorded as `{"answer": "..."}`.
- `next` emits `confirm` until `confirm` ran (or `--yes`); `use_figma` params already
  include `skillNames`; `tool` is the bare tool name (call it on whichever Figma
  server prefix is connected); `stop` actions carry the reason in `explain`.
- `record` prints the FigJam file URL as soon as it is known.
- `update-diff` names affected findings areas by slug (`execution`, `dataflow`,
  `algorithms`, `infrastructure`, `sub-<slug>`) and recommends `partial|full|none`.

## Rate limiting

- Counted tools: every Figma MCP tool except `config.budget.exempt_tools`.
- `hooks/figma_gate.py` (registered in SKILL.md frontmatter, matcher
  `mcp__.*figma.*`) acts only while `active.json` exists and is fresh. Pre: rolling
  60 s window → sleep (≤ `max_hook_sleep_seconds`) or deny; rolling 24 h window →
  deny. Post/PostFailure: append outcome; detect rate-limit text → backoff.
- `dc.py next` checks the same ledger before emitting a counted action
  (defense in depth) and estimates from its own records if hooks never fired.
- Ledger numbers are **local estimates**, never presented as Figma's quota.

## Tests

`tests/unit` (pure functions), `tests/integration` (fixture repo → scan → merge →
plan → mermaid; mocked Figma driver loop). Fixtures live in `tests/fixtures/` and
are tiny, self-contained, and contain no real secrets (fake ones are clearly fake).
No test may require network, Figma, ROS, or Node (Node-based checks are skipped
when unavailable; CI installs Node).
