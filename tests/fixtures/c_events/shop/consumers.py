"""Event consumers reacting to order lifecycle events."""

from .bus import bus
from .tasks import send_receipt


async def reserve_stock(payload: dict) -> None:
    print("reserving stock for", payload["order_id"])


async def email_receipt(payload: dict) -> None:
    send_receipt.delay(payload["order_id"], "customer@example.com")


def register() -> None:
    bus.subscribe("order.created", reserve_stock)
    bus.subscribe("payment.settled", email_receipt)
