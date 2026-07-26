package com.vpnsupport.bot;

import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Caps how often a user can trigger a model call.
 *
 * <p>Applied per coalesced batch rather than per message — {@link
 * UserMessageBuffer} has already merged a normal typing burst by the time this
 * runs, so tripping here means sustained flooding, not someone typing quickly.
 */
@Component
public class UserRateLimiter {

    private static final long MIN_INTERVAL_MS = 3_000;

    /** Entries this old can no longer affect a decision. */
    private static final long RETENTION_MS = 60_000;

    private final ConcurrentHashMap<Long, Long> lastRequestAt = new ConcurrentHashMap<>();

    public boolean tryAcquire(long userId) {
        long now = System.currentTimeMillis();
        AtomicBoolean allowed = new AtomicBoolean(false);
        lastRequestAt.compute(userId, (k, v) -> {
            if (v == null || now - v >= MIN_INTERVAL_MS) {
                allowed.set(true);
                return now;
            }
            return v;
        });
        return allowed.get();
    }

    /** Keeps the map from retaining one permanent entry per user ever seen. */
    @Scheduled(fixedDelay = 10, timeUnit = TimeUnit.MINUTES)
    public void evictStaleEntries() {
        long cutoff = System.currentTimeMillis() - RETENTION_MS;
        lastRequestAt.entrySet().removeIf(entry -> entry.getValue() < cutoff);
    }
}
