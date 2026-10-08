"""Business logic: adding, listing and restocking items."""

from app.forecast import reorder_quantity
from app.notifier import WebhookNotifier
from app.repository import SqliteRepository


class InventoryService:
    def __init__(self, repo: SqliteRepository, notifier: WebhookNotifier) -> None:
        self.repo = repo
        self.notifier = notifier

    def add_item(self, name: str, qty: int) -> None:
        self.repo.add(name, qty)

    def list_items(self):
        return self.repo.all()

    def restock(self) -> int:
        """Top up every item below its reorder point and report it."""
        restocked = 0
        for item in self.repo.all():
            needed = reorder_quantity(item)
            if needed > 0:
                self.repo.set_qty(item.id, item.qty + needed)
                restocked += 1
        if restocked:
            self.notifier.send(f"Restocked {restocked} items")
        return restocked
