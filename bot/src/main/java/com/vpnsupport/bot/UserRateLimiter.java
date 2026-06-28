package com.vpnsupport.bot;

import org.springframework.stereotype.Component;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;

@Component
public class UserRateLimiter {

    private static final long MIN_INTERVAL_MS = 3_000;

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
}
