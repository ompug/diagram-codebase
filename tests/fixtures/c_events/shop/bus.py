"""In-process asynchronous publish/subscribe bus."""

import asyncio
from collections import defaultdict


class EventBus:
    def __init__(self) -> None:
        self._handlers = defaultdict(list)

    def subscribe(self, topic: str, handler) -> None:
        self._handlers[topic].append(handler)

    async def publish(self, topic: str, payload: dict) -> None:
        for handler in self._handlers[topic]:
            await handler(payload)


bus = EventBus()
