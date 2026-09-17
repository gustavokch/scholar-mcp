import asyncio
import threading
import time
import pytest

from scholar_mcp.utils.rate_limit import AsyncRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_throttles():
    limiter = AsyncRateLimiter(rate_per_sec=10.0)
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    # 5 tokens at 10/s cannot complete faster than ~0.4s after the initial token
    assert time.monotonic() - start >= 0.3


@pytest.mark.asyncio
async def test_rate_limiter_throttle_backoff():
    limiter = AsyncRateLimiter(rate_per_sec=100.0)
    # First call is immediate
    await limiter.acquire()

    # Apply dynamic throttle of 0.15s
    limiter.throttle(0.15)

    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.12


@pytest.mark.asyncio
async def test_rate_limiter_safe_pacing_prevents_burst_in_window():
    # 2.8 rps requires ~0.357s per token.
    # 4 requests (1 initial + 3 subsequent) must take > 1.0s to never exceed 3 req/1s window.
    limiter = AsyncRateLimiter(rate_per_sec=2.8)
    start = time.monotonic()
    for _ in range(4):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 1.0


@pytest.mark.asyncio
async def test_rate_limiter_concurrent_acquires():
    limiter = AsyncRateLimiter(rate_per_sec=20.0)
    start = time.monotonic()
    await asyncio.gather(*(limiter.acquire() for _ in range(5)))
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15


def test_rate_limiter_rejects_invalid_rate():
    with pytest.raises(ValueError, match="positive and finite"):
        AsyncRateLimiter(rate_per_sec=0.0)

    with pytest.raises(ValueError, match="positive and finite"):
        AsyncRateLimiter(rate_per_sec=-5.0)

    with pytest.raises(ValueError, match="positive and finite"):
        AsyncRateLimiter(rate_per_sec=float("inf"))

    with pytest.raises(ValueError, match="positive and finite"):
        AsyncRateLimiter(rate_per_sec=float("nan"))


def test_rate_limiter_throttle_sanitizes_non_finite():
    limiter = AsyncRateLimiter(rate_per_sec=10.0)
    baseline = limiter.throttled_until
    limiter.throttle(float("nan"))
    assert limiter.throttled_until == baseline

    limiter.throttle(float("inf"))
    assert limiter.throttled_until == baseline

    limiter.throttle(-10.0)
    assert limiter.throttled_until >= baseline


def test_limiter_survives_a_second_event_loop():
    """One limiter, two sequential loops. An asyncio.Lock binds to the first
    loop that awaits it, so the old limiter raised RuntimeError here once a
    waiter had to block: its future was created on the first loop."""
    limiter = AsyncRateLimiter(rate_per_sec=100.0)

    async def contend() -> None:
        # Throttle so the first acquire sleeps while holding the bucket and
        # the second blocks on the lock -- the path that used to bind an
        # asyncio.Lock to whichever loop ran first.
        limiter.throttle(0.01)
        await asyncio.gather(*(limiter.acquire() for _ in range(2)))

    asyncio.run(contend())  # burst 1 + 1 refill interval; binds the old lock
    asyncio.run(contend())  # must not raise "is bound to a different event loop"


def test_spacing_holds_across_loops():
    limiter = AsyncRateLimiter(rate_per_sec=5.0)  # 0.2s per token, burst 1
    start = time.monotonic()
    for _ in range(3):
        asyncio.run(limiter.acquire())
    # First call spends the burst token; the next two each pay an interval.
    assert time.monotonic() - start >= 0.4


def test_concurrent_acquires_are_spaced_not_stacked():
    limiter = AsyncRateLimiter(rate_per_sec=5.0)

    async def main() -> float:
        start = time.monotonic()
        await asyncio.gather(*(limiter.acquire() for _ in range(3)))
        return time.monotonic() - start

    assert asyncio.run(main()) >= 0.4


def test_throttle_from_another_thread_is_observed():
    limiter = AsyncRateLimiter(rate_per_sec=100.0)
    t = threading.Thread(target=limiter.throttle, args=(0.3,))
    t.start()
    t.join()

    start = time.monotonic()
    asyncio.run(limiter.acquire())
    assert time.monotonic() - start >= 0.25


