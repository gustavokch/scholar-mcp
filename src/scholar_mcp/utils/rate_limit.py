import asyncio
import math
import threading
import time


class AsyncRateLimiter:
    """Async token bucket rate limiter with dynamic throttle and backoff.

    The token state is guarded by a ``threading.Lock`` held across plain
    arithmetic only -- never across an await -- so one limiter may be shared
    by any number of event loops and threads. ``acquire`` reserves its slot
    under the lock and sleeps outside it.
    """

    def __init__(self, rate_per_sec: float, max_burst: float = 1.0) -> None:
        rate = float(rate_per_sec)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"rate_per_sec must be positive and finite, got {rate_per_sec}")
        self.rate_per_sec = rate
        self.capacity = float(max_burst)
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self.throttled_until = 0.0
        self._lock = threading.Lock()

    def throttle(self, duration: float) -> None:
        """Pause every request on this bucket for ``duration`` seconds.

        Called from the 429 path in ``AsyncHttpClient.get`` so sibling coroutines
        on the same host back off too, not just the one that was rejected.

        Takes ``_lock``; this is safe because no caller ever sleeps while
        holding it. The limiter registry is process-global, so a request
        handling thread may legitimately throttle a bucket that another
        thread or event loop is acquiring from.

        ``last_update`` is pushed forward to ``throttled_until`` on purpose: it
        stops the bucket from accruing tokens during the pause, so the first
        request after the throttle still pays a full token interval instead of
        firing immediately into the host that just rejected us.
        """
        if not math.isfinite(duration) or duration <= 0.0:
            return
        with self._lock:
            now = time.monotonic()
            self.throttled_until = max(self.throttled_until, now + duration)
            self.tokens = 0.0
            self.last_update = max(self.last_update, self.throttled_until)

    def _reserve(self, tokens: float) -> float:
        """Claim ``tokens`` and return the seconds the caller must sleep.

        Holds ``_lock`` across arithmetic only -- never across an await -- so
        the limiter belongs to no single event loop and is safe to share
        across threads. ``earliest`` folds in ``last_update`` so a second
        caller that arrives while the first is still sleeping queues behind
        it instead of reserving the same instant.
        """
        with self._lock:
            now = time.monotonic()
            earliest = max(now, self.throttled_until, self.last_update)
            elapsed = earliest - self.last_update
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
            self.last_update = earliest
            if self.tokens >= tokens:
                self.tokens -= tokens
                return earliest - now
            deficit = tokens - self.tokens
            wait = deficit / self.rate_per_sec
            self.tokens = 0.0
            self.last_update = earliest + wait
            return earliest - now + wait

    async def acquire(self, tokens: float = 1.0) -> None:
        delay = self._reserve(tokens)
        if delay > 0.0:
            await asyncio.sleep(delay)
