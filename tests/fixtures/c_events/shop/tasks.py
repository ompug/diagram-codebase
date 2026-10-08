"""Celery tasks executed by a separate worker process."""

from celery import Celery

app = Celery("shop", broker="redis://redis:6379/0")


@app.task
def send_receipt(order_id: str, email: str) -> None:
    print(f"receipt for {order_id} sent to {email}")
