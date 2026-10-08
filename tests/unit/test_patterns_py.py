"""Tests for Python framework pattern matchers."""

from __future__ import annotations

import pytest
from diagram_codebase.model.builder import ModelBuilder
from diagram_codebase.scan.lang_python import analyze_python
from diagram_codebase.scan.patterns_py import PyPatterns, normalize_route


def run(make_repo, files):
    root = make_repo(files)
    inv = [{"path": p, "language": "python", "analyzed": True} for p in sorted(files)]
    b = ModelBuilder()
    idx = analyze_python(root, inv, b)
    PyPatterns(idx, b).run()
    return b


def edge(b, kind, src, dst):
    return b.edges.get(f"e:{kind}:{src}>{dst}")


def statuses(b, e):
    return {b.evidence[i]["status"] for i in e["evidence_ids"]}


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("/users/{user_id}", "/users/{}"),
        ("/users/<int:id>/", "/users/{}"),
        ("/users/:id", "/users/{}"),
        ("/users/${id}?x=1", "/users/{}"),
        ("users//list", "/users/list"),
        ("/", "/"),
    ],
)
def test_normalize_route(raw, norm):
    assert normalize_route(raw) == norm


def test_fastapi_flask_routes(make_repo):
    b = run(
        make_repo,
        {
            "api.py": (
                "from fastapi import APIRouter, FastAPI\n"
                "app = FastAPI()\n"
                "router = APIRouter()\n"
                "PREFIX = '/x'\n"
                "\n"
                "@app.get('/items/{item_id}')\n"
                "def read(item_id: int):\n"
                "    pass\n"
                "\n"
                "@router.post(path='/items')\n"
                "async def create():\n"
                "    pass\n"
                "\n"
                "@app.route('/legacy', methods=['GET', 'PUT'])\n"
                "def legacy():\n"
                "    pass\n"
                "\n"
                "@app.websocket('/ws')\n"
                "def ws():\n"
                "    pass\n"
                "\n"
                "@app.get(PREFIX + '/dynamic')\n"
                "def dynamic():\n"
                "    pass\n"
                "\n"
                "@get('/bare')\n"
                "def bare():\n"
                "    pass\n"
            )
        },
    )
    apis = {n for n in b.nodes if n.startswith("api:")}
    assert apis == {
        "api:GET /items/{}",
        "api:POST /items",
        "api:GET /legacy",
        "api:PUT /legacy",
        "api:WS /ws",
    }
    e = edge(b, "handles", "api:GET /items/{}", "fn:api.py:read")
    assert e and statuses(b, e) == {"confirmed"} and e["phase"] == "runtime"
    assert edge(b, "handles", "api:POST /items", "fn:api.py:create")
    assert b.nodes["api:GET /items/{}"]["metadata"]["framework"] == "app"


def test_django_urls(make_repo):
    b = run(
        make_repo,
        {
            "shop/__init__.py": "",
            "shop/views.py": "def index(request):\n    pass\n\n\nclass ItemView:\n    pass\n",
            "shop/urls.py": (
                "from django.urls import path\n"
                "from . import views\n"
                "urlpatterns = [\n"
                "    path('', views.index),\n"
                "    path('items/<int:pk>/', views.ItemView.as_view()),\n"
                "]\n"
            ),
        },
    )
    assert edge(b, "handles", "api:ANY /", "fn:shop/views.py:index")
    assert edge(b, "handles", "api:ANY /items/{}", "cls:shop/views.py:ItemView")


MODELS = (
    "from sqlalchemy import Column, ForeignKey, Integer, String\n"
    "from sqlalchemy.orm import relationship\n"
    "from .db import Base\n"
    "\n"
    "class User(Base):\n"
    "    __tablename__ = 'users'\n"
    "    id = Column(Integer, primary_key=True)\n"
    "    email = Column(String)\n"
    "    todos = relationship('Todo')\n"
    "\n"
    "class Todo(Base):\n"
    "    __tablename__ = 'todos'\n"
    "    id = Column(Integer, primary_key=True)\n"
    "    owner_id = Column(Integer, ForeignKey('users.id'))\n"
    "\n"
    "class Plain:\n"
    "    id = Column(Integer)\n"
)


def test_orm_models_and_session_operations(make_repo):
    b = run(
        make_repo,
        {
            "app/__init__.py": "",
            "app/db.py": "from sqlalchemy import create_engine\nengine = create_engine('postgresql+psycopg2://u:FAKE_PASSWORD@db/x')\nBase = object\n",
            "app/models.py": MODELS,
            "app/crud.py": (
                "from . import models\n"
                "\n"
                "def list_todos(db):\n"
                "    return db.query(models.Todo).all()\n"
                "\n"
                "def add(db, title):\n"
                "    todo = models.Todo(title=title)\n"
                "    db.add(todo)\n"
                "    db.commit()\n"
                "\n"
                "def unrelated(cache):\n"
                "    cache.add(1)\n"
            ),
        },
    )
    users, todos = b.nodes["ent:users"], b.nodes["ent:todos"]
    assert users["metadata"]["model_class"] == "cls:app/models.py:User"
    assert users["metadata"]["attributes"] == [
        {"name": "id", "type": "Integer", "key": "PK"},
        {"name": "email", "type": "String", "key": ""},
    ]
    fk = next(a for a in todos["metadata"]["attributes"] if a["name"] == "owner_id")
    assert fk == {"name": "owner_id", "type": "Integer", "key": "FK", "references": "users"}
    assert "ent:Plain" not in b.nodes
    store = "store:postgresql"
    assert b.nodes[store]["kind"] == "datastore"
    assert edge(b, "db_access", "mod:app/db.py", store)["phase"] == "init"
    read = edge(b, "db_read", "fn:app/crud.py:list_todos", store)
    assert read["payload"] == ["todos"] and read["label"] == "reads todos"
    write = edge(b, "db_write", "fn:app/crud.py:add", store)
    assert write["payload"] == ["todos"] and len(write["evidence_ids"]) == 2
    assert not [e for e in b.edges.values() if e["from"] == "fn:app/crud.py:unrelated"]


def test_sqlmodel_and_django_models(make_repo):
    b = run(
        make_repo,
        {
            "m.py": (
                "from sqlmodel import Field, SQLModel\n"
                "from django.db import models\n"
                "\n"
                "class Hero(SQLModel):\n"
                "    id: int = Field(primary_key=True)\n"
                "    name: str\n"
                "\n"
                "class Order(models.Model):\n"
                "    customer = models.ForeignKey('Customer', on_delete=models.CASCADE)\n"
                "    total = models.DecimalField()\n"
            )
        },
    )
    assert b.nodes["ent:Hero"]["metadata"]["attributes"] == [
        {"name": "id", "type": "int", "key": "PK"},
        {"name": "name", "type": "str", "key": ""},
    ]
    attrs = {a["name"]: a for a in b.nodes["ent:Order"]["metadata"]["attributes"]}
    assert attrs["customer"]["key"] == "FK" and attrs["customer"]["references"] == "Customer"
    assert attrs["total"]["type"] == "Decimal"


def test_raw_sql_and_connectors(make_repo):
    b = run(
        make_repo,
        {
            "r.py": (
                "import sqlite3\n"
                "import redis\n"
                "SCHEMA = '''CREATE TABLE IF NOT EXISTS items (\n"
                "  id INTEGER PRIMARY KEY,\n"
                "  owner INTEGER REFERENCES users(id),\n"
                "  FOREIGN KEY (owner) REFERENCES users(id)\n"
                ")'''\n"
                "\n"
                "def connect():\n"
                "    return sqlite3.connect('x.db')\n"
                "\n"
                "def cache():\n"
                "    return redis.Redis(host='cache')\n"
                "\n"
                "def q(conn):\n"
                "    conn.execute('SELECT id FROM items')\n"
                "    conn.execute('DELETE FROM items WHERE id = 1')\n"
            )
        },
    )
    assert b.nodes["ent:items"]["metadata"]["attributes"][0] == {
        "name": "id",
        "type": "integer",
        "key": "PK",
    }
    assert b.nodes["ent:items"]["metadata"]["attributes"][1]["references"] == "users"
    assert edge(b, "db_access", "fn:r.py:connect", "store:sqlite")
    assert edge(b, "db_access", "fn:r.py:cache", "store:redis")
    # Two stores -> SQL strings go to a generic SQL database node, never guessed.
    assert edge(b, "db_read", "fn:r.py:q", "store:sql-database")["payload"] == ["items"]
    assert edge(b, "db_write", "fn:r.py:q", "store:sql-database")


def test_env_and_config_files(make_repo):
    b = run(
        make_repo,
        {
            "c.py": (
                "import os\n"
                "from os import getenv\n"
                "import yaml\n"
                "\n"
                "def load():\n"
                "    a = os.environ.get('DB_URL')\n"
                "    b = getenv('API_TOKEN', 'sk_test_FAKE')\n"
                "    c = os.environ['REGION']\n"
                "    d = os.getenv('lowercase')\n"
                "    with open('settings/app.yaml') as fh:\n"
                "        return yaml.safe_load(fh)\n"
            )
        },
    )
    cfgs = {n for n in b.nodes if n.startswith("cfg:")}
    assert cfgs == {
        "cfg:env:DB_URL",
        "cfg:env:API_TOKEN",
        "cfg:env:REGION",
        "cfg:file:settings/app.yaml",
    }
    assert b.nodes["cfg:file:settings/app.yaml"]["name"] == "app.yaml"
    assert edge(b, "config_dependency", "fn:c.py:load", "cfg:env:API_TOKEN")["label"] == "reads env"
    # The fake default secret never becomes model metadata.
    assert "sk_test_FAKE" not in repr(b.to_fragment())


def test_http_clients_and_sdks(make_repo):
    b = run(
        make_repo,
        {
            "h.py": (
                "import boto3\n"
                "import httpx\n"
                "import requests as rq\n"
                "\n"
                "def ext():\n"
                "    rq.get('https://api.example.com/v1/rates', timeout=3)\n"
                "\n"
                "def internal():\n"
                "    httpx.post('/api/orders/{id}', json={})\n"
                "    requests_like = 1\n"
                "\n"
                "def local():\n"
                "    rq.get('http://localhost:8000/health')\n"
                "\n"
                "def s3():\n"
                "    boto3.client('s3').put_object(Bucket='b')\n"
            )
        },
    )
    e = edge(b, "external_call", "fn:h.py:ext", "ext:api.example.com")
    assert e and e["label"] == "GET /v1/rates"
    assert edge(b, "api_request", "fn:h.py:internal", "api:POST /api/orders/{}")
    assert edge(b, "api_request", "fn:h.py:local", "api:GET /health")
    assert edge(b, "external_call", "fn:h.py:s3", "ext:aws")


def test_pubsub_queues_spawns_signals(make_repo):
    b = run(
        make_repo,
        {
            "ev.py": (
                "import asyncio\n"
                "import queue\n"
                "import signal\n"
                "import threading\n"
                "from concurrent.futures import ThreadPoolExecutor\n"
                "\n"
                "jobs = queue.Queue()\n"
                "\n"
                "class Bus:\n"
                "    def publish(self, topic, payload):\n"
                "        pass\n"
                "\n"
                "    def subscribe(self, topic, handler):\n"
                "        pass\n"
                "\n"
                "bus = Bus()\n"
                "\n"
                "def on_created(payload):\n"
                "    pass\n"
                "\n"
                "def wire(name):\n"
                "    bus.subscribe('order.created', on_created)\n"
                "    bus.publish('order.created', {})\n"
                "    bus.publish(name, {})\n"
                "    bus.publish('not a topic', {})\n"
                "\n"
                "class Worker:\n"
                "    def __init__(self):\n"
                "        self.q = asyncio.Queue()\n"
                "\n"
                "    async def produce(self):\n"
                "        await self.q.put(1)\n"
                "\n"
                "    async def consume(self):\n"
                "        await self.q.get()\n"
                "\n"
                "    def stop(self, *_):\n"
                "        pass\n"
                "\n"
                "def loop_fn():\n"
                "    jobs.put(1)\n"
                "\n"
                "async def main():\n"
                "    w = Worker()\n"
                "    asyncio.create_task(w.consume())\n"
                "    threading.Thread(target=loop_fn, daemon=True).start()\n"
                "    ThreadPoolExecutor().submit(loop_fn)\n"
                "    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, w.stop)\n"
                "    signal.signal(signal.SIGHUP, on_created)\n"
                "    signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
            )
        },
    )
    chan = "chan:order.created"
    assert edge(b, "publishes", "fn:ev.py:wire", chan)
    assert edge(b, "subscribes", chan, "fn:ev.py:on_created")
    assert edge(b, "triggers", "fn:ev.py:wire", "fn:ev.py:on_created")["phase"] == "init"
    assert "chan:not a topic" not in b.nodes
    assert edge(b, "publishes", "fn:ev.py:Worker.produce", "chan:Worker.q")
    assert edge(b, "subscribes", "chan:Worker.q", "fn:ev.py:Worker.consume")
    assert b.nodes["chan:Worker.q"]["metadata"]["transport"] == "asyncio.Queue"
    assert edge(b, "publishes", "fn:ev.py:loop_fn", "chan:ev.jobs")
    task = edge(b, "spawns", "fn:ev.py:main", "fn:ev.py:Worker.consume")
    assert task["label"] == "asyncio task" and statuses(b, task) == {"static_inferred"}
    thread = edge(b, "spawns", "fn:ev.py:main", "fn:ev.py:loop_fn")
    assert statuses(b, thread) == {"confirmed"}
    assert "concurrent" in b.nodes["fn:ev.py:loop_fn"]["tags"]
    term = edge(b, "triggers", "chan:SIGTERM", "fn:ev.py:Worker.stop")
    assert term["phase"] == "shutdown" and statuses(b, term) == {"static_inferred"}
    assert edge(b, "triggers", "chan:SIGHUP", "fn:ev.py:on_created")["phase"] == "runtime"
    assert "chan:SIGINT" not in b.nodes  # SIG_DFL is not a repository handler


def test_celery_tasks_and_handler_decorators(make_repo):
    b = run(
        make_repo,
        {
            "tasks.py": (
                "from celery import Celery\n"
                "app = Celery('x')\n"
                "\n"
                "@app.task\n"
                "def send(x):\n"
                "    pass\n"
                "\n"
                "@app.task(bind=True)\n"
                "def other(self):\n"
                "    pass\n"
            ),
            "use.py": (
                "from tasks import send, app\n"
                "from faust_like import agent\n"
                "\n"
                "def go():\n"
                "    send.delay(1)\n"
                "    app.send_task('reports.build')\n"
                "\n"
                "@agent('clicks')\n"
                "def on_click(stream):\n"
                "    pass\n"
            ),
        },
    )
    assert "background-task" in b.nodes["fn:tasks.py:send"]["tags"]
    assert "background-task" in b.nodes["fn:tasks.py:other"]["tags"]
    pub = edge(b, "publishes", "fn:use.py:go", "chan:task queue")
    assert pub["payload"] == ["reports.build", "send"]
    assert edge(b, "triggers", "chan:task queue", "fn:tasks.py:send")
    assert edge(b, "subscribes", "chan:clicks", "fn:use.py:on_click")
