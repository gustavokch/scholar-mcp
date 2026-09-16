import asyncio

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
