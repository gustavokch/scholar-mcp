import asyncio
import time


class AsyncRateLimiter:
    """Async token bucket rate limiter with dynamic throttle and backoff."""

    def __init__(self, rate_per_sec: float, max_burst: float = 1.0) -> None:
        self.rate_per_sec = float(rate_per_sec)
        self.capacity = float(max_burst)
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self.throttled_until = 0.0
        self._lock = asyncio.Lock()

    def throttle(self, duration: float) -> None:
        """Dynamically throttle all subsequent requests for ``duration`` seconds."""
        now = time.monotonic()
        self.throttled_until = max(self.throttled_until, now + max(0.0, duration))
        self.tokens = 0.0
        self.last_update = max(self.last_update, self.throttled_until)

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            now = time.monotonic()
            if self.throttled_until > now:
                await asyncio.sleep(self.throttled_until - now)
                now = time.monotonic()

            elapsed = max(0.0, now - self.last_update)
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
