import asyncio
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

