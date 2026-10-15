"""Thread-safe token bucket used to stay under external API rate limits."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    """Refills `rate` tokens per second up to `capacity`; `acquire` blocks until a token is free.

    capacity=1 (the default) gives strict spacing of 1/rate seconds, which never exceeds
    `rate` requests in any one-second window. Larger capacities allow bursts.
    """

    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0 or capacity < 1:
            raise ValueError("rate must be > 0 and capacity >= 1")
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                self._sleep((1 - self._tokens) / self.rate)
