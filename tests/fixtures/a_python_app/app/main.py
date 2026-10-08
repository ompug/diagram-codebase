"""Command-line entry point for the inventory tool."""

import argparse
import os

from app.notifier import WebhookNotifier
from app.repository import SqliteRepository
from app.service import InventoryService


def build_service() -> InventoryService:
    db_path = os.environ.get("INVENTORY_DB", "inventory.db")
    repo = SqliteRepository(db_path)
    notifier = WebhookNotifier()
    return InventoryService(repo, notifier)


def main() -> int:
    parser = argparse.ArgumentParser(prog="inventory")
    parser.add_argument("command", choices=["add", "restock", "list"])
    parser.add_argument("--name")
    parser.add_argument("--qty", type=int, default=1)
    args = parser.parse_args()
    service = build_service()
    if args.command == "add":
        service.add_item(args.name, args.qty)
    elif args.command == "restock":
        service.restock()
    else:
        for item in service.list_items():
            print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
