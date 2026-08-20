"""Recurring housekeeping: the Java @Scheduled jobs, as supervised asyncio tasks."""

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class Evictable(Protocol):
    """Anything holding per-user state that has to be pruned on a schedule."""

    def evict_stale_entries(self) -> Any:
        """Drop whatever has outlived its retention window."""
        ...


class ExpiringState(Protocol):
    """Per-user state that prunes itself by TTL."""

    def evict_expired(self) -> Any:
        """Drop every entry past its TTL."""
        ...


CHAT_HISTORY_INTERVAL_SECONDS = 60 * 60
RATE_LIMITER_INTERVAL_SECONDS = 10 * 60
CONVERSATION_STATE_INTERVAL_SECONDS = 15 * 60


@dataclass(frozen=True)
class MaintenanceJob:
    """One recurring cleanup, named so a failure says which one failed."""

    name: str
    interval_seconds: float
    run: Callable[[], Any]


class MaintenanceScheduler:
    """Runs cleanup jobs on a fixed delay for as long as the bot is up.

    Without this the in-memory maps of ChatHistoryService, UserRateLimiter and
    ConversationState grow one entry per user forever, and chat_messages never
    honours its TTL — the eviction methods exist on all three, but nothing was
    calling them.
    """

    def __init__(self, jobs: list[MaintenanceJob]) -> None:
        self.jobs = jobs
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        """Spawn one supervising task per job."""
        for job in self.jobs:
            self._tasks.append(asyncio.create_task(self._loop(job), name=f"maintenance:{job.name}"))
        logger.info("Maintenance scheduler started with %d job(s)", len(self._tasks))

    async def _loop(self, job: MaintenanceJob) -> None:
        while True:
            await asyncio.sleep(job.interval_seconds)
            try:
                result = job.run()
                if inspect.isawaitable(result):
                    await result
                logger.debug("Maintenance job %s completed", job.name)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A failed cleanup must not kill the loop: the next tick retries.
                logger.warning("Maintenance job %s failed: %s", job.name, e)

    async def stop(self) -> None:
        """Cancel every job and wait for the loops to unwind."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Maintenance scheduler stopped")


def build_default_jobs(
    chat_history_service: Evictable,
    rate_limiter: Evictable,
    conversation_state: ExpiringState,
) -> list[MaintenanceJob]:
    """Assemble the three jobs the Java service ran on a Spring schedule."""
    return [
        MaintenanceJob(
            name="chat-history-eviction",
            interval_seconds=CHAT_HISTORY_INTERVAL_SECONDS,
            run=chat_history_service.evict_stale_entries,
        ),
        MaintenanceJob(
            name="rate-limiter-eviction",
            interval_seconds=RATE_LIMITER_INTERVAL_SECONDS,
            run=rate_limiter.evict_stale_entries,
        ),
        MaintenanceJob(
            name="conversation-state-eviction",
            interval_seconds=CONVERSATION_STATE_INTERVAL_SECONDS,
            run=conversation_state.evict_expired,
        ),
    ]
