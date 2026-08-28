import asyncio
import time


class AsyncRateLimiter:
    """Async token bucket rate limiter."""

    def __init__(self, rate_per_sec: float, max_burst: float = 1.0) -> None:
        self.rate_per_sec = float(rate_per_sec)
        self.capacity = float(max_burst)
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)

            if self.tokens < tokens:
                deficit = tokens - self.tokens
                wait_time = deficit / self.rate_per_sec
                self.tokens = 0.0
                await asyncio.sleep(wait_time)
                self.last_update = time.monotonic()
            else:
                self.tokens -= tokens
