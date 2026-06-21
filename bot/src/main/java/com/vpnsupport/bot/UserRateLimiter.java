package com.vpnsupport.bot;

import org.springframework.stereotype.Component;

import java.util.concurrent.ConcurrentHashMap;

@Component
public class UserRateLimiter {

    private static final long MIN_INTERVAL_MS = 3_000;

    private final ConcurrentHashMap<Long, Long> lastRequestAt = new ConcurrentHashMap<>();

    public boolean tryAcquire(long userId) {
        long now = System.currentTimeMillis();
        Long previous = lastRequestAt.get(userId);
        if (previous != null && now - previous < MIN_INTERVAL_MS) {
            return false;
        }
        lastRequestAt.put(userId, now);
        return true;
    }
}
