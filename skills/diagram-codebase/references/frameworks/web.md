# Web frameworks

Backends (FastAPI, Django, Flask, Starlette, Express, Koa, Fastify, NestJS) and
frontends (React, Next.js, Vue, Svelte, Angular, with Redux/Zustand state).

## Backends: look for

- Routes: FastAPI `@app.get` / `APIRouter` + `include_router(prefix=...)`; Flask
  `@app.route` / blueprints with `url_prefix`; Django `urls.py` `path()` /
  `include()` → views/viewsets; Express/Koa/Fastify `router.get(...)` and
  `app.use("/prefix", router)`; NestJS `@Controller` + `@Get`.
- Middleware and dependencies: FastAPI `Depends`, Django `MIDDLEWARE`, Express
  `app.use(fn)`, auth guards. They run on every request in order: an `init`
  registration plus a `runtime` step in request flows.
- Persistence: ORM queries inside handlers or service layers; migrations define
  the schema (prefer models + migrations for the ERD).
- Startup/shutdown: FastAPI `lifespan`/`on_event`, Django `AppConfig.ready`,
  Express `listen`.
- Background work: FastAPI `BackgroundTasks`, Celery tasks, Django signals.

## Frontends: look for

- Routes/pages: Next.js `app/**/page.tsx` and `pages/**`, React Router route
  tables, Vue Router.
- Data fetching: `fetch`/`axios` calls, React Query/SWR hooks, Next.js server
  components, `getServerSideProps`, route handlers (`app/api/**/route.ts`).
- State: Redux slices/reducers/selectors, Zustand stores, Context providers;
  which components dispatch and which subscribe.
- Server vs client: `"use client"` boundaries, server actions (`"use server"`).

## Map to the model

| Code | Node / edge |
|---|---|
| Route declaration | `api_endpoint` `api:<METHOD> <full path>` (prefixes joined, params → `{}`) |
| Route → handler function | `handles` endpoint → `fn:` |
| Frontend call to own API | `api_request` component/hook → endpoint |
| Page / component | `ui_component` (only those that matter for flows) |
| Client store / slice | `data_artifact` `data:<store>`; `data_flow` dispatcher → store → subscribing component |
| ORM model | `db_entity` `ent:<table>` |
| Middleware chain | flow steps of kind `call`, phase `runtime`, in registration order |

## Pitfalls

- Full path = all router prefixes joined; a bare decorator path is incomplete.
- A frontend `fetch("/api/x")` may hit a proxy or rewrite (`next.config.js`
  rewrites, dev-server proxy); check before linking it to an endpoint.
- Declared routes are not proof of use; client calls are what connect UI to API.
- Next.js API routes and server actions run on the server: they belong to the
  backend side of a flow even when they live in the frontend package.
- Generated API clients (OpenAPI) mirror endpoints; cite the call site, not the
  generated file.
