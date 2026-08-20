"""Unit tests for the recurring cleanup scheduler."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.maintenance import MaintenanceJob, MaintenanceScheduler, build_default_jobs


@pytest.mark.asyncio
async def test_runs_the_job_repeatedly_on_its_interval() -> None:
    calls: list[int] = []
    scheduler = MaintenanceScheduler(
        [MaintenanceJob(name="tick", interval_seconds=0.01, run=lambda: calls.append(1))]
    )
    scheduler.start()
    await asyncio.sleep(0.06)
    await scheduler.stop()

    assert len(calls) >= 3


@pytest.mark.asyncio
async def test_awaits_coroutine_jobs() -> None:
    job = AsyncMock()
    scheduler = MaintenanceScheduler(
        [MaintenanceJob(name="async-tick", interval_seconds=0.01, run=job)]
    )
    scheduler.start()
    await asyncio.sleep(0.03)
    await scheduler.stop()

    assert job.await_count >= 1


@pytest.mark.asyncio
async def test_a_failing_job_does_not_kill_its_loop() -> None:
    calls: list[int] = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("database is down")

    scheduler = MaintenanceScheduler(
        [MaintenanceJob(name="failing", interval_seconds=0.01, run=boom)]
    )
    scheduler.start()
    await asyncio.sleep(0.06)
    await scheduler.stop()

    assert len(calls) >= 3, "loop stopped after the first failure"


@pytest.mark.asyncio
async def test_stop_cancels_every_task() -> None:
    scheduler = MaintenanceScheduler(
        [
            MaintenanceJob(name="a", interval_seconds=10, run=lambda: None),
            MaintenanceJob(name="b", interval_seconds=10, run=lambda: None),
        ]
    )
    scheduler.start()
    assert len(scheduler._tasks) == 2
    await scheduler.stop()
    assert scheduler._tasks == []


@pytest.mark.asyncio
async def test_default_jobs_wire_the_three_eviction_methods() -> None:
    chat_history = MagicMock()
    rate_limiter = MagicMock()
    conversation_state = MagicMock()

    jobs = build_default_jobs(chat_history, rate_limiter, conversation_state)

    assert [j.name for j in jobs] == [
        "chat-history-eviction",
        "rate-limiter-eviction",
        "conversation-state-eviction",
    ]
    assert jobs[0].run is chat_history.evict_stale_entries
    assert jobs[1].run is rate_limiter.evict_stale_entries
    assert jobs[2].run is conversation_state.evict_expired
    assert all(j.interval_seconds > 0 for j in jobs)
