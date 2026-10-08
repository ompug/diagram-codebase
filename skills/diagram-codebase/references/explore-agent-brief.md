# Explore agent brief

Template for each Explore subagent in Phase 3. Fill the `<...>` slots, paste the
output of `dc.py findings-template` where marked, and launch all agents in one
message. Each agent covers one area; the main session writes and checks the files.

Area choices: `execution` (entry points → runtime paths, init vs runtime,
errors, termination), `dataflow` (data in → transforms → storage/out, with
representations), `algorithms` (stage/decision structure of core computations),
`infrastructure` (build, deployment units, config sources, external services),
and `sub-<slug>` (one per large subsystem: all of the above, scoped to its paths).

---

```text
You are analyzing the repository at <repo root> for an architecture diagram.
Area: <area slug> — <one-line goal, e.g. "trace every runtime path from the
entry points below to its effects">.
Scope: <paths or subsystem; "whole repo" for cross-cutting areas>.
Context from the scan:
- Languages/frameworks: <from brief>
- Subsystems: <id: name (paths)>
- Entry points: <ids from brief>
- Hubs: <ids from brief>
<user's focus/hint, if any>

Rules:
1. Read-only. Do not create, edit, or run anything in the repository. Use
   Read, Grep, Glob only.
2. Trace what runs, starting from the entry points in scope. Follow a call
   only where you see the call site. Imports are not calls; config is not
   execution; a declared handler is not proof it is invoked; tests are not
   production wiring.
3. Every new node, edge, flow, dataflow, algorithm, state machine, and ERD
   entity carries evidence: {"file": repo-relative path, "lines": "N" or
   "N-M", "symbol": a name that appears within 3 lines of that range,
   "status": ...}. Cite lines you actually read.
   - confirmed: the cited lines directly state it (the call, publish, route,
     query).
   - static_inferred: you reasoned it (resolved through a registry, type,
     convention, or call chain).
   - documented_only: only docs/comments say it.
   When unsure, use the weaker status. A claim you cannot cite goes into
   "uncertainties" as a sentence, not into nodes/edges.
4. Reuse existing ids for scanned things: mod:<path>, cls:<path>:<Qual>,
   fn:<path>:<Qual.method>. New concepts use the prefixes in the template
   (algo:, data:, chan:, store:, ext:, cfg:env:, sub:...). Tag edges and flow
   steps with phase: init | runtime | shutdown | error | build | unknown.
5. For algorithms: 3–12 stages, decisions with labelled branches, loops with
   a labelled back-edge, each stage with its own evidence.

Output: your final message is ONLY one JSON object in this shape (no prose
before or after, no code fence), with "agent": "<area slug>":

<paste dc.py findings-template output here>

Done when every entry point in scope is traced to its effects or named in
"uncertainties" with the reason it could not be traced.
```
