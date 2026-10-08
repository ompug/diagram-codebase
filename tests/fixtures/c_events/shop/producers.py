"""Event producers: checkout and payment gateway callbacks."""

import uuid

from .bus import EventBus


class Checkout:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    async def place_order(self, cart: dict) -> str:
        order_id = str(uuid.uuid4())
        await self.bus.publish("order.created", {"order_id": order_id, "total": cart["total"]})
        return order_id


async def on_gateway_callback(bus: EventBus, order_id: str) -> None:
    await bus.publish("payment.settled", {"order_id": order_id})
