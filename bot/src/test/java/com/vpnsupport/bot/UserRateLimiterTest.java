package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class UserRateLimiterTest {

    /** A clock the test moves by hand, so no test has to sleep through the window. */
    private static final class TestClock extends Clock {
        private Instant now = Instant.parse("2026-01-01T00:00:00Z");

        void advanceMillis(long millis) {
            now = now.plusMillis(millis);
        }

        @Override
        public Instant instant() {
            return now;
        }

        @Override
        public ZoneOffset getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(java.time.ZoneId zone) {
            return this;
        }
    }

    private final TestClock clock = new TestClock();
    private final UserRateLimiter limiter = new UserRateLimiter(clock);

    @Test
    void shouldAcquireOnFirstRequest() {
        assertTrue(limiter.tryAcquire(1L));
    }

    @Test
    void shouldBlockRequestWithinInterval() {
        assertTrue(limiter.tryAcquire(1L));
        assertFalse(limiter.tryAcquire(1L));
    }

    @Test
    void shouldAllowDifferentUsersIndependently() {
        assertTrue(limiter.tryAcquire(1L));
        assertTrue(limiter.tryAcquire(2L));
        assertFalse(limiter.tryAcquire(1L));
    }

    @Test
    void shouldAllowAfterIntervalExpires() {
        assertTrue(limiter.tryAcquire(1L));

        clock.advanceMillis(3_000);

        assertTrue(limiter.tryAcquire(1L));
    }

    @Test
    void shouldStillBlockJustBeforeTheIntervalExpires() {
        assertTrue(limiter.tryAcquire(1L));

        clock.advanceMillis(2_999);

        assertFalse(limiter.tryAcquire(1L));
    }

    @Test
    void shouldStillBlockWithinIntervalEvenAfterOtherUser() {
        assertTrue(limiter.tryAcquire(1L));

        clock.advanceMillis(1_000);

        assertTrue(limiter.tryAcquire(2L));
        assertFalse(limiter.tryAcquire(1L));
    }

    @ParameterizedTest
    @ValueSource(longs = {-1L, 0L, Long.MAX_VALUE, Long.MIN_VALUE})
    void shouldHandleAnyUserId(long userId) {
        assertTrue(limiter.tryAcquire(userId));
        assertFalse(limiter.tryAcquire(userId));
    }

    @Test
    void shouldEvictEntriesOlderThanTheRetentionWindow() {
        assertTrue(limiter.tryAcquire(1L));

        clock.advanceMillis(61_000);
        limiter.evictStaleEntries();

        // Evicted, so the next call is treated as a first request.
        assertTrue(limiter.tryAcquire(1L));
    }

    @Test
    void shouldKeepRecentEntriesDuringEviction() {
        assertTrue(limiter.tryAcquire(1L));

        limiter.evictStaleEntries();

        assertFalse(limiter.tryAcquire(1L));
    }
}
