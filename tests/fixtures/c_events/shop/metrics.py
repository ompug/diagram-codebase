"""Periodic metrics reporting on a background thread."""

import time

STOP = False


def metrics_loop() -> None:
    while not STOP:
        time.sleep(10)
