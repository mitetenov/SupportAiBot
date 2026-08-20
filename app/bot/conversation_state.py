"""Short-lived per-user conversation state: last question and operator activity timestamp."""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _EmptyContext:
    """Sentinel representation of an empty FAQ context."""

    text: str = ""
    results: tuple[Any, ...] = ()

    def __repr__(self) -> str:
        return "<EmptyFaqContext>"


EMPTY_FAQ_CONTEXT = _EmptyContext()


@dataclass
class LastQuery:
    """The user's most recent question and the FAQ retrieval it produced."""

    text: str
    faq_context: Any = None
    recorded_at: float = 0.0

    def faq_context_or_empty(self) -> Any:
        """Returns the associated FAQ context or an empty fallback."""
        if self.faq_context is not None:
            return self.faq_context
        return EMPTY_FAQ_CONTEXT


class ConversationState:
    """Short-lived per-user conversation state: what the user last asked and when an operator last replied.

    Both facts expire, and both are pruned on a schedule to prevent unbounded memory growth.
    """

    DEFAULT_OPERATOR_SUPPRESSION_WINDOW: float = 1800.0  # 30 minutes
    DEFAULT_LAST_QUERY_TTL: float = 21600.0  # 6 hours

    def __init__(
        self,
        operator_suppression_window: float | timedelta = DEFAULT_OPERATOR_SUPPRESSION_WINDOW,
        last_query_ttl: float | timedelta = DEFAULT_LAST_QUERY_TTL,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        self.operator_suppression_window = (
            operator_suppression_window.total_seconds()
            if isinstance(operator_suppression_window, timedelta)
            else float(operator_suppression_window)
        )
        self.last_query_ttl = (
            last_query_ttl.total_seconds()
            if isinstance(last_query_ttl, timedelta)
            else float(last_query_ttl)
        )
        self._time_func = time_func if time_func is not None else time.time
        self._last_queries: dict[int, LastQuery] = {}
        self._last_operator_reply_at: dict[int, float] = {}
        self._lock = threading.Lock()

    def record_query(
        self,
        user_id: int,
        query: str | None,
        faq_context: Any = None,
    ) -> None:
        """Record the user's latest query along with its FAQ retrieval context."""
        if not query or not query.strip():
            return
        now = self._time_func()
        with self._lock:
            self._last_queries[user_id] = LastQuery(
                text=query.strip(),
                faq_context=faq_context,
                recorded_at=now,
            )

    def last_query(self, user_id: int) -> LastQuery | None:
        """Retrieve the last query if within the TTL window, or None if expired/missing."""
        now = self._time_func()
        with self._lock:
            last = self._last_queries.get(user_id)
            if last is None:
                return None
            if (now - last.recorded_at) >= self.last_query_ttl:
                del self._last_queries[user_id]
                return None
            return last

    def record_operator_reply(self, user_id: int) -> None:
        """Record that a human operator replied to the user, setting the suppression timestamp."""
        now = self._time_func()
        with self._lock:
            self._last_operator_reply_at[user_id] = now

    def is_operator_recently_active(self, user_id: int) -> bool:
        """True while the AI should stay out of the way because a human is handling this conversation."""
        now = self._time_func()
        with self._lock:
            at = self._last_operator_reply_at.get(user_id)
            if at is None:
                return False
            if (now - at) >= self.operator_suppression_window:
                del self._last_operator_reply_at[user_id]
                return False
            return True

    def clear(self, user_id: int) -> None:
        """Drop all transient state for a user."""
        with self._lock:
            self._last_queries.pop(user_id, None)
            self._last_operator_reply_at.pop(user_id, None)

    def evict_expired(self) -> int:
        """Prune all stale queries and suppression records beyond their respective TTLs."""
        now = self._time_func()
        removed = 0
        with self._lock:
            stale_queries = [
                uid
                for uid, lq in self._last_queries.items()
                if (now - lq.recorded_at) >= self.last_query_ttl
            ]
            for uid in stale_queries:
                del self._last_queries[uid]
                removed += 1

            stale_replies = [
                uid
                for uid, at in self._last_operator_reply_at.items()
                if (now - at) >= self.operator_suppression_window
            ]
            for uid in stale_replies:
                del self._last_operator_reply_at[uid]
                removed += 1

        if removed > 0:
            logger.debug("Evicted %d stale conversation-state entries", removed)
        return removed
