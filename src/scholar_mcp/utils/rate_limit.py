import asyncio
import math
import time


class AsyncRateLimiter:
    """Async token bucket rate limiter with dynamic throttle and backoff."""

    def __init__(self, rate_per_sec: float, max_burst: float = 1.0) -> None:
        rate = float(rate_per_sec)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"rate_per_sec must be positive and finite, got {rate_per_sec}")
        self.rate_per_sec = rate
        self.capacity = float(max_burst)
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self.throttled_until = 0.0
        self._lock = asyncio.Lock()

    def throttle(self, duration: float) -> None:
        """Pause every request on this bucket for ``duration`` seconds.

        Called from the 429 path in ``AsyncHttpClient.get`` so sibling coroutines
        on the same host back off too, not just the one that was rejected.

        Deliberately synchronous, and so deliberately not holding ``_lock``: it
        must be callable from inside a request that is not currently in
        ``acquire``, and taking the lock there would deadlock against a waiter
        already sleeping under it. Because there is no await between the reads
        and the writes below, the update is atomic with respect to the event
        loop. That makes it safe for one event loop only -- do not call it from
        another thread.

        ``last_update`` is pushed forward to ``throttled_until`` on purpose: it
        stops the bucket from accruing tokens during the pause, so the first
        request after the throttle still pays a full token interval instead of
        firing immediately into the host that just rejected us.
        """
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
