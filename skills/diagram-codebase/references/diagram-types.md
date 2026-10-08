# Diagram types

What each diagram shows and what it needs from the model. The planner
(`dc.py plan`) decides which to produce; this file explains its choices to the
user and tells you which findings make a type possible. A type with no supporting
model content is listed in `plan.json` `skipped` with the reason.

| Type | Answers | Renderer | Needs in the model | FigJam row |
|---|---|---|---|---|
| `master` | What are the major parts and how do they connect? | architecture or flowchart | subsystems, cross-subsystem edges, datastores, externals | 0 |
| `subsystem` | What is inside one subsystem and what does it talk to? | flowchart | nodes assigned to the subsystem, their edges | 1 |
| `dataflow` | How does data move and change representation? | flowchart (LR) | `dataflows` with stages/roles/representations | 2 |
| `execution` | What runs, in what order, with which branches? | flowchart (TD) | `flows` with steps, decisions, phases | 2 |
| `sequence` | Who talks to whom over time for one scenario? | sequence | a `flow` with steps between few participants | 2 |
| `algorithm` | What are the stages and decisions of a core computation? | flowchart (TD) | `algorithms` with stages and labelled transitions | 3 |
| `state` | What states does an entity/process move through? | state | `state_machines` | 3 |
| `dependency` | What depends on what (packages, modules, libraries)? | flowchart | `imports`/`depends_on` edges | 4 |
| `infrastructure` | What is deployed where, with what config and externals? | architecture or flowchart | deployment units, config, externals, launches | 4 |
| `ros2` | Which nodes exchange which topics/services/actions/TF? | flowchart (LR) | ROS 2 nodes and interfaces with pub/sub edges | 1 |
| `erd` | What are the tables/entities and their relations? | erd | `erd.entities`, `erd.relations` | 4 |

## Depth

`--depth` caps how many diagrams are planned (defaults in `config.plan.caps`):

- `overview` (2): the master diagram plus the single most informative one.
- `standard` (10): master, the largest subsystems, the main flows and algorithms.
- `deep` (25): every subsystem up to `max_subsystem_diagrams.deep`, every traced
  flow, algorithm, and state machine, plus structure diagrams. Deep also turns on
  parallel analysis, so findings are richer.

Readability limits apply to every type: about 25 nodes and 30 edges per diagram
(master 12–20). Oversized diagrams are split into `-part-2`, `-part-3`; anything
left out is named in the diagram's `notes`, never dropped silently.

## Requesting types

`--type t[,t...]` restricts the plan to those types. Aliases: `overview`,
`architecture`, `arch` → master; `data`, `data-flow` → dataflow; `flow`, `exec`,
`runtime` → execution; `seq` → sequence; `algo` → algorithm; `deps` →
dependency; `infra`, `deployment` → infrastructure; `ros` → ros2; `er`,
`entities` → erd; `states`, `state-machine` → state.

A requested type the model cannot support is skipped with a reason, not faked.
When the user asks for one, aim the analysis at what it needs (from the table
above): `--type algorithm` means extracting `algorithms` with real stages;
`--type erd` means finding the schema (ORM models, migrations, SQL).

`--focus X` scopes every diagram to subsystem X plus one hop of context.

## Not supported by Figma's `generate_diagram`

Class diagrams, mindmaps, pie charts, C4, journeys, timelines, git graphs. The
planner never emits them; if the user asks, say so and offer the nearest type
(class structure → `subsystem` or `dependency`).
