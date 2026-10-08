# Node.js / TypeScript

## Look for

- Entry points: `package.json` `main`, `bin`, `scripts.start`/`dev`, `exports`;
  `src/index.ts`, `server.ts`; framework CLIs (`next`, `nest start`, `vite`).
- Workspaces: `workspaces` in `package.json`, `pnpm-workspace.yaml`, `turbo.json`,
  `nx.json`; each package is a subsystem candidate.
- Wiring: module-level instances exported as singletons, DI containers
  (NestJS modules/providers, `tsyringe`, `inversify`), `app.use` chains.
- Async: promises chained across modules, `EventEmitter` `.on`/`.emit`,
  `setInterval`/`setTimeout`, worker threads, `child_process`, queue clients
  (BullMQ, kafkajs, amqplib).
- I/O: `fetch`/`axios`/SDK clients, ORMs (Prisma schema, TypeORM entities,
  Sequelize, Mongoose models), `fs` calls.

## Map to the model

| Code | Node / edge |
|---|---|
| Package in a workspace | `package` node; `depends_on` from its `package.json` deps |
| `emitter.emit("x")` / `.on("x", h)` | `event_channel` `chan:x` + `publishes` / `subscribes` |
| `fetch(url)` to own API | `api_request` client → `api:<METHOD> <path>` |
| `fetch` to third party | `ext:<host>` + `external_call` |
| `prisma.user.findMany` | `store:<db>` + `db_read`; Prisma models → `ent:` entities |
| `new Worker(file)` / `fork` | `spawns` |
| `process.env.X` | `cfg:env:X` + `config_dependency` |

## Pitfalls

- Barrel files (`index.ts` re-exports) make imports look like dependencies on
  everything; trace to the actual definition.
- `import type` is erased at compile time: never a runtime dependency.
- Path aliases (`tsconfig.json` `paths`, bundler aliases) change what an import
  resolves to; read the config before citing a target.
- Build output (`dist/`, `.next/`) is not source; cite `src/`.
- Promise chains without `await` may run after the caller returns: model as
  `async`, not `call`.
