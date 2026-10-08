# Analysis guide

How to turn source code into findings that survive the evidence check. Read
this at the start of Phase 3; subagents get the same rules through
`explore-agent-brief.md`.

The scanner already produced modules, classes, functions, imports, declared
endpoints, manifests, deployments, and (for ROS 2) declared interfaces. Your job
is the part syntax cannot show: what actually runs, in what order, carrying what
data, and why. Add to the scanned skeleton; do not restate it.

## The 12 execution questions

Answer each one the repository makes relevant. An unanswerable question goes into
`uncertainties` with the reason ("handler registered via entry points; consumers
not traced").

1. **Start**: what starts the program? (`main`, CLI command, server bootstrap,
   launch file, container `CMD`, scheduled job)
2. **Init**: what happens during startup, in order? (config load, dependency
   wiring, connections opened, handlers/subscriptions registered)
3. **Driver**: what keeps it running? (request loop, event loop, timer, frame
   callback, training loop, UI render cycle)
4. **Triggers**: what starts each unit of work? (HTTP request, message, timer,
   user action, file arrival)
5. **Path**: for each main unit of work, what is the call path from trigger to
   effect?
6. **Data**: what enters, how is it transformed, where does it end up, and in
   what representation at each stage?
7. **State**: where does state live (memory, DB, cache, files) and who mutates it?
8. **Async**: where are the asynchronous boundaries: who produces, through which
   channel, who consumes?
9. **Algorithms**: which computations do the heavy lifting, and what are their
   stages, decisions, and loops?
10. **External**: which external systems are called, in which direction, and why?
11. **Errors**: how are failures handled: retry, fallback, propagate, crash, error
    path to a user or log?
12. **Termination**: how does it stop: shutdown hooks, termination conditions,
    cleanup, signals?

## Trace from entry points

Start at each entry point in the brief (`entry_points`) and follow the code that
runs, not the code that exists:

- Follow a call only where you can see the call site. Resolve the callee by
  reading it, not by matching names.
- At each branch, note the condition; at each loop, note what it iterates over.
- Stop descending at library boundaries; represent the library call as one step
  to an `external_service`, `datastore`, or `library` node.
- Prefer the few flows a newcomer must understand (the main request path, the main
  pipeline, the startup sequence) over exhaustive coverage of helpers.
- When a call target is chosen at runtime (registry, plugin, dependency
  injection, `getattr`, virtual dispatch), find where the registry is filled. If
  you can tie a concrete target to it, use `static_inferred`; otherwise record the
  dispatch point and add an uncertainty.

## Init vs runtime

Tag every edge and flow step with a `phase`:

- `init`: runs once during startup (construction, registration, connection setup,
  subscription creation, route declaration).
- `runtime`: runs per request/message/tick.
- `shutdown`: cleanup on exit.
- `error`: only on failure paths.
- `build`: build/deploy time (codegen, Dockerfile, CI).
- `unknown`: you cannot tell; say why in `uncertainties`.

A subscription created in `__init__` is an `init` edge from the node to the topic;
the callback that fires per message is a `runtime` flow. Draw both, labelled
differently.

## Async producers and consumers

For every queue, topic, event bus, signal, or channel:

1. Find every producer (the line that publishes/enqueues/emits) and every consumer
   (the line that subscribes/dequeues/registers a handler).
2. Model the channel as a node (`event_channel`, `chan:<name>`; ROS 2 uses
   `topic:/<name>`) with `publishes` producer→channel and `subscribes`
   channel→consumer.
3. Note the payload type in the edge `label` or `payload`.
4. A channel with producers but no consumers (or the reverse) is a finding: keep
   the half you found and say in `uncertainties` that the other side is outside
   the repo or not found.

Callbacks across thread, process, or network boundaries are flow steps of kind
`async`; same-thread synchronous calls are `call`.

## Algorithm stage extraction

For each algorithm worth its own diagram (non-trivial, central to the system's
purpose: registration, planning, scoring, scheduling, inference):

1. Name its purpose in one sentence, plus `inputs` and `outputs`.
2. Split it into 3–12 stages, each one a meaningful step, not one line of code.
   Stage `kind`: `step`, `decision`, `loop`, `io`, `terminal`, `error`.
3. Every `decision` stage has at least two outgoing `transitions`, each labelled
   with the condition ("converged", "max iterations reached").
4. Loops get a back-transition labelled with the loop condition.
5. List the implementing functions in `functions` (model ids) and give each stage
   its own evidence.

## Evidence rules

Every new node, edge, flow, dataflow, algorithm, state machine, and ERD entity
needs at least one evidence item `{file, lines, symbol, status}` that the checker
can verify:

- `file`: repo-relative path that the scan inventoried (docs are always allowed).
- `lines`: `"40"` or `"12-80"`; must exist in the file.
- `symbol`: a name that appears within ±3 lines of the range (its last
  `.`/`:`-separated part is what gets matched). Cite the call site, not the
  definition, when claiming a call.

Pick the status that matches what you actually saw:

| Status | Use when |
|---|---|
| `confirmed` | The cited lines directly declare it: the call, the publish, the route decorator, the SQL statement. |
| `static_inferred` | You reasoned it from code that does not state it directly: a target resolved through a registry, a type, a naming convention, or a chain of calls. |
| `documented_only` | Only a README/doc says it (doc files are forced to this status). |
| `dynamic_observed` | You saw it in runtime output already present in the repo (logs, traces). Requires a `detail` describing the observation. Rare. |
| `unknown` | You believe it exists but cannot place it; usually better as an `uncertainty`. |

When unsure between two statuses, choose the weaker one.

## What not to infer

Each of these looks like evidence and is not:

- **Imports ≠ calls.** An import shows a dependency, never a call; the scanner
  already emits `imports` edges. A `calls` edge needs the call site.
- **Config ≠ execution.** A setting, env var, or YAML key shows something can be
  configured, not that the code path using it runs. Model it as `config` with a
  `config_dependency` edge to the code that reads it.
- **Declared ≠ invoked.** A defined route, handler, or method that nothing
  reaches is dead or externally invoked; say which, or add an uncertainty.
- **Test code ≠ production.** Tests, fixtures, mocks, and examples show intended
  use, not the system's wiring. Cite them only as `static_inferred` support for a
  production claim, never as the sole evidence of a production edge.
- **Names ≠ behavior.** `OrderProcessor` may not process orders; read the body.
- **Comments and TODOs ≠ code.** Treat them like docs: `documented_only` at most.

## Writing findings

One file per area, `findings/<area>.json`, with `agent` and `area` set. Shape and
id conventions: `findings-format.md`; exact template: `dc.py findings-template`.

- Reuse scanned ids (`fn:app/service.py:InventoryService.restock`) whenever the
  thing already exists; an entry with an existing id is an update and may add
  `description`, `subsystem`, and `tags` without new evidence.
- New concepts get a prefix from the conventions (`algo:`, `data:`, `chan:`,
  `ext:`, `store:`) and a stable slug derived from the code's own name.
- Name subsystems the way the team would (`sub:localization`, "Localization"),
  and give each a one-sentence description.
- Edge `label`: a short verb phrase ("reads items", "per frame"), ≤ 80 chars.
- Put every unresolved question in `uncertainties` as a full sentence.

Example: an execution flow and the edge it rests on (line numbers illustrative;
cite the lines you read).

```json
{
  "agent": "execution",
  "area": "restock command",
  "nodes": [
    {"id": "fn:app/service.py:InventoryService.restock",
     "description": "Validates quantity, writes the new stock level, notifies on low stock."}
  ],
  "edges": [
    {"from": "fn:app/main.py:main", "to": "fn:app/service.py:InventoryService.restock",
     "kind": "calls", "label": "restock command", "phase": "runtime",
     "evidence": [{"file": "app/main.py", "lines": "41", "symbol": "restock", "status": "confirmed"}]}
  ],
  "flows": [
    {"id": "flow-restock", "name": "Restock command", "kind": "execution", "trigger": "CLI restock",
     "steps": [
       {"from": "fn:app/main.py:main", "to": "fn:app/service.py:InventoryService.restock",
        "label": "restock(sku, qty)", "kind": "call", "phase": "runtime"},
       {"from": "fn:app/service.py:InventoryService.restock", "to": "store:sqlite",
        "label": "UPDATE items", "kind": "io", "phase": "runtime"},
       {"from": "fn:app/service.py:InventoryService.restock", "to": "fn:app/notifier.py:notify",
        "label": "if below threshold", "kind": "decision", "condition": "qty < min_stock", "phase": "runtime"}
     ],
     "evidence": [{"file": "app/service.py", "lines": "20-48", "symbol": "restock", "status": "confirmed"}]}
  ],
  "uncertainties": ["Notifier webhook URL comes from INVENTORY_HOOK at runtime; target host unknown."]
}
```
