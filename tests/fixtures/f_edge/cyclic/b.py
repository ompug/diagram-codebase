"""Module B calls back into A."""

import a


def pong(n: int) -> int:
    return a.ping(n - 1) + 1
