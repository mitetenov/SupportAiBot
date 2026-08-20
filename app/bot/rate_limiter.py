"""Caps how often a user can trigger a model call."""

import time
from collections.abc import Callable
from datetime import timedelta


class UserRateLimiter:
    """Caps how often a user can trigger a model call.

    Applied per coalesced batch rather than per message — UserMessageBuffer
    has already merged a normal typing burst by the time this runs, so tripping
    here means sustained flooding, not someone typing quickly.
    """

    DEFAULT_MIN_INTERVAL: float = 3.0  # seconds (3,000 ms)
    DEFAULT_RETENTION: float = 60.0  # seconds (60,000 ms)

    def __init__(
        self,
        min_interval: float | timedelta = DEFAULT_MIN_INTERVAL,
        retention: float | timedelta = DEFAULT_RETENTION,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        self.min_interval = (
            min_interval.total_seconds()
            if isinstance(min_interval, timedelta)
            else float(min_interval)
        )
        self.retention = (
            retention.total_seconds() if isinstance(retention, timedelta) else float(retention)
        )
        self._time_func = time_func if time_func is not None else time.time
        # No lock: the bot runs one event loop, and neither method below
        # awaits between reading a timestamp and writing it back.
        self._last_request_at: dict[int, float] = {}

    def try_acquire(self, user_id: int) -> bool:
        """Attempts to acquire a rate limit slot for the given user ID.

        Returns True if allowed (and updates the timestamp), False if blocked.
        """
        now = self._time_func()
        last = self._last_request_at.get(user_id)
        if last is None or (now - last) >= self.min_interval:
            self._last_request_at[user_id] = now
            return True
        return False

    def evict_stale_entries(self) -> int:
        """Removes entries older than retention window.

        Returns number of evicted entries.
        """
        now = self._time_func()
        cutoff = now - self.retention
        stale_keys = [uid for uid, ts in self._last_request_at.items() if ts < cutoff]
        for uid in stale_keys:
            del self._last_request_at[uid]
        return len(stale_keys)
