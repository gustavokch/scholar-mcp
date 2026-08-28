import asyncio
from collections import OrderedDict
import time
from typing import Any


class TTLCache:
    """Thread-safe and async-safe LRU cache with time-to-live expiration."""

    def __init__(self, maxsize: int = 500, ttl_seconds: int = 3600) -> None:
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            if key not in self._cache:
                return None
            value, expires_at = self._cache[key]
            if time.monotonic() > expires_at:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
            elif len(self._cache) >= self.maxsize:
                self._cache.popitem(last=False)
            self._cache[key] = (value, time.monotonic() + self.ttl_seconds)

    async def delete(self, key: str) -> None:
        async with self._lock:
            if key in self._cache:
                del self._cache[key]

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()
