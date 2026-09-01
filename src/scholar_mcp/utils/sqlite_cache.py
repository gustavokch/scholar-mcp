import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from scholar_mcp.config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    last_accessed REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_source ON cache_entries(source);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(created_at, ttl_seconds);
CREATE INDEX IF NOT EXISTS idx_cache_lru ON cache_entries(last_accessed);
"""


@dataclass
class CacheMetadata:
    cached: bool
    cache_age: int
    error: bool = False


class SQLiteCacheManager:
    """Persistent async SQLite cache with per-source TTLs and LRU eviction.

    Constructed at module import; the database is opened lazily on first use
    (server.py instantiates clients before an event loop is running).
    """

    def __init__(self, db_path: Path, settings: Settings) -> None:
        self.db_path = db_path
        self.settings = settings
        self._db: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    def _ttl_for(self, source: str, ttl: int | None) -> int:
        if ttl is not None:
            return ttl
        by_source = {
            "fda": self.settings.cache_ttl_fda,
            "pubmed": self.settings.cache_ttl_pubmed,
            "who": self.settings.cache_ttl_who,
            "rxnorm": self.settings.cache_ttl_rxnorm,
            "guidelines": self.settings.cache_ttl_guidelines,
            "bright_futures": self.settings.cache_ttl_bright_futures,
            "aap_policy": self.settings.cache_ttl_aap_policy,
            "pediatric_journals": self.settings.cache_ttl_pediatric_journals,
            "child_health": self.settings.cache_ttl_child_health,
            "pediatric_drugs": self.settings.cache_ttl_pediatric_drugs,
            "clinical_trials": self.settings.cache_ttl_clinical_trials,
            "who_iris": self.settings.cache_ttl_who_iris,
        }
        return by_source.get(source, self.settings.cache_ttl_seconds)

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            async with self._init_lock:
                if self._db is None:
                    self.db_path.parent.mkdir(parents=True, exist_ok=True)
                    db = await aiosqlite.connect(self.db_path)
                    await db.execute("PRAGMA journal_mode=WAL")
                    await db.executescript(SCHEMA)
                    await db.commit()
                    self._db = db
        return self._db

    async def init_db(self) -> None:
        await self._ensure_db()

    async def get(self, key: str) -> tuple[Any | None, CacheMetadata]:
        db = await self._ensure_db()
        now = time.time()

        async with db.execute(
            "SELECT data, created_at, ttl_seconds FROM cache_entries WHERE key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            self._misses += 1
            return None, CacheMetadata(cached=False, cache_age=0)

        data_json, created_at, ttl_seconds = row
        if created_at + ttl_seconds < now:
            await db.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
            await db.commit()
            self._misses += 1
            return None, CacheMetadata(cached=False, cache_age=0)

        await db.execute(
            "UPDATE cache_entries SET last_accessed = ? WHERE key = ?",
            (now, key),
        )
        await db.commit()

        self._hits += 1
        cache_age = max(0, int(now - created_at))
        data = json.loads(data_json)
        return data, CacheMetadata(cached=True, cache_age=cache_age)

    async def set(
        self,
        key: str,
        data: Any,
        source: str,
        ttl: int | None = None,
    ) -> None:
        db = await self._ensure_db()
        now = time.time()
        resolved_ttl = self._ttl_for(source, ttl)
        data_json = json.dumps(data)

        await db.execute(
            """
            INSERT OR REPLACE INTO cache_entries
            (key, source, data, created_at, ttl_seconds, last_accessed)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, source, data_json, now, resolved_ttl, now),
        )
        await db.commit()

        # Evict oldest entries if capacity exceeded
        async with db.execute("SELECT COUNT(*) FROM cache_entries") as cur:
            count = (await cur.fetchone())[0]

        if count > self.settings.cache_max_entries:
            excess = count - self.settings.cache_max_entries
            await db.execute(
                """
                DELETE FROM cache_entries
                WHERE key IN (
                    SELECT key FROM cache_entries
                    ORDER BY last_accessed ASC
                    LIMIT ?
                )
                """,
                (excess,),
            )
            await db.commit()

    async def get_stats(self) -> dict[str, Any]:
        db = await self._ensure_db()
        now = time.time()

        async with db.execute(
            "SELECT COUNT(*) FROM cache_entries WHERE created_at + ttl_seconds >= ?",
            (now,),
        ) as cur:
            total_active = (await cur.fetchone())[0]

        async with db.execute(
            """
            SELECT source, COUNT(*)
            FROM cache_entries
            WHERE created_at + ttl_seconds >= ?
            GROUP BY source
            """,
            (now,),
        ) as cur:
            source_rows = await cur.fetchall()

        sources = {row[0]: row[1] for row in source_rows}
        total_requests = self._hits + self._misses
        hit_rate = (self._hits / total_requests) if total_requests > 0 else 0.0
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "total_entries": total_active,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": hit_rate,
            "sources": sources,
            "db_size_bytes": db_size,
        }

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
