package com.vpnsupport.bot;

import java.time.Instant;

public record GapStatsDto(String userQuery, int gapCount, String triggerReason,
                           Instant firstSeen, Instant lastSeen) {
}
