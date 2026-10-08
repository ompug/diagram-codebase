"""Service entry point: wires the bus, worker and background threads."""

import asyncio
import signal
import threading

from . import consumers
from .bus import bus
from .metrics import metrics_loop
from .producers import Checkout
from .worker import FulfilmentWorker


async def run() -> None:
    worker = FulfilmentWorker()
    consumers.register()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, worker.stop)
    worker_task = asyncio.create_task(worker.run())
    checkout = Checkout(bus)
    order_id = await checkout.place_order({"total": 42})
    await worker.enqueue(order_id)
    await worker_task


def shutdown(signum, frame) -> None:
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=metrics_loop, daemon=True).start()
    asyncio.run(run())


if __name__ == "__main__":
    main()
