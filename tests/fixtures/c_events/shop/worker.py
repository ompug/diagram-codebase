"""Background fulfilment worker fed through an asyncio queue."""

import asyncio


class FulfilmentWorker:
    def __init__(self) -> None:
        self.queue = asyncio.Queue()
        self.running = True

    async def enqueue(self, order_id: str) -> None:
        await self.queue.put(order_id)

    async def run(self) -> None:
        while self.running:
            order_id = await self.queue.get()
            await self.ship(order_id)
            self.queue.task_done()

    async def ship(self, order_id: str) -> None:
        await asyncio.sleep(0.1)

    def stop(self) -> None:
        self.running = False
