"""Per-request state on shared singletons.

`WaterfallResolver` and the providers are module-level singletons in
``server.py``: an ordinary instance attribute written during one MCP tool call
is visible to every other in-flight call. ``ContextScoped`` keeps the ergonomics
of an instance attribute while giving each asyncio task its own value.

asyncio copies the current context when a task is *created*, not on every
``await``, so a value written inside ``search()`` is visible to the caller that
awaited it and invisible to a sibling task scheduled alongside it — exactly the
per-request isolation an MCP tool call needs.
"""

from __future__ import annotations

import contextvars
import weakref
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class _KeyedRef(weakref.ref):
    """A ``weakref.ref`` whose hash survives the referent's death.

    ``weakref.ref.__hash__`` hashes the referent while it is alive and falls
    back to ``hash(None)`` once it is dead, so a plain ref cannot be found in
    a dict after cleanup time. This subclass pins the identity hash at
    creation, letting the death callback pop the entry it keyed.
    """

    __slots__ = ("_pinned_hash",)

    def __init__(self, obj: Any, callback: Callable[[_KeyedRef], None]) -> None:
        super().__init__(obj, callback)
        self._pinned_hash = object.__hash__(obj)

    def __hash__(self) -> int:
        return self._pinned_hash

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, weakref.ref):
            return NotImplemented
        self_obj = self()
        other_obj = other()
        if self_obj is None or other_obj is None:
            return self is other
        return self_obj is other_obj


class ContextScoped(Generic[T]):
    """Data descriptor whose value lives in a :class:`contextvars.ContextVar`.

    Instances are keyed by a weak reference inside a single per-attribute
    ``ContextVar`` holding a small dict, so one descriptor serves every
    instance of the owning class without leaking values between them. The
    death callback drops the entry, so the map does not accumulate stale rows
    for freed instances — and, unlike ``id(obj)``, a recycled identity can
    never resurrect a dead instance's value.
    """

    def __init__(self, default_factory: Any) -> None:
        self._default_factory = default_factory
        self._var: contextvars.ContextVar[dict[_KeyedRef, T]] | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name
        self._var = contextvars.ContextVar(f"{owner.__name__}.{name}")

    def _values(self) -> dict[_KeyedRef, T]:
        assert self._var is not None
        try:
            return self._var.get()
        except LookupError:
            values: dict[_KeyedRef, T] = {}
            self._var.set(values)
            return values

    def _key(self, obj: Any) -> _KeyedRef:
        var = self._var

        def _cleanup(ref: _KeyedRef) -> None:
            # Best effort: drop the entry from the context that is current
            # when the referent dies. Other contexts' dicts die with their
            # task, so rows there cannot outlive the request that made them.
            try:
                values = var.get()  # type: ignore[union-attr]
            except LookupError:
                return
            values.pop(ref, None)

        return _KeyedRef(obj, _cleanup)

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        values = self._values()
        key = self._key(obj)
        if key not in values:
            # Go through __set__ so the default lands in a dict owned by this
            # context, never in one inherited from a parent task.
            self.__set__(obj, self._default_factory())
            return self._values()[key]
        return values[key]

    def __set__(self, obj: Any, value: T) -> None:
        # Re-``set`` the ContextVar rather than mutating the dict in place: a
        # child task that inherited this context must not write back into the
        # parent's dict.
        values = dict(self._values())
        values[self._key(obj)] = value
        assert self._var is not None
        self._var.set(values)
