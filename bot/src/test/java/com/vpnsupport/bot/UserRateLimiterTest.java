package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class UserRateLimiterTest {

    @Test
    void shouldAcquireOnFirstRequest() {
        UserRateLimiter limiter = new UserRateLimiter();
        assertTrue(limiter.tryAcquire(1L));
    }

    @Test
    void shouldBlockRequestWithinInterval() {
        UserRateLimiter limiter = new UserRateLimiter();
        assertTrue(limiter.tryAcquire(1L));
        assertFalse(limiter.tryAcquire(1L));
    }

    @Test
    void shouldAllowDifferentUsersIndependently() {
        UserRateLimiter limiter = new UserRateLimiter();
        assertTrue(limiter.tryAcquire(1L));
        assertTrue(limiter.tryAcquire(2L));
        assertFalse(limiter.tryAcquire(1L));
    }

    @Test
    void shouldAllowAfterIntervalExpires() throws InterruptedException {
        UserRateLimiter limiter = new UserRateLimiter();
        assertTrue(limiter.tryAcquire(1L));

        Thread.sleep(3100); // slightly more than MIN_INTERVAL_MS
        assertTrue(limiter.tryAcquire(1L));
    }

    @Test
    void shouldStillBlockWithinIntervalEvenAfterOtherUser() throws InterruptedException {
        UserRateLimiter limiter = new UserRateLimiter();
        assertTrue(limiter.tryAcquire(1L));

        Thread.sleep(1000);
        assertTrue(limiter.tryAcquire(2L));
        assertFalse(limiter.tryAcquire(1L));
    }

    @Test
    void shouldHandleNegativeUserId() {
        UserRateLimiter limiter = new UserRateLimiter();
        assertTrue(limiter.tryAcquire(-1L));
        assertFalse(limiter.tryAcquire(-1L));
    }

    @Test
    void shouldHandleZeroUserId() {
        UserRateLimiter limiter = new UserRateLimiter();
        assertTrue(limiter.tryAcquire(0L));
        assertFalse(limiter.tryAcquire(0L));
    }

    @Test
    void shouldHandleLargeUserId() {
        UserRateLimiter limiter = new UserRateLimiter();
        assertTrue(limiter.tryAcquire(Long.MAX_VALUE));
        assertFalse(limiter.tryAcquire(Long.MAX_VALUE));
    }
}
