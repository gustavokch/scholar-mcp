"""Process-wide guard for the BVS search endpoint.

``pesquisa.bvsalud.org`` answers in 37-43 s or 502s when degraded, and the
engine is a shared singleton that concurrent callers search through at once.
Without a shared guard each of N concurrent searches pays its own stage budget
against the same sick host. Three mechanisms, one object:

* single-flight: callers composing the identical request share one in-flight
  request instead of each spending a slot on it;
* a concurrency cap: the host limiter paces request *starts* (1 req/s) but
  does not bound requests in flight, and a host holding each connection for
  tens of seconds accumulates them;
* a breaker: ``threshold`` consecutive outage-class failures (timeout, 5xx)
  open it for ``cooldown_s``, during which callers skip BVS and are served the
  gov.br catalogs at once. Once the cooldown elapses a single further failure
  re-opens it; one success closes it.

Only outcomes that say the *origin* is unhealthy count. A CDN shield 403 is
a different failure with a different remedy (the browser tier) and neither
trips nor resets the breaker.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, TypeVar

_T = TypeVar("_T")


@dataclass
class _Flight:
    task: "asyncio.Future[Any]"
    waiters: int = 0


class BvsGuard:
    def __init__(
        self, max_concurrent: int, threshold: int, cooldown_s: float
    ) -> None:
        self._slots = asyncio.Semaphore(max(1, max_concurrent))
        self._threshold = max(1, threshold)
        self._cooldown_s = cooldown_s
        self._failures = 0
        self._open_until = 0.0
        self._open_kind: str = ""
        self._inflight: dict[Hashable, _Flight] = {}

    def slot(self) -> AbstractAsyncContextManager[None]:
        """One of the bounded in-flight request slots."""
        return self._slots

    def tripped(self) -> str:
        """The outage kind that opened the breaker, or ``""`` while it is closed."""
        if time.monotonic() < self._open_until:
            return self._open_kind
        return ""

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def record_failure(self, kind: str) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._open_until = time.monotonic() + self._cooldown_s
            self._open_kind = kind
            # Half-open after the cooldown: one more failure re-opens.
            self._failures = self._threshold - 1

    async def single_flight(
        self, key: Hashable, work: Callable[[], Awaitable[_T]]
    ) -> _T:
        """Run ``work`` once per ``key`` at a time; concurrent callers share it.

        The shared request is cancelled only when its last waiter leaves, so
        one caller's stage timeout never aborts a request another still wants,
        and a lone caller keeps the cancellation semantics it had before.
        """
        flight = self._inflight.get(key)
        if flight is None:
            flight = _Flight(task=asyncio.ensure_future(work()))
            self._inflight[key] = flight
            flight.task.add_done_callback(self._retrieve)
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                if self._inflight.get(key) is flight:
                    del self._inflight[key]
                if not flight.task.done():
                    flight.task.cancel()

    @staticmethod
    def _retrieve(task: "asyncio.Future[Any]") -> None:
        # Mark the exception retrieved so an abandoned flight does not log
        # "Task exception was never retrieved".
        if not task.cancelled():
            task.exception()
