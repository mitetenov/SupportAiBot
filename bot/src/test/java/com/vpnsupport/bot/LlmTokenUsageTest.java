package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

class LlmTokenUsageTest {

    @Test
    void shouldCreateWithDefaultConstructor() {
        LlmTokenUsage usage = new LlmTokenUsage();
        assertNull(usage.getId());
        assertNull(usage.getTelegramId());
    }

    @Test
    void shouldCreateWithParameterizedConstructor() {
        LlmTokenUsage usage = new LlmTokenUsage(123L, 100L, 50L, 150L);
        assertNull(usage.getId());
        assertEquals(123L, usage.getTelegramId());
        assertEquals(100L, usage.getPromptTokens());
        assertEquals(50L, usage.getCompletionTokens());
        assertEquals(150L, usage.getTotalTokens());
        assertNotNull(usage.getCreatedAt());
    }

    @Test
    void shouldSetAndGetAllFields() {
        LlmTokenUsage usage = new LlmTokenUsage();
        usage.setId(1L);
        usage.setTelegramId(456L);
        usage.setPromptTokens(200L);
        usage.setCompletionTokens(100L);
        usage.setTotalTokens(300L);

        assertEquals(1L, usage.getId());
        assertEquals(456L, usage.getTelegramId());
        assertEquals(200L, usage.getPromptTokens());
        assertEquals(100L, usage.getCompletionTokens());
        assertEquals(300L, usage.getTotalTokens());
    }
}
