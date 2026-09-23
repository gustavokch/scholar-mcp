import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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

# Engine diagnostics contract (ENAMED 2026 misses, track B §2), currently
# populated by the BVS (brazil_moh) engine on its search path. Lives here,
# next to ``CacheMetadata``, rather than in brazil_moh.py, so this generic
# cache module does not need a reverse import from a specific medical
# engine. ``origin_outage`` must not count against any caller-side breaker.
BvsErrorKind = Literal[
    "ok", "successful_empty", "cdn_challenge", "origin_outage", "timeout", "backend_error"
]


@dataclass
class CacheMetadata:
    cached: bool
    cache_age: int
    error: bool = False
    # All defaulted so every existing constructor keeps working; "" means
    # the producer predates the contract (or is a non-BVS engine).
    error_kind: BvsErrorKind | Literal[""] = ""
    http_status: int | None = None
    challenge_hit: bool = False
    timeout: bool = False
    # The relaxed variant that produced the results, when a PubMed-backed
    # search walked the query-relaxation ladder past the original query.
    # None when the original query sufficed or nothing was found. Surfaced
    # in the MCP tool envelope by server._with_degraded so the caller can
    # see the ladder worked.
    relaxed_query: str | None = None


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
        # Serializes concurrent get/set against one shared connection. Under
        # concurrency 4 the BVS chain issues PCDT + A-Z + several BVS stages
        # at once; without this, an expiry DELETE racing an INSERT OR REPLACE
        # on the same connection loses rows and surfaces as flaky
        # cache-miss storms. Contention scope is one event loop, matching
        # _init_lock above.
        self._io_lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        # asyncio.Lock binds to whichever loop first awaits on it while
        # contended (see PR #31); unlike AsyncRateLimiter's registry, this
        # class serializes real awaited DB I/O under _io_lock, so that fix's
        # threading.Lock-around-arithmetic-only pattern does not transfer --
        # holding a threading.Lock across an ``await`` would block the whole
        # OS thread and deadlock any sibling coroutine on the same loop.
        # Instead this class declares itself single-loop and asserts it on
        # every public entry point, before either lock is touched.
        self._loop: asyncio.AbstractEventLoop | None = None

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif loop is not self._loop:
            raise RuntimeError(
                f"SQLiteCacheManager({self.db_path}) is single-loop: it was first "
                "used on a different asyncio event loop and cannot be shared "
                "across loops."
            )

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
            "brazil_moh": self.settings.cache_ttl_brazil_moh,
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
        self._check_loop()
        await self._ensure_db()

    async def get(self, key: str) -> tuple[Any | None, CacheMetadata]:
        self._check_loop()
        now = time.time()

        async with self._io_lock:
            db = await self._ensure_db()
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
        self._check_loop()
        now = time.time()
        resolved_ttl = self._ttl_for(source, ttl)
        data_json = json.dumps(data)

        async with self._io_lock:
            db = await self._ensure_db()
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
        self._check_loop()
        now = time.time()

        async with self._io_lock:
            db = await self._ensure_db()
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

            hits = self._hits
            misses = self._misses

        sources = {row[0]: row[1] for row in source_rows}
        total_requests = hits + misses
        hit_rate = (hits / total_requests) if total_requests > 0 else 0.0
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "total_entries": total_active,
            "hits": hits,
            "misses": misses,
            "hit_rate": hit_rate,
            "sources": sources,
            "db_size_bytes": db_size,
        }

    async def close(self) -> None:
        self._check_loop()
        async with self._io_lock:
            if self._db is not None:
                await self._db.close()
                self._db = None
