# Python

## Look for

- Entry points: `if __name__ == "__main__":`, `[project.scripts]` /
  `console_scripts` in `pyproject.toml`/`setup.cfg`, `__main__.py`, CLI
  frameworks (`argparse` subparsers, `click`/`typer` commands), `manage.py`.
- Wiring: module-level singletons, factory functions, dependency-injection
  containers, registries filled by decorators (`@register`, `@app.task`).
- Async: `asyncio.run`, `create_task`, `gather`, queues (`asyncio.Queue`,
  `queue.Queue`, `multiprocessing`), `ThreadPoolExecutor`, signal handlers.
- Data and state: ORM models (SQLAlchemy `declarative_base`, Django `models.Model`,
  Pydantic/dataclasses as payloads), `open()`/`pathlib` I/O, `sqlite3`, `requests`/
  `httpx` calls.
- Termination: `atexit`, `signal.signal`, context managers around the main loop,
  `sys.exit` paths.

## Map to the model

| Code | Node / edge |
|---|---|
| `main()` / CLI command | existing `fn:` node tagged entrypoint; `calls` edges to what it runs |
| Long-running process (server, daemon, worker loop) | `service` or `worker` |
| `requests.post(url)` / SDK client | `external_service` `ext:<host>` + `external_call` |
| DB session query / `cursor.execute` | `datastore` `store:<engine>` + `db_read` / `db_write` |
| ORM model class | `db_entity` `ent:<table>` + ERD entity |
| `os.environ["X"]` / settings object | `config` `cfg:env:X` + `config_dependency` to the reader |
| `threading.Thread(target=f)` / `create_task(f())` | `spawns` (phase `init` or `runtime`) |

## Pitfalls

- Decorator registries hide the call graph: find the dispatcher that reads the
  registry, cite it, and mark resolved targets `static_inferred`.
- `getattr`, `importlib.import_module`, entry-point plugins: dynamic; record the
  dispatch point and an uncertainty.
- Module-level code runs at import time: that is `init`, even without a function.
- Monkeypatching in tests and `conftest.py` fixtures are not production wiring.
- Type hints name a class; they do not prove which implementation is passed in.
