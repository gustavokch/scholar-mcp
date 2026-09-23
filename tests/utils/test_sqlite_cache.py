import asyncio
from pathlib import Path

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager


async def test_sqlite_cache_set_get_miss(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "test_cache.db", settings=Settings.load())
    await cache.init_db()

    val, meta = await cache.get("fda:search:aspirin")
    assert val is None
    assert meta.cached is False
    assert meta.cache_age == 0

    await cache.set("fda:search:aspirin", {"brand": "Aspirin", "ndc": "123"}, source="fda")

    val, meta = await cache.get("fda:search:aspirin")
    assert val is not None
    assert val["brand"] == "Aspirin"
    assert meta.cached is True
    assert meta.cache_age >= 0

    await cache.close()


async def test_sqlite_cache_lazy_init_without_explicit_init_db(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "lazy.db", settings=Settings.load())
    await cache.set("k", "v", source="fda")  # must open DB implicitly
    val, meta = await cache.get("k")
    assert val == "v"
    assert meta.cached is True
    await cache.close()


async def test_sqlite_cache_expiration(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "exp.db", settings=Settings.load())
    await cache.init_db()

    await cache.set("short_lived", {"data": 1}, source="fda", ttl=1)
    val, _ = await cache.get("short_lived")
    assert val == {"data": 1}

    await asyncio.sleep(1.1)
    val, meta = await cache.get("short_lived")
    assert val is None
    assert meta.cached is False

    await cache.close()


async def test_sqlite_cache_source_ttl_resolution(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "ttl.db", settings=Settings.load())
    await cache.init_db()
    # source "fda" resolves to settings.cache_ttl_fda; "unknown-source" falls back to cache_ttl_seconds
    await cache.set("a", 1, source="fda")
    await cache.set("b", 2, source="unknown-source")
    assert cache._db is not None
    async with cache._db.execute(
        "SELECT key, ttl_seconds FROM cache_entries WHERE key IN ('a', 'b')"
    ) as cur:
        rows = {k: ttl for k, ttl in await cur.fetchall()}
    assert rows["a"] == Settings.load().cache_ttl_fda
    assert rows["b"] == Settings.load().cache_ttl_seconds
    await cache.close()


async def test_sqlite_cache_stats(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "stats.db", settings=Settings.load())
    await cache.init_db()

    await cache.set("k1", "v1", source="fda")
    await cache.set("k2", "v2", source="who")
    await cache.get("k1")  # hit
    await cache.get("missing")  # miss

    stats = await cache.get_stats()
    assert stats["total_entries"] == 2
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["sources"]["fda"] == 1
    assert stats["sources"]["who"] == 1

    await cache.close()


async def test_sqlite_cache_who_iris_ttl_resolution(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "ttl_iris.db", settings=Settings.load())
    await cache.init_db()
    try:
        await cache.set("c", 3, source="who_iris")
        assert cache._db is not None
        async with cache._db.execute(
            "SELECT ttl_seconds FROM cache_entries WHERE key = 'c'"
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == Settings.load().cache_ttl_who_iris
    finally:
        await cache.close()


async def test_sqlite_cache_get_survives_concurrent_close(tmp_path: Path):
    """close() must never let a concurrent get() touch a closed connection.

    Regression test for the race where get()/set()/get_stats() bound
    ``db = await self._ensure_db()`` outside ``_io_lock``: close() could take
    the lock, close the connection, and set ``self._db = None`` while a
    waiting get() still held a stale reference to the now-closed connection,
    surfacing as an aiosqlite "Cannot operate on a closed database" error.
    _ensure_db() is now called inside _io_lock in all three methods, so
    get() and close() are fully serialized -- this test proves that holds
    under real concurrent scheduling.
    """
    cache = SQLiteCacheManager(db_path=tmp_path / "race.db", settings=Settings.load())
    await cache.init_db()

    resume = asyncio.Event()
    real_ensure_db = cache._ensure_db

    async def gated_ensure_db():
        # get() calls this from inside _io_lock now, so close() (which also
        # takes _io_lock before touching self._db) cannot run concurrently
        # with it -- this gate proves that by blocking get() mid-flight
        # while holding the lock, then checking close() is still waiting.
        await resume.wait()
        return await real_ensure_db()

    cache._ensure_db = gated_ensure_db

    get_task = asyncio.ensure_future(cache.get("missing-key"))
    await asyncio.sleep(0.01)  # let get() acquire _io_lock and reach the gate
    close_task = asyncio.ensure_future(cache.close())

    await asyncio.sleep(0.05)
    assert not close_task.done(), "close() should block behind the held _io_lock"

    resume.set()
    value, meta = await get_task
    await close_task

    assert value is None
    assert meta.cached is False
    assert cache._db is None


def test_sqlite_cache_second_event_loop_raises(tmp_path: Path):
    """SQLiteCacheManager declares itself single-loop; using it from a second
    asyncio event loop must raise a clear error rather than hang or corrupt
    state via a loop-bound asyncio.Lock (the same failure class fixed for
    AsyncRateLimiter's registry in PR #31).
    """
    db_path = tmp_path / "second_loop.db"
    settings = Settings.load()
    cache = SQLiteCacheManager(db_path=db_path, settings=settings)

    async def first_loop_use():
        await cache.init_db()
        await cache.set("k", "v", source="fda")

    asyncio.run(first_loop_use())

    async def second_loop_use():
        await cache.get("k")

    with pytest.raises(RuntimeError, match="single-loop"):
        asyncio.run(second_loop_use())
