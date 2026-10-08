"""Module A calls into B, which calls back into A."""

import b


def ping(n: int) -> int:
    if n <= 0:
        return 0
    return b.pong(n - 1)
