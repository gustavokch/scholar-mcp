import asyncio
import gc

import pytest

from scholar_mcp.utils.ctxstate import ContextScoped

pytestmark = pytest.mark.asyncio


class Holder:
    value: dict = ContextScoped(dict)
    label: str | None = ContextScoped(lambda: None)


async def test_default_is_per_instance():
    a, b = Holder(), Holder()
    a.value["x"] = 1
    assert b.value == {}
    assert a.value == {"x": 1}


async def test_set_and_get_roundtrip():
    h = Holder()
    h.label = "blocked"
    assert h.label == "blocked"


async def test_concurrent_tasks_do_not_share_state():
    """Two concurrent calls against the SAME object must not see each other.

    This is the whole point: the resolver and the providers are module-level
    singletons, so a plain instance attribute would leak one MCP request's
    degradation status into another's response.
    """
    shared = Holder()
    started = asyncio.Event()

    async def slow() -> dict:
        shared.value["src"] = "slow"
        started.set()
        await asyncio.sleep(0.05)
        return dict(shared.value)

    async def fast() -> dict:
        await started.wait()
        shared.value.clear()
        shared.value["src"] = "fast"
        return dict(shared.value)

    slow_res, fast_res = await asyncio.gather(slow(), fast())
    assert slow_res == {"src": "slow"}
    assert fast_res == {"src": "fast"}


async def test_value_written_in_callee_visible_to_awaiting_caller():
    """An await chain shares one context, so the caller reads what it awaited."""
    h = Holder()

    async def callee() -> None:
        h.label = "ok"

    await callee()
    assert h.label == "ok"


async def test_freed_instance_id_reuse_does_not_leak_stale_value():
    """`id()` is reused after GC: a new instance that lands on a freed id
    must read its default, not the dead instance's value."""
    dead = Holder()
    dead.label = "stale"
    dead_id = id(dead)
    del dead  # refcount drop fires the weakref cleanup; no gc.collect(): it
    # would release the whole arena and the freed block would never be reused

    recycled = None
    for _ in range(10_000):
        candidate = Holder()
        if id(candidate) == dead_id:
            recycled = candidate
            break
        del candidate
    assert recycled is not None, "test setup: could not recycle the freed id"
    assert recycled.label is None


async def test_backing_map_does_not_grow_across_short_lived_instances():
    """Entries must be dropped with their instance, not accumulate forever."""
    descriptor = Holder.value
    holders = [Holder() for _ in range(500)]
    for h in holders:
        h.value["x"] = 1
    del holders
    del h  # the loop variable still pins the last instance
    gc.collect()
    assert len(descriptor._values()) == 0
