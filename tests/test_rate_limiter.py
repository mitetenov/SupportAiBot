"""Unit tests for UserRateLimiter with simulated injectable time source."""

import pytest

from app.bot.rate_limiter import UserRateLimiter


class MockClock:
    """A controllable clock for stepping time without sleeping."""

    def __init__(self, initial_time_ms: float = 1_000_000.0) -> None:
        self._current_time_ms = initial_time_ms

    def __call__(self) -> float:
        """Return current time in seconds (or ms depending on interface)."""
        return self._current_time_ms / 1000.0

    def millis(self) -> int:
        return int(self._current_time_ms)

    def advance_millis(self, ms: float) -> None:
        self._current_time_ms += ms

    def advance_seconds(self, s: float) -> None:
        self._current_time_ms += s * 1000.0


class TestUserRateLimiter:
    """Test 3-second rate limiter interval and 60-second eviction."""

    @pytest.fixture
    def clock(self) -> MockClock:
        return MockClock()

    @pytest.fixture
    def limiter(self, clock: MockClock) -> UserRateLimiter:
        return UserRateLimiter(time_func=clock)

    def test_should_acquire_on_first_request(self, limiter: UserRateLimiter) -> None:
        assert limiter.try_acquire(1) is True

    def test_should_block_request_within_interval(self, limiter: UserRateLimiter) -> None:
        assert limiter.try_acquire(1) is True
        assert limiter.try_acquire(1) is False

    def test_should_allow_different_users_independently(self, limiter: UserRateLimiter) -> None:
        assert limiter.try_acquire(1) is True
        assert limiter.try_acquire(2) is True
        assert limiter.try_acquire(1) is False

    def test_should_allow_after_interval_expires(
        self, limiter: UserRateLimiter, clock: MockClock
    ) -> None:
        assert limiter.try_acquire(1) is True
        clock.advance_millis(3000)
        assert limiter.try_acquire(1) is True

    def test_should_still_block_just_before_interval_expires(
        self, limiter: UserRateLimiter, clock: MockClock
    ) -> None:
        assert limiter.try_acquire(1) is True
        clock.advance_millis(2999)
        assert limiter.try_acquire(1) is False

    def test_should_still_block_within_interval_even_after_other_user(
        self, limiter: UserRateLimiter, clock: MockClock
    ) -> None:
        assert limiter.try_acquire(1) is True
        clock.advance_millis(1000)
        assert limiter.try_acquire(2) is True
        assert limiter.try_acquire(1) is False

    @pytest.mark.parametrize("user_id", [-1, 0, 9223372036854775807, -9223372036854775808])
    def test_should_handle_any_user_id(self, limiter: UserRateLimiter, user_id: int) -> None:
        assert limiter.try_acquire(user_id) is True
        assert limiter.try_acquire(user_id) is False

    def test_should_evict_entries_older_than_retention_window(
        self, limiter: UserRateLimiter, clock: MockClock
    ) -> None:
        assert limiter.try_acquire(1) is True
        clock.advance_millis(61000)
        limiter.evict_stale_entries()
        # Evicted, so next call is treated as first request
        assert limiter.try_acquire(1) is True

    def test_should_keep_recent_entries_during_eviction(
        self, limiter: UserRateLimiter, clock: MockClock
    ) -> None:
        assert limiter.try_acquire(1) is True
        limiter.evict_stale_entries()
        assert limiter.try_acquire(1) is False

    def test_camel_case_method_aliases(self, limiter: UserRateLimiter) -> None:
        assert limiter.tryAcquire(100) is True
        assert limiter.tryAcquire(100) is False
        limiter.evictStaleEntries()
