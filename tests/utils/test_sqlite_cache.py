import asyncio
from pathlib import Path

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
