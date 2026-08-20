"""Tests for the per-key lock shared by topic resolution and the message pipeline."""

import asyncio

import pytest

from app.bot.keyed_lock import KeyedLock


class TestKeyedLock:
    async def test_holders_of_the_same_key_do_not_overlap(self) -> None:
        locks = KeyedLock()
        in_flight = 0
        peak = 0

        async def work() -> None:
            nonlocal in_flight, peak
            async with locks.hold(42):
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0)
                in_flight -= 1

        await asyncio.gather(*(work() for _ in range(4)))

        assert peak == 1

    async def test_different_keys_run_side_by_side(self) -> None:
        locks = KeyedLock()
        in_flight = 0
        peak = 0

        async def work(key: int) -> None:
            nonlocal in_flight, peak
            async with locks.hold(key):
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0)
                in_flight -= 1

        await asyncio.gather(*(work(key) for key in range(3)))

        assert peak == 3

    async def test_order_is_preserved_per_key(self) -> None:
        locks = KeyedLock()
        finished: list[int] = []

        async def work(index: int) -> None:
            async with locks.hold("user"):
                await asyncio.sleep(0)
                finished.append(index)

        await asyncio.gather(*(work(i) for i in range(5)))

        assert finished == [0, 1, 2, 3, 4]

    async def test_nothing_is_retained_once_the_work_is_done(self) -> None:
        locks = KeyedLock()

        for key in range(50):
            async with locks.hold(key):
                pass

        assert locks.active_keys() == 0, "the lock map grows one entry per user forever"

    async def test_a_raising_holder_still_releases_the_key(self) -> None:
        locks = KeyedLock()

        with pytest.raises(RuntimeError):
            async with locks.hold("user"):
                raise RuntimeError("boom")

        assert locks.active_keys() == 0
        async with locks.hold("user"):
            pass
