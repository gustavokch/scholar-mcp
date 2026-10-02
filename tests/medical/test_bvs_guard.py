"""BvsGuard: single-flight, concurrency cap and breaker."""

import asyncio

from scholar_mcp.medical.bvs_guard import BvsGuard


def _guard(max_concurrent=2, threshold=2, cooldown_s=60.0) -> BvsGuard:
    return BvsGuard(max_concurrent, threshold, cooldown_s)


async def test_identical_keys_share_one_request():
    guard = _guard()
    runs = 0

    async def work():
        nonlocal runs
        runs += 1
        await asyncio.sleep(0.05)
        return "payload"

    results = await asyncio.gather(*(guard.single_flight("q", work) for _ in range(4)))
    assert results == ["payload"] * 4
    assert runs == 1


async def test_distinct_keys_do_not_share():
    guard = _guard()
    runs = 0

    async def work():
        nonlocal runs
        runs += 1
        return runs

    await asyncio.gather(guard.single_flight("a", work), guard.single_flight("b", work))
    assert runs == 2


async def test_a_completed_flight_is_not_reused_by_a_later_call():
    guard = _guard()
    runs = 0

    async def work():
        nonlocal runs
        runs += 1
        return runs

    assert await guard.single_flight("q", work) == 1
    assert await guard.single_flight("q", work) == 2


async def test_one_waiter_leaving_does_not_cancel_the_shared_request():
    guard = _guard()
    finished = asyncio.Event()

    async def work():
        await asyncio.sleep(0.1)
        finished.set()
        return "ok"

    leaver = asyncio.ensure_future(asyncio.wait_for(guard.single_flight("q", work), 0.02))
    stayer = asyncio.ensure_future(guard.single_flight("q", work))
    try:
        await leaver
    except (asyncio.TimeoutError, TimeoutError):
        pass
    assert await stayer == "ok"
    assert finished.is_set()


async def test_last_waiter_leaving_cancels_the_request():
    guard = _guard()
    cancelled = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    try:
        await asyncio.wait_for(guard.single_flight("q", work), 0.02)
    except (asyncio.TimeoutError, TimeoutError):
        pass
    await asyncio.sleep(0)
    assert cancelled.is_set()
    # The abandoned flight must not be handed to the next caller.
    assert await guard.single_flight("q", _value("fresh")) == "fresh"


def _value(v):
    async def work():
        return v

    return work


async def test_slots_cap_requests_in_flight():
    guard = _guard(max_concurrent=2)
    active = peak = 0

    async def work():
        nonlocal active, peak
        async with guard.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(work() for _ in range(6)))
    assert peak == 2


def test_breaker_opens_at_threshold_with_the_kind_that_opened_it():
    guard = _guard(threshold=2)
    guard.record_failure("timeout")
    assert guard.tripped() == ""
    guard.record_failure("origin_outage")
    assert guard.tripped() == "origin_outage"


def test_success_resets_the_failure_count():
    guard = _guard(threshold=2)
    guard.record_failure("timeout")
    guard.record_success()
    guard.record_failure("timeout")
    assert guard.tripped() == ""


async def test_breaker_half_opens_after_cooldown_and_one_failure_reopens_it():
    guard = _guard(threshold=2, cooldown_s=0.05)
    guard.record_failure("timeout")
    guard.record_failure("timeout")
    assert guard.tripped() == "timeout"
    await asyncio.sleep(0.08)
    assert guard.tripped() == ""
    guard.record_failure("timeout")
    assert guard.tripped() == "timeout"


async def test_success_after_cooldown_closes_the_breaker():
    guard = _guard(threshold=2, cooldown_s=0.05)
    guard.record_failure("timeout")
    guard.record_failure("timeout")
    await asyncio.sleep(0.08)
    guard.record_success()
    guard.record_failure("timeout")
    assert guard.tripped() == ""
