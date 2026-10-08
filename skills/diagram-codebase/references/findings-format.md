# Findings format

Contract for `findings/<area>.json`. Source of truth: `docs/DESIGN.md` ("Claude
findings") and `model/schema.py` (vocabularies). `dc.py findings-template` prints
the live template; if it and this file disagree, the template wins.

## File

- Path: `<out>/findings/<area>.json`, `<area>` a slug (`execution`, `dataflow`,
  `algorithms`, `infrastructure`, `sub-localization`). `scan.json` is reserved for
  the scanner.
- Written only by the main session. Subagents return the JSON as text.
- Validate with `dc.py check-findings <file>` before `merge`.

## Top level

```json
{
  "agent": "execution",
  "area": "request handling",
  "subsystems": [], "nodes": [], "edges": [],
  "flows": [], "dataflows": [], "algorithms": [], "state_machines": [],
  "erd": {"entities": [], "relations": []},
  "uncertainties": []
}
```

All collections are optional; omit what the area does not produce.

## Evidence (inline)

```json
{"file": "pipeline/icp.py", "lines": "12-80", "symbol": "register", "status": "confirmed", "detail": "optional"}
```

- `file`: repo-relative, must be in the scan inventory (any file not excluded,
  binary, generated, a lockfile, or oversized) or a doc (`.md .rst .txt .adoc`).
- `lines`: `"N"` or `"N-M"` within the file. Omit only with `documented_only`.
- `symbol`: optional but strongly preferred; its last `.`/`:`/`/`-separated token
  must appear within ±3 lines of the range. A `calls` edge cited as `confirmed`
  without a symbol is downgraded to `static_inferred`.
- `status`: `confirmed | dynamic_observed | static_inferred | documented_only |
  unknown` (default `static_inferred`). Doc files are forced to `documented_only`.
  `dynamic_observed` requires `detail`.

## Subsystems

```json
{"id": "sub:backend", "name": "Backend API", "description": "...", "paths": ["backend"]}
```

An existing id is renamed/extended; `paths` are merged.

## Nodes

```json
{"id": "algo:icp", "name": "ICP registration", "kind": "algorithm", "subsystem": "sub:pipeline",
 "description": "...", "parent": null, "tags": [], "inputs": [], "outputs": [],
 "evidence": [...]}
```

- New node: needs `id`, `name`, valid `kind`, and accepted evidence.
- Existing id (scanned or from another area): an update; evidence optional. The
  scanner's `kind` wins on conflict.
- Node kinds: `module package class function service worker ros_node api_endpoint
  ui_component datastore db_entity event_channel topic ros_service ros_action
  tf_frame external_service library config algorithm data_artifact
  deployment_unit state`.

## Edges

```json
{"from": "fn:a.py:x", "to": "fn:b.py:y", "kind": "calls", "label": "per frame",
 "phase": "runtime", "payload": [], "evidence": [...]}
```

- Edge kinds: `calls imports inherits instantiates data_flow publishes subscribes
  api_request handles db_read db_write db_access ipc state_transition
  config_dependency tf_transform service_call service_provide action_call
  action_provide external_call spawns triggers launches depends_on error_path`.
- Phases: `init runtime shutdown error build unknown`.
- Direction follows control or data: `publishes` producer→channel, `subscribes`
  channel→consumer, `triggers` event source→callback, `handles`
  endpoint→handler, `api_request` client→endpoint, `db_read`/`db_write`
  code→datastore, `tf_transform` parent→child frame, `launches` launch
  file→node, `depends_on` dependent→dependency.
- An edge matching an existing (scanned) edge may refine `label`/`phase` without
  new evidence. The scanner owns `imports`; leave them out.

## Flows, dataflows, algorithms, state machines, ERD

```json
"flows": [{"id": "flow-restock", "name": "Restock command", "kind": "execution", "trigger": "CLI restock",
  "steps": [{"from": "...", "to": "...", "label": "...", "kind": "call|return|async|event|decision|loop|error|io",
             "condition": "optional", "phase": "runtime", "evidence": [...]}],
  "evidence": [...]}],
"dataflows": [{"id": "df-scan", "name": "...",
  "stages": [{"node": "...", "role": "input|transform|intermediate|storage|output", "representation": "PointCloud2"}],
  "links": [{"from": "...", "to": "...", "label": "..."}], "evidence": [...]}],
"algorithms": [{"id": "algo-icp", "name": "ICP", "node": "algo:icp", "purpose": "...", "inputs": [], "outputs": [],
  "stages": [{"id": "s1", "name": "Find correspondences", "kind": "step|decision|loop|io|terminal|error",
              "functions": ["fn:..."], "evidence": [...]}],
  "transitions": [{"from": "s1", "to": "s2", "label": "..."}], "evidence": [...]}],
"state_machines": [{"id": "sm-job", "name": "...", "states": [{"id": "Idle", "name": "Idle"}], "initial": "Idle",
  "transitions": [{"from": "Idle", "to": "Run", "label": "start"}], "evidence": [...]}],
"erd": {"entities": [{"id": "users", "name": "users", "attributes": [{"name": "id", "type": "int", "key": "PK"}], "evidence": [...]}],
        "relations": [{"from": "users", "to": "orders", "cardinality": "one-to-many", "label": "places"}]}
```

- Each structure needs accepted evidence at the top level or on at least one
  step/stage. A step or stage whose own evidence fails the check is dropped (with
  a warning), so give every stage evidence that holds.
- A second structure with the same `id` replaces the first; keep ids unique
  across areas.
- `stages[].node` and `steps[].from/to` should be node ids present in the model
  (scanned or yours).

## Node id conventions

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
| anything else | Claude-defined concept | `algo:icp`, `data:point-cloud` |

Paths are repo-relative POSIX. Ids derive from the code's own names, never from
time or randomness, so re-runs and `--update` keep them stable.

## Rejections

`check-findings` and `merge` list every rejected item with `where` and `reason`
(also saved in `validation.json`). Typical reasons and the fix:

| Reason | Fix |
|---|---|
| `file not in analyzed inventory` | Path typo, or the file is excluded; cite an inventoried file. |
| `lines N-M out of range` | Re-read the file and cite real lines. |
| `symbol 'x' not found near f:N-M` | Move the range to where `x` appears, or cite the right symbol. |
| `new node without accepted evidence` | Add evidence, or drop the node. |
| `edge without accepted evidence` | Cite the call/publish/query site, or drop the edge. |
| `invalid kind` | Use a kind from the lists above. |
| `dynamic_observed needs a detail` | Add `detail`, or use a weaker status. |
