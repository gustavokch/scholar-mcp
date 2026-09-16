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
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class ContextScoped(Generic[T]):
    """Data descriptor whose value lives in a :class:`contextvars.ContextVar`.

    Instances are keyed by ``id(obj)`` inside a single per-attribute
    ``ContextVar`` holding a small dict, so one descriptor serves every instance
    of the owning class without leaking values between them.
    """

    def __init__(self, default_factory: Any) -> None:
        self._default_factory = default_factory
        self._var: contextvars.ContextVar[dict[int, T]] | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name
        self._var = contextvars.ContextVar(f"{owner.__name__}.{name}")

    def _values(self) -> dict[int, T]:
        assert self._var is not None
        try:
            return self._var.get()
        except LookupError:
            values: dict[int, T] = {}
            self._var.set(values)
            return values

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        values = self._values()
        key = id(obj)
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
        values[id(obj)] = value
        assert self._var is not None
        self._var.set(values)
