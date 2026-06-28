package com.vpnsupport.bot;

public record TokenStatsDto(Long telegramId, long totalTokens, long promptTokens,
                             long completionTokens, long requestCount) {
}
