"""A lock per key, held only while someone is waiting on it."""

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager


class KeyedLock:
    """Serialises work per key without leaking a lock object per key seen.

    A plain ``dict[key, Lock]`` grows by one entry for every user the bot ever
    talks to and never shrinks. Here the entry is reference-counted and dropped
    as soon as the last holder leaves, so the map only ever holds the keys with
    work in flight.
    """

    def __init__(self) -> None:
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._waiters: dict[Hashable, int] = {}
        self._guard = asyncio.Lock()

    def active_keys(self) -> int:
        """How many keys currently hold a lock — nothing else should be retained."""
        return len(self._locks)

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        """Hold the lock for ``key`` for the duration of the block."""
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._waiters[key] = self._waiters.get(key, 0) + 1

        try:
            async with lock:
                yield
        finally:
            async with self._guard:
                remaining = self._waiters.get(key, 1) - 1
                if remaining <= 0:
                    self._waiters.pop(key, None)
                    self._locks.pop(key, None)
                else:
                    self._waiters[key] = remaining
