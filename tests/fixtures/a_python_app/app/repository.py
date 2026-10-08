"""SQLite persistence for inventory items."""

import sqlite3

from app.models import Item

SCHEMA = """CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    qty INTEGER NOT NULL
)"""


class SqliteRepository:
    """Stores items in a local SQLite file."""

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute(SCHEMA)

    def add(self, name: str, qty: int) -> None:
        self.conn.execute("INSERT INTO items (name, qty) VALUES (?, ?)", (name, qty))
        self.conn.commit()

    def all(self) -> list[Item]:
        rows = self.conn.execute("SELECT id, name, qty FROM items").fetchall()
        return [Item(*row) for row in rows]

    def set_qty(self, item_id: int, qty: int) -> None:
        self.conn.execute("UPDATE items SET qty = ? WHERE id = ?", (qty, item_id))
        self.conn.commit()
