"""Memoisation for the expensive history aggregates.

The climate, calendar and all-time-record queries each scan the whole reading
table — together they dominate a page render, and they grow with the archive.
Their answers only change when a reading is written, which happens every few
minutes, so they are memoised and the cache is dropped on every write.

Entries are also keyed by the current local date, because several of these
queries deliberately exclude today and so change at midnight even if nothing
is written.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Bumped on every write; part of every cache key, so a bump orphans the
#: previous generation's entries wholesale.
_generation = 0
_caches: list[dict] = []


def invalidate() -> None:
    """Drop every memoised value. Called after any write."""
    global _generation
    _generation += 1
    for store in _caches:
        store.clear()


def _day_key() -> str:
    from . import clock

    return clock.fmt_date(clock.now())


def cached(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Memoise an async function on its arguments, the generation and the date.

    A per-key lock keeps a burst of concurrent requests from all computing the
    same half-second aggregate.
    """
    store: dict[Any, T] = {}
    locks: dict[Any, asyncio.Lock] = {}
    _caches.append(store)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        key = (_generation, _day_key(), args, tuple(sorted(kwargs.items())))
        if key in store:
            return store[key]

        lock = locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Another waiter may have filled it while we queued.
            if key in store:
                return store[key]
            value = await fn(*args, **kwargs)
            store[key] = value
        locks.pop(key, None)
        return value

    wrapper.cache_clear = store.clear  # type: ignore[attr-defined]
    return wrapper
