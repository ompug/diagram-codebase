"""Tests for regex-based JavaScript/TypeScript analysis."""

from __future__ import annotations

from diagram_codebase.model.builder import ModelBuilder
from diagram_codebase.scan.inventory import LANGUAGES
from diagram_codebase.scan.lang_js import analyze_js
from diagram_codebase.scan.lang_python import analyze_python
from diagram_codebase.scan.patterns_py import PyPatterns


def inv(files):
    out = []
    for p in sorted(files):
        lang = LANGUAGES.get("." + p.rsplit(".", 1)[-1])
        out.append({"path": p, "language": lang, "analyzed": lang is not None})
    return out


def run(make_repo, files, b=None):
    root = make_repo(files)
    b = b or ModelBuilder()
    info = analyze_js(root, inv(files), b)
    return b, info


def edge(b, kind, src, dst):
    return b.edges.get(f"e:{kind}:{src}>{dst}")


def statuses(b, e):
    return {b.evidence[i]["status"] for i in e["evidence_ids"]}


UI = {
    "src/api/index.ts": (
        "export async function fetchTodos() {\n"
        "  const res = await fetch('/api/todos');\n"
        "  return res.json();\n"
        "}\n"
        "\n"
        "export const createTodo = async (title: string) => {\n"
        "  const res = await fetch(`/api/todos`, {\n"
        "    headers: { 'Content-Type': 'application/json' },\n"
        "    body: JSON.stringify({ title }),\n"
        "    method: 'POST',\n"
        "  });\n"
        "  return res.json();\n"
        "};\n"
        "\n"
        "export function fetchOne(id) {\n"
        "  return fetch(`${BASE}/api/todos/${id}`).then((r) => r.json());\n"
        "}\n"
    ),
    "src/App.tsx": (
        "import React from 'react';\n"
        "import { fetchTodos, createTodo as create } from '@/api';\n"
        "import * as api from './api';\n"
        "import { Thing } from '@scope/ui-kit';\n"
        "\n"
        "export function List({ items }) {\n"
        "  return <ul>{items.map((i) => <li>{i}</li>)}</ul>;\n"
        "}\n"
        "\n"
        "export default function App() {\n"
        "  React.useEffect(() => { fetchTodos(); }, []);\n"
        "  const onAdd = () => create('x');\n"
        "  return <List items={[]} />;\n"
        "}\n"
        "\n"
        "export const Header = () => (\n"
        "  <h1>{api.fetchOne(1) && 'x'}</h1>\n"
        ");\n"
        "\n"
        "fetchTodos();\n"
    ),
    "src/index.js": "import App from './App';\nconst lazy = import('./api');\nrender(<App />);\n",
}


def test_modules_definitions_and_imports(make_repo):
    b, info = run(make_repo, UI)
    assert b.nodes["mod:src/App.tsx"]["tags"] == ["ui"]
    assert b.nodes["fn:src/App.tsx:App"]["kind"] == "ui_component"
    assert "exported" in b.nodes["fn:src/App.tsx:App"]["tags"]
    assert b.nodes["fn:src/api/index.ts:createTodo"]["kind"] == "function"
    assert edge(b, "imports", "mod:src/App.tsx", "mod:src/api/index.ts")  # '@/api' and './api'
    assert edge(b, "imports", "mod:src/index.js", "mod:src/App.tsx")
    assert edge(b, "imports", "mod:src/index.js", "mod:src/api/index.ts")  # dynamic import
    assert info["external_packages"] == ["@scope/ui-kit", "react"]


def test_function_spans(make_repo):
    b, _ = run(make_repo, UI)
    assert b.nodes["fn:src/api/index.ts:fetchTodos"]["metadata"]["end_line"] == 4
    assert b.nodes["fn:src/api/index.ts:createTodo"]["metadata"]["end_line"] == 13
    assert b.nodes["fn:src/App.tsx:Header"]["metadata"]["end_line"] == 18


def test_calls_through_imports_are_static_inferred(make_repo):
    b, _ = run(make_repo, UI)
    e = edge(b, "calls", "fn:src/App.tsx:App", "fn:src/api/index.ts:fetchTodos")
    assert e and statuses(b, e) == {"static_inferred"}
    assert edge(b, "calls", "fn:src/App.tsx:App", "fn:src/api/index.ts:createTodo")  # aliased
    assert edge(b, "calls", "fn:src/App.tsx:Header", "fn:src/api/index.ts:fetchOne")  # namespace
    # Module-level call after the last function belongs to the module, not to Header.
    assert edge(b, "calls", "mod:src/App.tsx", "fn:src/api/index.ts:fetchTodos")
    assert edge(b, "calls", "mod:src/index.js", "fn:src/App.tsx:App")["label"] == "renders"
    # Imports alone are not calls.
    assert not edge(b, "calls", "mod:src/App.tsx", "fn:src/api/index.ts:createTodo")


def test_fetch_requests(make_repo):
    b, _ = run(make_repo, UI)
    assert edge(b, "api_request", "fn:src/api/index.ts:fetchTodos", "api:GET /api/todos")
    # method after JSON.stringify(...) in the options object is still found
    assert edge(b, "api_request", "fn:src/api/index.ts:createTodo", "api:POST /api/todos")
    assert not edge(b, "api_request", "fn:src/api/index.ts:createTodo", "api:GET /api/todos")
    assert edge(b, "api_request", "fn:src/api/index.ts:fetchOne", "api:GET /api/todos/{}")


def test_http_clients_external_and_local(make_repo):
    b, _ = run(
        make_repo,
        {
            "c.js": (
                "import axios from 'axios';\n"
                "export function a() { return axios.get('https://api.stripe.com/v1/charges'); }\n"
                "export function b() { return fetch('http://localhost:3000/api/ping'); }\n"
                "export function c() { return axios.delete('/api/items/42'); }\n"
                "export function d(url) { return fetch(url); }\n"
            )
        },
    )
    assert edge(b, "external_call", "fn:c.js:a", "ext:api.stripe.com")
    assert edge(b, "api_request", "fn:c.js:b", "api:GET /api/ping")
    assert edge(b, "api_request", "fn:c.js:c", "api:DELETE /api/items/42")
    assert not [e for e in b.edges.values() if e["from"] == "fn:c.js:d"]


SERVER = {
    "server/handlers.js": (
        "export function listUsers(req, res) { res.json([]); }\n"
        "export function getUser(req, res) { res.json({}); }\n"
    ),
    "server/ctrl.js": "function remove(req, res) {}\nmodule.exports = { remove };\n",
    "server/app.js": (
        "const express = require('express');\n"
        "const ctrl = require('./ctrl');\n"
        "import { listUsers, getUser } from './handlers';\n"
        "const app = express();\n"
        "const api = express.Router();\n"
        "app.get('/users', listUsers);\n"
        "api.get('/users/:id', auth, getUser);\n"
        "app.delete('/users/:id', ctrl.remove);\n"
        "app.post('/users', async (req, res) => {\n"
        "  res.status(201).json({});\n"
        "});\n"
    ),
    "web/client.js": (
        "const instance = makeClient();\n"
        "export function save(body) {\n"
        "  return instance.post('/users', body);\n"
        "}\n"
    ),
}


def test_express_routes(make_repo):
    b, _ = run(make_repo, SERVER)
    assert edge(b, "handles", "api:GET /users", "fn:server/handlers.js:listUsers")
    assert edge(b, "handles", "api:GET /users/{}", "fn:server/handlers.js:getUser")
    assert edge(b, "handles", "api:DELETE /users/{}", "fn:server/ctrl.js:remove")
    inline = edge(b, "handles", "api:POST /users", "mod:server/app.js")
    assert inline and inline["label"] == "inline handler"
    # An HTTP client call with the same shape is not a route declaration.
    assert not [
        e for e in b.edges.values() if e["to"] == "mod:web/client.js" and e["kind"] == "handles"
    ]


def test_nextjs_file_routes(make_repo):
    b, _ = run(
        make_repo,
        {
            "pages/api/users/[id].ts": "export default function handler(req, res) {}\n",
            "app/orders/[orderId]/route.ts": "export async function GET() {}\nexport async function DELETE() {}\n",
        },
    )
    assert edge(b, "handles", "api:ANY /api/users/{}", "mod:pages/api/users/[id].ts")
    assert edge(b, "handles", "api:GET /orders/{}", "mod:app/orders/[orderId]/route.ts")
    assert edge(b, "handles", "api:DELETE /orders/{}", "mod:app/orders/[orderId]/route.ts")


def test_messaging_and_dom_events(make_repo):
    b, _ = run(
        make_repo,
        {
            "m.js": (
                "export function onOrder(o) {}\n"
                "export function wire(bus, button, kafka) {\n"
                "  bus.on('order:created', onOrder);\n"
                "  bus.emit('order:created', {});\n"
                "  button.on('click', () => {});\n"
                "  process.on('SIGTERM', () => {});\n"
                "  kafka.send({ topic: 'audit', messages: [] });\n"
                "}\n"
            )
        },
    )
    assert edge(b, "subscribes", "chan:order:created", "fn:m.js:onOrder")
    assert edge(b, "publishes", "fn:m.js:wire", "chan:order:created")
    assert edge(b, "publishes", "fn:m.js:wire", "chan:audit")
    assert "chan:click" not in b.nodes and "chan:SIGTERM" not in b.nodes


def test_data_access_and_client_state(make_repo):
    b, _ = run(
        make_repo,
        {
            "d.js": (
                "const User = mongoose.model('User', schema);\n"
                "export async function load() { return prisma.order.findMany(); }\n"
                "export async function save() { return prisma.order.create({}); }\n"
                "export async function raw(db) { return db.query('SELECT * FROM invoices'); }\n"
                "const todos = createSlice({ name: 'todos', initialState: [] });\n"
                "export const ThemeContext = React.createContext('light');\n"
            )
        },
    )
    assert b.nodes["ent:User"]["kind"] == "db_entity"
    assert edge(b, "db_read", "fn:d.js:load", "store:prisma-db")["payload"] == ["order"]
    assert edge(b, "db_write", "fn:d.js:save", "store:prisma-db")
    assert edge(b, "db_read", "fn:d.js:raw", "store:sql-database")["payload"] == ["invoices"]
    assert b.nodes["state:todos"]["kind"] == "datastore"
    assert "state:ThemeContext" in b.nodes


def test_fetch_matches_python_endpoint(make_repo):
    files = {
        "backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n\n\n@app.get('/api/todos/{todo_id}')\ndef read_todo(todo_id: int):\n    pass\n",
        "frontend/api.js": "export function getTodo(id) { return fetch(`/api/todos/${id}`); }\n",
    }
    root = make_repo(files)
    b = ModelBuilder()
    idx = analyze_python(root, inv(files), b)
    PyPatterns(idx, b).run()
    analyze_js(root, inv(files), b)
    api = "api:GET /api/todos/{}"
    assert edge(b, "handles", api, "fn:backend/main.py:read_todo")
    assert edge(b, "api_request", "fn:frontend/api.js:getTodo", api)
