# Diagram planner

`plan/planner.py::plan_diagrams(model, options, config) -> plan` turns `model.json`
into the `plan.json` structure defined in `DESIGN.md` ("Diagram plan"). It is
deterministic: the same model, options and config give byte-identical output.
`generated_at` is copied from `options["generated_at"]` (the CLI stamps it);
`content_hash` is left empty and set later from the generated Mermaid.

Supporting modules: `plan/view.py` (`ModelView`: indexes, unit mapping,
aggregation, labels, shapes), `plan/graph.py` (SCC, label propagation, components,
packing, Jaccard), `plan/archmode.py` (architecture-layout eligibility).

## Levels

A diagram draws **units**. `ModelView.unit(node, level)` maps a model node to one:

| Level | Code nodes become | Cross-cutting nodes (stores, channels, externals, ROS interfaces, TF frames, deployments) |
|---|---|---|
| `subsystem` | their subsystem | stay themselves |
| `module` | their file (`ros_node`, `service`, `worker`, `algorithm`, endpoints without a handler stay themselves) | stay themselves |
| `symbol` | themselves | stay themselves |

Endpoints fold into their handler's unit except at symbol level. Edges between
units are aggregated (`view.summarize`); import edges are used only when no
runtime edge links the pair ("import-only", drawn dotted, label `imports`).
`config_dependency`, `inherits` never become edges.

Each spec carries `level` (extra key) and `model_nodes` = every model node drawn
or folded into a drawn unit.

## Readability limits (`config.DEFAULTS["plan"]`)

`max_nodes` 25, `max_edges` 30, `master_target_min/max` 12/20,
`architecture_max_edges` 20, `caps` {overview 2, standard 10, deep 25},
`max_subsystem_diagrams` {0, 5, 12}.

`_fit` enforces the limits in this order, writing a note for each step:
1. drop configuration units; 2. collapse channels/topics into direct
producer→consumer edges labelled with the channel name; 3. keep the
best-connected units (pinned units first) and list the omitted ones;
4. if edges are still over the limit, drop import-only edges, then lowest-weight.

## Diagram types

Priority (generation order under the cap): master, ros2 graph, subsystems,
dataflow, execution, sequence, algorithm, erd, state, dependency, infrastructure,
ros2-tf, then deep extras (2nd/3rd execution and sequence, per-subsystem
dependency). Capped diagrams go to `skipped` with their id.

| Type (id) | Row | Trigger / source | Skip when |
|---|---|---|---|
| master (`master`) | 0 | Always. Subsystem level if ≥3 subsystems contain significant nodes, else module level; switches level to land in 12–20 units. Cross-cutting nodes grouped into Data stores / Messaging / External services (≥2 members); at module level modules boxed by subsystem when >1 subsystem. TF and config edges excluded. Subsystem-level masters that pass `archmode.check` use `renderer: architecture` with `lanes`; otherwise `flowchart LR`. | fewer than 2 connected units |
| subsystem (`subsystem-<slug>[-part-N]`) | 1 | Top subsystems by significant nodes + cross-subsystem edges, up to `max_subsystem_diagrams`. Symbol level if everything (incl. 1-hop context) fits, else module level, else split by subdirectory or label propagation. Context: other subsystems (category `context`), cross-cutting nodes as themselves. Symbols boxed by module (≥2 members). | <2 significant nodes; over the per-depth subsystem cap |
| dataflow (`dataflow-<slug>` / `dataflow-derived`) | 2 | Claude `dataflows` (shape by role, edge label = link label or the source stage's representation). Otherwise derived from data-moving edges (publish/subscribe, channel triggers, db read/write, api_request, handles, external_call, data_flow) at the finest level that fits; reads point store→reader; code sources `lean_r`, sinks `lean_l`. | derived: <4 units, or Jaccard > 0.8 with an already-planned diagram |
| execution (`execution-<slug>`) | 2 | Claude `flows` of kind execution (decision → diamond, error → red + cross end, async dotted, Initialization/Runtime boxes). Otherwise BFS from entry points (functions preferred over modules) over calls/instantiates/spawns/triggers/handles/publishes/api_request, with db/external/service calls as leaves; children ordered by evidence line; depth ≤6, ≤`max_nodes`. Standard 1, deep 3. | <4 units; no entry points |
| sequence (`sequence-<slug>`) | 2 | Claude flows of kind sequence/request. Otherwise web request chains: `api_request` → endpoint → handler → cross-module calls → db/external → replies. Endpoints nobody requests start at the endpoint itself. Participants at module level. Standard 1, deep 3. | <3 participants; no chains |
| algorithm (`algorithm-<slug>`) | 3 | Claude `algorithms` only; TD; stage kind → shape (decision diamond, io lean_r, terminal stadium, loop hexagon, error red). | <2 stages |
| state (`state-<slug>`) | 3 | Claude `state_machines`; `renderer: state`; finals = states without outgoing transitions unless given. | <2 states |
| dependency (`dependency`, deep: `dependency-<slug>`) | 4 | Import graph at module level (boxed by subsystem) or subsystem level with counts when too big. Import cycles (SCCs) red + noted. | <3 units or <2 edges |
| infrastructure (`infrastructure`) | 4 | Deployment units (compose/k8s/launch files) + `depends_on`/`launches` edges. | <2 deployment units; outside `--focus` unless requested |
| ros2 (`ros2-graph[-part-N]`, `ros2-tf`) | 4 | ROS nodes + topics/services/actions; provide edges drawn as service→server ("served by"); nodes boxed by package; split by package when too big. TF tree (TD, circles) separately. | no ROS nodes; no connections; <2 TF frames |
| erd (`erd[-part-N]`) | 4 | `model.erd` entities, split into connected-component packs of ≤20. | <2 entities |

## Filters

- `types`: only those types; master only if listed (or no filter).
- `focus`: matched against subsystem id/name/paths, else node names/ids
  (case-insensitive substring). Scope = matches + 1-hop neighbours. Master stays
  as context; subsystem diagrams only for matched subsystems; dataflow, execution,
  sequence, algorithm, erd, state, dependency and ros2 are derived within scope.
  No match → only the master is planned and `skipped` says why.

## Coverage

`coverage` counts model nodes shown in the master, in any detail diagram, in any
diagram, drawn individually, and only in the model (with ids and per-kind counts).
Every count carries its denominator (`of` = all model nodes).

## Adding a diagram type

1. Add the type to `args.DIAGRAM_TYPES`, `ROW` and `TIER` in `planner.py`, and the
   DiagramSpec `type` list in `DESIGN.md`.
2. Write `_Planner._<type>()`: build units/edges (usually via `ModelView.aggregate`),
   call `self._fit`, then `self._flowchart(...)` (or `_base(...)` plus renderer
   fields), and `self.add(spec, tier)`. Call `self.skip(type, reason, id)` instead of
   emitting anything empty or trivial; respect `self.scope` for `--focus`.
3. Register it in the `steps` list in `run()` and add unit tests with a small
   synthetic model in `tests/unit/test_planner.py`.
