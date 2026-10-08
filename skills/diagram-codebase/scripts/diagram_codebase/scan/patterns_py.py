"""Framework-agnostic pattern matchers over Python call records and decorators.

Each matcher turns a recognizable declaration (route decorator, publish call,
DB connection, HTTP client call, env read, thread spawn) into model nodes and
edges with `confirmed` evidence at the declaring line. Matchers only fire on
explicit syntax; anything dynamic is left for Claude's analysis.
"""

from __future__ import annotations

import ast
import re
from typing import Any
from urllib.parse import urlparse

from ..model.builder import ModelBuilder
from .lang_python import CallRecord, PyFile, PyIndex, dotted, literal

HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")
ROUTE_DECORATORS = {*HTTP_METHODS, "route", "api_route", "websocket"}
PUBLISH_ATTRS = {
    "publish",
    "emit",
    "produce",
    "send_event",
    "dispatch_event",
    "basic_publish",
    "xadd",
    "send_message",
    "fire",
}
SUBSCRIBE_ATTRS = {
    "subscribe",
    "add_listener",
    "add_event_handler",
    "on",
    "listen",
    "register_handler",
    "basic_consume",
    "add_handler",
}
HANDLER_DECORATORS = {
    "on",
    "on_event",
    "subscriber",
    "subscribe",
    "consumer",
    "listener",
    "handler",
    "agent",
    "event",
    "receiver",
    "task",
    "shared_task",
}
SPAWN_FUNCS = {
    "threading.Thread": "thread",
    "Thread": "thread",
    "multiprocessing.Process": "process",
    "Process": "process",
}
SPAWN_ATTRS = {
    "create_task": "asyncio task",
    "ensure_future": "asyncio task",
    "run_in_executor": "executor",
    "submit": "executor",
    "start_new_thread": "thread",
    "call_soon": "event loop",
    "call_later": "event loop",
}
EXTERNAL_SDKS = {
    "boto3": "AWS", "botocore": "AWS", "stripe": "Stripe", "openai": "OpenAI", "anthropic": "Anthropic",
    "twilio": "Twilio", "sendgrid": "SendGrid", "slack_sdk": "Slack", "github": "GitHub API",
    "google": "Google Cloud", "azure": "Azure", "firebase_admin": "Firebase", "sentry_sdk": "Sentry",
    "paho": "MQTT broker", "smtplib": "SMTP server",
}  # fmt: skip
HTTP_CLIENT_HEADS = {
    "requests",
    "httpx",
    "aiohttp",
    "urllib.request",
    "urllib3",
    "session",
    "client",
}
DATASTORE_CONNECTORS = {
    "sqlite3.connect": "SQLite", "psycopg2.connect": "PostgreSQL", "psycopg.connect": "PostgreSQL",
    "asyncpg.connect": "PostgreSQL", "asyncpg.create_pool": "PostgreSQL", "pymysql.connect": "MySQL",
    "mysql.connector.connect": "MySQL", "pymongo.MongoClient": "MongoDB", "MongoClient": "MongoDB",
    "motor.motor_asyncio.AsyncIOMotorClient": "MongoDB", "redis.Redis": "Redis", "redis.StrictRedis": "Redis",
    "redis.from_url": "Redis", "Redis": "Redis", "aioredis.from_url": "Redis", "create_engine": "SQL database",
    "sqlalchemy.create_engine": "SQL database", "create_async_engine": "SQL database",
    "elasticsearch.Elasticsearch": "Elasticsearch", "Elasticsearch": "Elasticsearch",
}  # fmt: skip
SCHEME_STORES = {"postgres": "PostgreSQL", "postgresql": "PostgreSQL", "mysql": "MySQL", "sqlite": "SQLite",
                 "mongodb": "MongoDB", "redis": "Redis", "rediss": "Redis"}  # fmt: skip
SESSION_NAMES = {"db", "session", "sess", "db_session", "Session"}
ORM_READ = {"query", "get", "scalars", "scalar", "exec"}
ORM_WRITE = {"add", "add_all", "delete", "merge", "commit", "flush", "bulk_save_objects"}
ORM_BASES = ("Base", "db.Model", "models.Model", "DeclarativeBase", "SQLModel", "Model", "Document")
SQL_READ = re.compile(r"\bSELECT\b[\s\S]+?\bFROM\s+[\"`]?(\w+)", re.I)
SQL_WRITE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"`]?(\w+)", re.I)
SQL_CREATE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?(\w+)[\"`]?\s*\(([\s\S]*?)\)\s*;?\s*$", re.I
)
CONFIG_FILE_RE = re.compile(r"[\w./-]+\.(ya?ml|json|toml|ini|cfg|env)$")


def _first_str(args: list[Any], kwargs: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        v = kwargs.get(k)
        if isinstance(v, str):
            return v
    for a in args:
        if isinstance(a, str):
            return a
    return None


class PyPatterns:
    def __init__(self, idx: PyIndex, b: ModelBuilder) -> None:
        self.idx = idx
        self.b = b
        self.datastores: dict[str, str] = {}  # label -> node id

    def run(self) -> None:
        parsed = [pf for pf in self.idx.files.values() if pf.tree is not None]
        # Pass 1: declarations other matchers depend on (entities, data stores).
        for pf in parsed:
            self._orm_models(pf)
            for rec in pf.calls:
                self._datastore_connect(pf, rec)
        for pf in parsed:
            self._routes(pf)
            for rec in pf.calls:
                self._call_patterns(pf, rec)
            self._handler_decorators(pf)
            self._sql_strings(pf)
            self._env(pf)
            self._django_urls(pf)

    # ------------------------------------------------------------ helpers
    def _ev(
        self,
        pf: PyFile,
        line: int,
        end: int | None = None,
        symbol: str = "",
        detail: str = "",
        status: str = "confirmed",
    ) -> str:
        return self.b.ev(
            pf.path, line, end or line, symbol=symbol, detail=detail, source="ast", status=status
        )

    def _cls_scope(self, rec: CallRecord) -> str | None:
        return rec.__dict__.get("_class_scope")

    def _resolve_head(self, pf: PyFile, func: str) -> str:
        """Map an alias head to its imported module path: `rq.get` -> `requests.get`."""
        head, _, rest = func.partition(".")
        target = pf.imports.get(head)
        if target:
            return f"{target}.{rest}" if rest else target
        return func

    def datastore(self, label: str, ev: str, **meta: Any) -> str:
        nid = self.datastores.get(label)
        if nid is None:
            nid = f"store:{label.lower().replace(' ', '-')}"
            self.datastores[label] = nid
        self.b.node(nid, label, "datastore", evidence_ids=[ev], **meta)
        return nid

    def channel(self, name: str, ev: str, transport: str = "") -> str:
        nid = f"chan:{name}"
        self.b.node(nid, name, "event_channel", evidence_ids=[ev], transport=transport)
        return nid

    # ------------------------------------------------------------ web routes
    def _table_for_class(self, cls_name: str) -> str | None:
        for n in self.b.nodes.values():
            if n["kind"] == "db_entity" and str(n["metadata"].get("model_class", "")).endswith(
                f":{cls_name}"
            ):
                return n["name"]
        return None

    def _routes(self, pf: PyFile) -> None:
        for fid, decos in pf.decorators.items():
            for d in decos:
                attr = d["name"].rsplit(".", 1)[-1]
                if "." not in d["name"] or attr not in ROUTE_DECORATORS:
                    continue
                path = _first_str(d["args"], d["kwargs"], "path", "rule")
                if not path or not path.startswith("/"):
                    continue
                if attr in ("route", "api_route"):
                    methods = d["kwargs"].get("methods") or ["GET"]
                    methods = [m for m in methods if isinstance(m, str)] or ["GET"]
                elif attr == "websocket":
                    methods = ["WS"]
                else:
                    methods = [attr.upper()]
                for m in methods:
                    self.endpoint(
                        pf, m.upper(), path, d["line"], fid, framework=d["name"].split(".")[0]
                    )

    def endpoint(
        self,
        pf: PyFile,
        method: str,
        path: str,
        line: int,
        handler: str | None,
        framework: str = "",
    ) -> str:
        norm = normalize_route(path)
        nid = f"api:{method} {norm}"
        ev = self._ev(pf, line, symbol=f"{method} {path}", detail="route declaration")
        self.b.node(
            nid,
            f"{method} {norm}",
            "api_endpoint",
            evidence_ids=[ev],
            method=method,
            path=norm,
            framework=framework,
        )
        if handler:
            self.b.edge(
                nid, handler, "handles", evidence_ids=[ev], phase="runtime", label="handled by"
            )
        return nid

    def _django_urls(self, pf: PyFile) -> None:
        if not pf.path.endswith("urls.py"):
            return
        for rec in pf.calls:
            if rec.attr in ("path", "re_path", "url") and rec.args:
                route = rec.args[0]
                view = rec.args[1] if len(rec.args) > 1 else None
                if not isinstance(route, str):
                    continue
                handler = None
                if isinstance(view, dict) and view.get("call", "").endswith("as_view"):
                    cls = self.idx.resolve_callable(
                        pf, {"name": view["call"].rsplit(".", 1)[0]}, None
                    )
                    handler = cls
                else:
                    handler = self.idx.resolve_callable(pf, view, None)
                self.endpoint(
                    pf,
                    "ANY",
                    "/" + route.lstrip("^/").rstrip("$"),
                    rec.line,
                    handler,
                    framework="django",
                )

    # ------------------------------------------------------------ data stores
    def _orm_models(self, pf: PyFile) -> None:
        for qual, info in pf.classes.items():
            bases = info["bases"]
            if not any(
                bs in ORM_BASES or bs.endswith((".Model", "DeclarativeBase")) for bs in bases
            ):
                continue
            cls_node = next(
                (
                    n
                    for n in ast.walk(pf.tree)
                    if isinstance(n, ast.ClassDef) and n.lineno == info["line"]
                ),
                None,
            )
            if cls_node is None:
                continue
            table = qual.split(".")[-1]
            attrs = []
            for stmt in cls_node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                ):
                    name = stmt.targets[0].id
                    if name == "__tablename__" and isinstance(stmt.value, ast.Constant):
                        table = str(stmt.value.value)
                        continue
                    if isinstance(stmt.value, ast.Call):
                        attrs.append(_orm_attr(name, stmt.value))
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if isinstance(stmt.value, ast.Call):
                        attrs.append(
                            _orm_attr(stmt.target.id, stmt.value, ast.unparse(stmt.annotation))
                        )
                    elif stmt.value is None and "SQLModel" in bases:
                        attrs.append(
                            {
                                "name": stmt.target.id,
                                "type": ast.unparse(stmt.annotation),
                                "key": "",
                            }
                        )
            attrs = [a for a in attrs if a]
            ev = self._ev(pf, info["line"], symbol=qual, detail="ORM model")
            eid = f"ent:{table}"
            self.b.node(
                eid, table, "db_entity", evidence_ids=[ev], attributes=attrs, model_class=info["id"]
            )
            fks = [a for a in attrs if a.get("references")]
            for fk in fks:
                self.b.nodes[eid]["metadata"].setdefault("foreign_keys", []).append(fk)

    def _datastore_connect(self, pf: PyFile, rec: CallRecord) -> None:
        full = self._resolve_head(pf, rec.func)
        label = DATASTORE_CONNECTORS.get(full) or DATASTORE_CONNECTORS.get(rec.func)
        if not label:
            return
        url = _first_str(rec.args, rec.kwargs, "url", "dsn", "host")
        if url and "://" in url:
            scheme = url.split("://", 1)[0].split("+", 1)[0].lower()
            label = SCHEME_STORES.get(scheme, label)
        elif label == "SQLite" and isinstance(url, str) and url != ":memory:":
            label = "SQLite"
        ev = self._ev(pf, rec.line, rec.end_line, symbol=rec.func, detail=f"{label} connection")
        nid = self.datastore(label, ev)
        self.b.edge(rec.caller, nid, "db_access", evidence_ids=[ev], phase="init", label="connects")

    def _sql_strings(self, pf: PyFile) -> None:
        if not self.datastores and not any(".execute" in r.func for r in pf.calls):
            return
        store = next(iter(self.datastores.values())) if len(self.datastores) == 1 else None
        for text, line, caller in pf.strings:
            m = SQL_CREATE.search(text)
            if m:
                table = m.group(1)
                attrs = _sql_columns(m.group(2))
                ev = self._ev(pf, line, symbol=table, detail="CREATE TABLE")
                self.b.node(f"ent:{table}", table, "db_entity", evidence_ids=[ev], attributes=attrs)
                continue
            reads = (
                SQL_READ.findall(text)
                if text.lstrip().upper().startswith(("SELECT", "WITH"))
                else []
            )
            writes = SQL_WRITE.findall(text)
            if not (reads or writes):
                continue
            target = store or self.datastore(
                "SQL database", self._ev(pf, line, detail="SQL query", status="static_inferred")
            )
            if reads:
                ev = self._ev(pf, line, symbol=",".join(reads), detail="SQL read")
                self.b.edge(
                    caller,
                    target,
                    "db_read",
                    evidence_ids=[ev],
                    payload=reads,
                    label="reads " + ", ".join(sorted(set(reads))),
                    phase="runtime",
                )
            if writes:
                ev = self._ev(pf, line, symbol=",".join(writes), detail="SQL write")
                self.b.edge(
                    caller,
                    target,
                    "db_write",
                    evidence_ids=[ev],
                    payload=writes,
                    label="writes " + ", ".join(sorted(set(writes))),
                    phase="runtime",
                )

    # ------------------------------------------------------------ calls
    def _call_patterns(self, pf: PyFile, rec: CallRecord) -> None:
        full = self._resolve_head(pf, rec.func)
        head = full.split(".")[0]
        cls_scope = self._cls_scope(rec)

        # env/config reads
        if full in ("os.environ.get", "os.getenv", "environ.get", "getenv"):
            key = _first_str(rec.args[:1], {})
            if key:
                pf.env_reads.append((key, rec.line, rec.caller))
            return

        # config files opened by literal name
        if rec.attr in ("open", "load", "safe_load", "read_text", "read") and rec.args:
            name = rec.args[0] if isinstance(rec.args[0], str) else None
            if name and CONFIG_FILE_RE.search(name):
                ev = self._ev(pf, rec.line, symbol=name, detail="config file read")
                nid = f"cfg:file:{name}"
                self.b.node(
                    nid, name.rsplit("/", 1)[-1], "config", evidence_ids=[ev], source="file"
                )
                self.b.edge(
                    rec.caller,
                    nid,
                    "config_dependency",
                    evidence_ids=[ev],
                    phase="init",
                    label="reads config",
                )
                return

        # spawn threads / tasks / processes
        spawn_kind = SPAWN_FUNCS.get(full) or SPAWN_FUNCS.get(rec.func)
        target_val = rec.kwargs.get("target") if spawn_kind else None
        if spawn_kind is None and rec.attr in SPAWN_ATTRS and rec.args:
            spawn_kind = SPAWN_ATTRS[rec.attr]
            first = (
                rec.args[1] if rec.attr == "run_in_executor" and len(rec.args) > 1 else rec.args[0]
            )
            target_val = (
                {"name": first["call"]} if isinstance(first, dict) and "call" in first else first
            )
        if spawn_kind and target_val is not None:
            fn = self.idx.resolve_callable(pf, target_val, cls_scope)
            if fn:
                ev = self._ev(
                    pf, rec.line, rec.end_line, symbol=rec.func, detail=f"spawns {spawn_kind}"
                )
                self.b.edge(
                    rec.caller, fn, "spawns", evidence_ids=[ev], label=spawn_kind, phase="runtime"
                )
                self.b.nodes[fn]["tags"] = sorted(set(self.b.nodes[fn]["tags"]) | {"concurrent"})
            return

        # Celery-style task dispatch: task.delay(...) / task.apply_async(...)
        if rec.attr in ("delay", "apply_async", "send_task"):
            fn = None
            if rec.attr == "send_task":
                name = _first_str(rec.args, {})
                task_name = name or "task"
            else:
                fn = self.idx.resolve_callable(pf, {"name": rec.func.rsplit(".", 1)[0]}, cls_scope)
                task_name = rec.func.rsplit(".", 1)[0].split(".")[-1]
            ev = self._ev(pf, rec.line, rec.end_line, symbol=rec.func, detail="task dispatch")
            chan = self.channel("task queue", ev, transport="celery")
            self.b.edge(
                rec.caller,
                chan,
                "publishes",
                evidence_ids=[ev],
                payload=[task_name],
                label=f"enqueue {task_name}",
                phase="runtime",
            )
            if fn:
                self.b.edge(
                    chan,
                    fn,
                    "triggers",
                    evidence_ids=[ev],
                    payload=[task_name],
                    label="runs task",
                    phase="runtime",
                )
            return

        # pub/sub style messaging with a literal channel name
        if rec.attr in PUBLISH_ATTRS or rec.attr in SUBSCRIBE_ATTRS:
            name = _first_str(
                rec.args[:1],
                rec.kwargs,
                "topic",
                "channel",
                "queue",
                "routing_key",
                "event",
                "subject",
            )
            if not name or len(name) > 80 or " " in name:
                return
            if rec.func.startswith("self.") and rec.func.count(".") == 2 and "ros" in rec.func:
                return
            transport = head if head not in ("self", rec.attr) else ""
            ev = self._ev(
                pf, rec.line, rec.end_line, symbol=rec.func, detail=f"{rec.attr} '{name}'"
            )
            chan = self.channel(name, ev, transport=transport)
            if rec.attr in PUBLISH_ATTRS:
                self.b.edge(
                    rec.caller,
                    chan,
                    "publishes",
                    evidence_ids=[ev],
                    label=f"{rec.attr}",
                    phase="runtime",
                )
            else:
                cb = (
                    rec.args[1]
                    if len(rec.args) > 1
                    else rec.kwargs.get("callback")
                    or rec.kwargs.get("on_message_callback")
                    or rec.kwargs.get("handler")
                )
                fn = self.idx.resolve_callable(pf, cb, cls_scope) if cb is not None else None
                self.b.edge(
                    chan,
                    fn or rec.caller,
                    "subscribes",
                    evidence_ids=[ev],
                    label="delivers to",
                    phase="runtime",
                )
                if fn:
                    self.b.edge(
                        rec.caller,
                        fn,
                        "triggers",
                        evidence_ids=[ev],
                        label=f"registers handler for {name}",
                        phase="init",
                    )
            return

        # asyncio / queue.Queue producers & consumers on a typed attribute
        if rec.attr in ("put", "put_nowait", "get", "get_nowait") and rec.func.count(".") >= 1:
            recv = rec.func.rsplit(".", 1)[0]
            ctor = None
            if recv.startswith("self.") and cls_scope:
                ctor = pf.classes[cls_scope]["attr_types"].get(recv[5:])
            else:
                ctor = next((f for v, f, _ in pf.assigned_calls if v == recv), None)
            if ctor and ctor.split(".")[-1] in (
                "Queue",
                "LifoQueue",
                "PriorityQueue",
                "SimpleQueue",
                "JoinableQueue",
            ):
                qname = recv.split(".")[-1]
                ev = self._ev(
                    pf, rec.line, rec.end_line, symbol=rec.func, detail=f"queue {rec.attr}"
                )
                chan = self.channel(
                    f"{qname} ({ctor.split('.')[0]})" if "." in ctor else qname, ev, transport=ctor
                )
                if rec.attr.startswith("put"):
                    self.b.edge(
                        rec.caller,
                        chan,
                        "publishes",
                        evidence_ids=[ev],
                        label="enqueue",
                        phase="runtime",
                    )
                else:
                    self.b.edge(
                        chan,
                        rec.caller,
                        "subscribes",
                        evidence_ids=[ev],
                        label="dequeue",
                        phase="runtime",
                    )
            return

        # ORM session operations: db.query(Model), session.add(obj), session.commit()
        recv = rec.func.rsplit(".", 1)[0] if "." in rec.func else ""
        if recv.split(".")[-1] in SESSION_NAMES and (rec.attr in ORM_READ or rec.attr in ORM_WRITE):
            tables = []
            for a in rec.args:
                if isinstance(a, dict) and "name" in a:
                    t = self._table_for_class(a["name"].split(".")[-1])
                    if t:
                        tables.append(t)
            ev = self._ev(pf, rec.line, rec.end_line, symbol=rec.func, detail=f"ORM {rec.attr}")
            store = (
                next(iter(self.datastores.values()))
                if len(self.datastores) == 1
                else self.datastore("SQL database", ev)
            )
            write = rec.attr in ORM_WRITE
            label = ("writes " if write else "reads ") + (", ".join(tables) if tables else "rows")
            self.b.edge(
                rec.caller,
                store,
                "db_write" if write else "db_read",
                evidence_ids=[ev],
                payload=tables,
                label=label,
                phase="runtime",
            )
            return

        # external SDKs and HTTP clients
        if head in EXTERNAL_SDKS and rec.target is None and full.count(".") >= 1:
            ev = self._ev(
                pf, rec.line, rec.end_line, symbol=full, detail=f"{EXTERNAL_SDKS[head]} SDK call"
            )
            nid = f"ext:{EXTERNAL_SDKS[head].lower().replace(' ', '-')}"
            self.b.node(nid, EXTERNAL_SDKS[head], "external_service", evidence_ids=[ev], sdk=head)
            self.b.edge(
                rec.caller, nid, "external_call", evidence_ids=[ev], label=rec.attr, phase="runtime"
            )
            return
        if rec.attr in (*HTTP_METHODS, "request", "urlopen", "fetch") and (
            head in HTTP_CLIENT_HEADS
            or full.startswith(("requests.", "httpx.", "aiohttp.", "urllib"))
        ):
            url = _first_str(rec.args, rec.kwargs, "url")
            host = urlparse(url).hostname if url and "://" in url else None
            if host in ("localhost", "127.0.0.1", "0.0.0.0"):
                host = None
            path = (
                urlparse(url).path
                if url and "://" in url
                else (url if url and url.startswith("/") else None)
            )
            method = (
                rec.attr if rec.attr in HTTP_METHODS else str(rec.kwargs.get("method", "GET"))
            ).upper()
            ev = self._ev(pf, rec.line, rec.end_line, symbol=rec.func, detail=f"HTTP {method}")
            if host:
                nid = f"ext:{host}"
                self.b.node(nid, host, "external_service", evidence_ids=[ev], protocol="http")
                self.b.edge(
                    rec.caller,
                    nid,
                    "external_call",
                    evidence_ids=[ev],
                    label=f"{method} {urlparse(url).path or '/'}"[:40],
                    phase="runtime",
                )
            elif path:
                api = f"api:{method} {normalize_route(path)}"
                self.b.node(
                    api,
                    f"{method} {normalize_route(path)}",
                    "api_endpoint",
                    evidence_ids=[ev],
                    method=method,
                    path=normalize_route(path),
                )
                self.b.edge(
                    rec.caller, api, "api_request", evidence_ids=[ev], label=method, phase="runtime"
                )

    def _handler_decorators(self, pf: PyFile) -> None:
        for fid, decos in pf.decorators.items():
            for d in decos:
                parts = d["name"].split(".")
                attr = parts[-1]
                if attr not in HANDLER_DECORATORS or (attr in ROUTE_DECORATORS):
                    continue
                if attr in ("task", "shared_task"):
                    self.b.nodes[fid]["tags"] = sorted(
                        set(self.b.nodes[fid]["tags"]) | {"background-task"}
                    )
                    continue
                name = _first_str(
                    d["args"], d["kwargs"], "topic", "event", "channel", "queue", "subject"
                )
                if not name:
                    topic = d["args"][0] if d["args"] else None
                    name = topic["name"] if isinstance(topic, dict) and "name" in topic else None
                if not name:
                    continue
                ev = self._ev(pf, d["line"], symbol=d["name"], detail=f"handler for '{name}'")
                chan = self.channel(name, ev, transport=parts[0])
                self.b.edge(
                    chan, fid, "subscribes", evidence_ids=[ev], label="delivers to", phase="runtime"
                )

    def _env(self, pf: PyFile) -> None:
        for key, line, caller in pf.env_reads:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", key):
                continue
            ev = self._ev(pf, line, symbol=key, detail="environment variable read")
            nid = f"cfg:env:{key}"
            self.b.node(nid, key, "config", evidence_ids=[ev], source="env")
            self.b.edge(
                caller, nid, "config_dependency", evidence_ids=[ev], label="reads env", phase="init"
            )


def normalize_route(path: str) -> str:
    """`/users/{id}`, `/users/<int:id>`, `/users/:id`, `/users/${id}` -> `/users/{}`."""
    p = path.split("?", 1)[0]
    p = re.sub(r"\{[^}]*\}|<[^>]*>|:\w+|\$\{[^}]*\}", "{}", p)
    p = re.sub(r"/+", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p if p.startswith("/") else "/" + p


def _orm_attr(name: str, call: ast.Call, annotation: str = "") -> dict[str, Any] | None:
    func = dotted(call.func)
    short = func.rsplit(".", 1)[-1]
    if short in ("relationship", "backref", "ManyToManyField"):
        return None
    if not (
        short in ("Column", "mapped_column", "Field")
        or short.endswith("Field")
        or short == "ForeignKey"
    ):
        return None
    typ = annotation
    key = ""
    ref = None
    for a in call.args:
        text = dotted(a.func) if isinstance(a, ast.Call) else dotted(a)
        if text.endswith("ForeignKey") and isinstance(a, ast.Call) and a.args:
            ref = literal(a.args[0])
            key = "FK"
        elif text and not typ:
            typ = text.rsplit(".", 1)[-1]
    if short.endswith("Field") and short != "Field":
        typ = typ or short.replace("Field", "")
        if short == "ForeignKey" or short.startswith("ForeignKey"):
            key = "FK"
            ref = literal(call.args[0]) if call.args else None
    for k in call.keywords:
        if k.arg == "primary_key" and isinstance(k.value, ast.Constant) and k.value.value:
            key = "PK"
    out = {"name": name, "type": (typ or "value").split("[")[0], "key": key}
    if isinstance(ref, str):
        out["references"] = ref.split(".")[0]
    elif isinstance(ref, dict) and "name" in ref:
        out["references"] = ref["name"].split(".")[-1]
    return out


def _sql_columns(body: str) -> list[dict[str, Any]]:
    cols = []
    for part in re.split(r",(?![^(]*\))", body):
        part = part.strip()
        m = re.match(r"[\"`]?(\w+)[\"`]?\s+(\w+)", part)
        if not m or m.group(1).upper() in (
            "PRIMARY",
            "FOREIGN",
            "UNIQUE",
            "CONSTRAINT",
            "CHECK",
            "KEY",
            "INDEX",
        ):
            fk = re.match(r"FOREIGN\s+KEY\s*\((\w+)\)\s*REFERENCES\s+(\w+)", part, re.I)
            if fk:
                for c in cols:
                    if c["name"] == fk.group(1):
                        c["key"] = "FK"
                        c["references"] = fk.group(2)
            continue
        col = {
            "name": m.group(1),
            "type": m.group(2).lower(),
            "key": "PK" if "PRIMARY KEY" in part.upper() else "",
        }
        ref = re.search(r"REFERENCES\s+(\w+)", part, re.I)
        if ref:
            col["key"] = col["key"] or "FK"
            col["references"] = ref.group(1)
        cols.append(col)
    return cols
